"""Gauntlet core: the pure, dependency-free contracts shared across Jonny's agents.

This file is the single source of truth for the Gauntlet's stage machine and its strict reply
parsers. It imports only the standard library and must stay that way, so the identical file can
be vendored into other agents (Lestinxor ships a byte-identical copy as
``lestinxor/gauntlet_core.py``). ``CORE_VERSION`` and the SHA-256 of this file are what the
daily sync compares to detect drift.

Contents: ``Stage`` and the transition policy (``next_stage``), the PREDICT contract
(``parse_prediction``) and the CRITIQUE contract (``parse_verdict``). Everything that touches
tools, approval, the workspace or an LLM stays in the host agent (``sebastian.gauntlet``).

Rules for editing: no third-party or host-package imports, Python 3.10 compatible, no behaviour
change without bumping ``CORE_VERSION`` and updating every vendored copy in the same cycle.
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import PurePosixPath
from typing import Protocol

CORE_VERSION = "1.0.0"

if sys.version_info >= (3, 11):  # noqa: UP036 - vendored into a Python 3.10 host
    from enum import StrEnum as _StrEnum
else:  # pragma: no cover - exercised by hosts on Python 3.10

    class _StrEnum(str, Enum):  # noqa: UP042 - StrEnum does not exist on 3.10
        """Python 3.10 stand-in for ``enum.StrEnum``: str() and format() give the value."""

        def __str__(self) -> str:
            return str(self.value)

        def __format__(self, spec: str) -> str:
            return format(str(self.value), spec)


MAX_PREDICTED_FILES = 20
MAX_PREDICTED_COMMANDS = 5
MAX_COMMAND_CHARS = 300
_EXE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")
_SHELL_CHARS = frozenset("|;&<>`$\n")  # checks run with no shell; reject operators outright
_BLOCK = re.compile(r"<checks>(.*?)</checks>", re.DOTALL)


class Stage(_StrEnum):
    """Where a Gauntlet run is."""

    PLAN = "plan"
    PREDICT = "predict"
    ACT = "act"
    VERIFY = "verify"
    CRITIQUE = "critique"
    DONE = "done"
    HELP = "help"


TERMINAL = frozenset({Stage.DONE, Stage.HELP})
_RETRYABLE_STAGES = frozenset({Stage.VERIFY, Stage.CRITIQUE})
_FORWARD = {
    Stage.PLAN: Stage.PREDICT,
    Stage.PREDICT: Stage.ACT,
    Stage.ACT: Stage.VERIFY,
    Stage.VERIFY: Stage.CRITIQUE,
    Stage.CRITIQUE: Stage.DONE,
}


class GauntletError(ValueError):
    """Invalid policy, prediction or transition."""


class LoopPolicy(Protocol):
    """The policy fields the core reads. Hosts supply their own policy object."""

    @property
    def max_loops(self) -> int:
        """Failed VERIFY/CRITIQUE rounds before HELP."""
        ...

    @property
    def replan_after(self) -> int:
        """Every Nth failed round goes back to PLAN instead of ACT."""
        ...

    @property
    def checks_required(self) -> bool:
        """Predicted Python changes must come with a check command."""
        ...


@dataclass(frozen=True)
class Prediction:
    """What PREDICT says will change and how VERIFY will check it."""

    files: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()

    @property
    def touches_code(self) -> bool:
        """True when any predicted file is Python source."""
        return any(f.endswith(".py") for f in self.files)


@dataclass(frozen=True)
class Transition:
    """The result of leaving a stage."""

    stage: Stage
    loops: int  # failed VERIFY/CRITIQUE rounds so far
    reason: str = ""


def next_stage(stage: Stage, passed: bool, loops: int, policy: LoopPolicy) -> Transition:
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
    if stage not in _RETRYABLE_STAGES:
        return Transition(Stage.HELP, loops, f"{stage} failed")
    loops += 1
    if loops >= policy.max_loops:
        return Transition(Stage.HELP, loops, f"{stage} failed {loops} time(s); loop limit reached")
    if loops % policy.replan_after == 0:
        return Transition(Stage.PLAN, loops, f"{stage} failed; replanning")
    return Transition(Stage.ACT, loops, f"{stage} failed; retrying")


def parse_prediction(text: str, policy: LoopPolicy | None = None) -> Prediction:
    """Strictly parse the single ``<checks>`` block. Any malformed line rejects the whole block.

    With no policy, Python changes must come with a check (the safe default).
    """
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
            files.append(check_path(value))
        elif key.strip() == "run":
            commands.append(check_command(value))
        else:
            raise GauntletError(f"unknown key {key.strip()[:20]!r}; use file: or run:")
    if len(files) > MAX_PREDICTED_FILES:
        raise GauntletError(f"too many files predicted (max {MAX_PREDICTED_FILES})")
    if len(commands) > MAX_PREDICTED_COMMANDS:
        raise GauntletError(f"too many commands predicted (max {MAX_PREDICTED_COMMANDS})")
    pred = Prediction(tuple(dict.fromkeys(files)), tuple(dict.fromkeys(commands)))
    if not pred.files and not pred.commands:
        raise GauntletError("prediction is empty")
    checks_required = True if policy is None else policy.checks_required
    if checks_required and pred.touches_code and not pred.commands:
        raise GauntletError("Python changes predicted but no run: check given")
    return pred


def check_path(value: str) -> str:
    """Reject a predicted path that is absolute, climbs out, or smuggles separators/NULs."""
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or "\\" in value or "\x00" in value:
        raise GauntletError(f"predicted path escapes the workspace: {value[:80]!r}")
    return p.as_posix()


def check_command(value: str) -> str:
    """Reject a predicted check that is too long, uses shell operators or no bare executable."""
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


# --- CRITIQUE contract ----------------------------------------------------------------------

MAX_FINDINGS = 10
MAX_FINDING_CHARS = 300
_VERDICT = re.compile(r"\AVERDICT:\s*(PASS|FAIL)\s*\Z")


@dataclass(frozen=True)
class Verdict:
    """A parsed critic reply. Anything malformed is a FAIL."""

    passed: bool
    findings: tuple[str, ...] = ()
    malformed: bool = False


def parse_verdict(text: str) -> Verdict:
    """Strict parse: first line ``VERDICT: PASS|FAIL``, then only ``- finding`` lines.

    A second VERDICT line, any other text, or a FAIL with no findings is malformed, and malformed
    always means FAIL. A PASS carrying findings is treated as a FAIL: the critic contradicted
    itself.
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    m = _VERDICT.match(lines[0]) if lines else None
    if m is None:
        return Verdict(False, ("critic reply did not start with VERDICT: PASS or FAIL",), True)
    findings = []
    for ln in lines[1:]:
        if not ln.startswith("- ") or ln.upper().lstrip("- ").startswith("VERDICT"):
            return Verdict(False, (f"critic reply malformed near: {ln[:80]!r}",), True)
        findings.append(ln[2:].strip()[:MAX_FINDING_CHARS])
    if len(findings) > MAX_FINDINGS:
        return Verdict(False, (f"critic gave more than {MAX_FINDINGS} findings",), True)
    if m.group(1) == "PASS":
        return Verdict(not findings, tuple(findings))
    if not findings:
        return Verdict(False, ("critic said FAIL without saying why",), True)
    return Verdict(False, tuple(findings))
