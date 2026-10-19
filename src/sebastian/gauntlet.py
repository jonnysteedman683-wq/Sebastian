"""Gauntlet stages (B11, design: ``docs/design/B11-gauntlet.md``).

A Gauntlet run passes PLAN -> PREDICT -> ACT -> VERIFY -> CRITIQUE -> DONE. VERIFY and
CRITIQUE can send the run back to ACT (or to PLAN after repeated failures). The loop is bounded
by ``GauntletPolicy.max_loops``. Past that bound, or when a stage cannot produce anything usable,
the run goes to HELP: it stops and reports, nothing more.

This module is B11a: the stage machine and the PREDICT stage. It is pure logic with no tools and
no I/O, and it is not wired into the agent or the CLI yet. Every model call it makes is
tool-free, and the caller charges each call to the run's ``Governor``.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from sebastian.llm.base import LLM
from sebastian.types import LLMResponse, Message, Role

MAX_PREDICTED_FILES = 20
MAX_PREDICTED_COMMANDS = 5
MAX_COMMAND_CHARS = 300
_EXE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")
_SHELL_CHARS = frozenset("|;&<>`$\n")  # run_command uses no shell; reject them to avoid confusion
_BLOCK = re.compile(r"<checks>(.*?)</checks>", re.DOTALL)

PREDICT_INSTRUCTION = """\

You are in the PREDICT phase. Tools are not available. Before acting, state how success will be \
checked. Reply with exactly one block and nothing else:

<checks>
file: <relative path you expect to create or change>
run: <a command that must exit 0 when the task is done, e.g. pytest -q>
</checks>

Use one line per item. List each file you expect to touch. Give at most 5 `run:` lines; each \
must be a single command with no shell operators. If you will change Python code, include at \
least one `run:` line that tests it.
"""


class Stage(StrEnum):
    """Where a Gauntlet run is."""

    PLAN = "plan"
    PREDICT = "predict"
    ACT = "act"
    VERIFY = "verify"
    CRITIQUE = "critique"
    DONE = "done"
    HELP = "help"


TERMINAL = frozenset({Stage.DONE, Stage.HELP})
_FORWARD = {
    Stage.PLAN: Stage.PREDICT,
    Stage.PREDICT: Stage.ACT,
    Stage.ACT: Stage.VERIFY,
    Stage.VERIFY: Stage.CRITIQUE,
    Stage.CRITIQUE: Stage.DONE,
}


class GauntletError(ValueError):
    """Invalid policy, prediction or transition."""


@dataclass(frozen=True, slots=True)
class GauntletPolicy:
    """Bounds for one Gauntlet run (design section 3)."""

    max_loops: int = 3  # failed VERIFY/CRITIQUE rounds before HELP
    replan_after: int = 2  # every Nth failed round goes back to PLAN instead of ACT
    checks_required: bool = True  # predicted Python changes must come with a check command
    critic_llm: LLM | None = None  # used from B11c; None means the planner model

    def __post_init__(self) -> None:
        if self.max_loops < 1:
            raise GauntletError("max_loops must be at least 1")
        if self.replan_after < 1:
            raise GauntletError("replan_after must be at least 1")


@dataclass(frozen=True, slots=True)
class Prediction:
    """What PREDICT says will change and how VERIFY will check it."""

    files: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()

    @property
    def touches_code(self) -> bool:
        """True when any predicted file is Python source."""
        return any(f.endswith(".py") for f in self.files)


@dataclass(frozen=True, slots=True)
class Transition:
    """The result of leaving a stage."""

    stage: Stage
    loops: int  # failed VERIFY/CRITIQUE rounds so far
    reason: str = ""


def next_stage(stage: Stage, passed: bool, loops: int, policy: GauntletPolicy) -> Transition:
    """Where to go after ``stage`` passed or failed its gate.

    PLAN, PREDICT and ACT failing means there is nothing to check, so the run goes to HELP. A
    failed VERIFY or CRITIQUE counts one loop; at ``max_loops`` the run goes to HELP. Otherwise,
    every ``replan_after``-th loop goes back to PLAN and the rest go back to ACT.
    """
    if stage in TERMINAL:
        raise GauntletError(f"{stage} is terminal")
    if loops < 0:
        raise GauntletError("loops must be non-negative")
    if passed:
        return Transition(_FORWARD[stage], loops)
    if stage not in (Stage.VERIFY, Stage.CRITIQUE):
        return Transition(Stage.HELP, loops, f"{stage} failed")
    loops += 1
    if loops >= policy.max_loops:
        return Transition(Stage.HELP, loops, f"{stage} failed {loops} time(s); loop limit reached")
    if loops % policy.replan_after == 0:
        return Transition(Stage.PLAN, loops, f"{stage} failed; replanning")
    return Transition(Stage.ACT, loops, f"{stage} failed; retrying")


def parse_prediction(text: str, policy: GauntletPolicy | None = None) -> Prediction:
    """Strictly parse the single ``<checks>`` block. Any malformed line rejects the whole block."""
    blocks = _BLOCK.findall(text)
    if len(blocks) != 1:
        raise GauntletError(f"expected exactly one <checks> block, found {len(blocks)}")
    files: list[str] = []
    commands: list[str] = []
    for raw in blocks[0].splitlines():
        line = raw.strip()
        if not line:
            continue
        key, sep, value = line.partition(":")
        value = value.strip()
        if not sep or not value:
            raise GauntletError(f"malformed line: {line[:80]!r}")
        if key.strip() == "file":
            files.append(_check_path(value))
        elif key.strip() == "run":
            commands.append(_check_command(value))
        else:
            raise GauntletError(f"unknown key {key.strip()[:20]!r}; use file: or run:")
    if len(files) > MAX_PREDICTED_FILES:
        raise GauntletError(f"too many files predicted (max {MAX_PREDICTED_FILES})")
    if len(commands) > MAX_PREDICTED_COMMANDS:
        raise GauntletError(f"too many commands predicted (max {MAX_PREDICTED_COMMANDS})")
    pred = Prediction(tuple(dict.fromkeys(files)), tuple(dict.fromkeys(commands)))
    if not pred.files and not pred.commands:
        raise GauntletError("prediction is empty")
    if (policy or GauntletPolicy()).checks_required and pred.touches_code and not pred.commands:
        raise GauntletError("Python changes predicted but no run: check given")
    return pred


def _check_path(value: str) -> str:
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or "\\" in value or "\x00" in value:
        raise GauntletError(f"predicted path escapes the workspace: {value[:80]!r}")
    return p.as_posix()


def _check_command(value: str) -> str:
    if len(value) > MAX_COMMAND_CHARS:
        raise GauntletError("predicted command too long")
    if any(ch in value for ch in _SHELL_CHARS):
        raise GauntletError(f"shell operators are not allowed: {value[:80]!r}")
    try:
        argv = shlex.split(value)
    except ValueError as exc:
        raise GauntletError(f"unparseable command: {exc}") from exc
    if not argv or not _EXE.match(argv[0]):
        raise GauntletError(f"command must start with a bare executable name: {value[:80]!r}")
    return shlex.join(argv)


def predict_messages(task: str, plan: str = "") -> list[Message]:
    """The single user turn for the PREDICT call."""
    text = f"<task>\n{task}\n</task>"
    if plan.strip():
        text += f"\n<plan>\n{plan.strip()[:4000]}\n</plan>"
    return [Message(Role.USER, text=text)]


@dataclass(frozen=True, slots=True)
class PredictOutcome:
    """The PREDICT call's result. ``prediction`` is None when the reply was rejected."""

    response: LLMResponse
    prediction: Prediction | None
    error: str = ""

    @property
    def passed(self) -> bool:
        """Whether PREDICT passed its gate."""
        return self.prediction is not None


def predict(
    llm: LLM, system: str, task: str, plan: str = "", policy: GauntletPolicy | None = None
) -> PredictOutcome:
    """Run the tool-free PREDICT call and parse it.

    The response is always returned, so the caller can charge its usage to the governor even
    when the reply is rejected. ``LLMError`` propagates, as in the planning call.
    """
    resp = llm.complete(system + PREDICT_INSTRUCTION, predict_messages(task, plan), [])
    try:
        return PredictOutcome(resp, parse_prediction(resp.text, policy))
    except GauntletError as exc:
        return PredictOutcome(resp, None, str(exc))
