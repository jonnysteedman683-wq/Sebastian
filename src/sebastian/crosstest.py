"""Cross-testing (B73): run every agent's test suite against every agent you list.

``sebastian crosstest --manifest agents.json`` reads an owner-written manifest of agents. Each
agent has a test suite (a folder of case files) and a command that runs the agent on one task.
For each (suite, target) pair it copies each case's seed files into a fresh temporary workspace,
runs the target's command there, checks the workspace afterwards, and prints a
target-by-suite matrix of pass counts.

It is read-only towards every repo: suites are only read, each case runs in a throw-away temp
directory that is deleted afterwards, and nothing is committed, pushed or proposed. Proposing
fixes is a separate, human-started step (the existing ``evolve`` -> PR path).

Trust model: the manifest is the owner's own config, and its commands run exactly as written
(no shell, fixed argv, per-case timeout, process-group kill, capped output). Each agent's own
approval gate and sandbox apply inside its run. Case task text is passed to the target agent as
its task, so a suite is effectively a set of instructions to the target: only list suites you
trust as much as you would trust typing those tasks yourself.

Case format: a JSON file per case, compatible with Sebastian's scar files (``version: 1``,
``name``, ``task``, ``files``, ``expect``). The checks that work for any agent are the workspace
checks ``files``, ``contains`` and ``absent``, plus ``exit_code``. Sebastian-specific keys
(``stop``, ``no_gated_*``, ...) can't be observed from outside an agent, so they are ignored and
reported. A case with no checkable key is reported as ``unchecked``, never as a pass.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from sebastian.clock import Clock, SystemClock

MANIFEST_VERSION = 1
CASE_VERSION = 1
MAX_AGENTS = 20
MAX_CASES_PER_SUITE = 200
MAX_SEED_BYTES = 200_000
MAX_OUTPUT = 4000  # tail of the agent's output kept per case
DEFAULT_TIMEOUT = 600.0
TASK_FILE = "CROSSTEST_TASK.md"
CHECKED_KEYS = frozenset({"files", "contains", "absent", "exit_code"})
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# A runner executes argv in cwd with a timeout: returns (exit code or None on timeout, output).
Runner = Callable[[Sequence[str], Path, float], tuple[int | None, str]]


class CrossTestError(ValueError):
    """Invalid manifest or case file."""


@dataclass(frozen=True, slots=True)
class Agent:
    """One agent in the manifest."""

    name: str
    suite: Path
    command: tuple[str, ...]
    timeout: float = DEFAULT_TIMEOUT


@dataclass(frozen=True, slots=True)
class Case:
    """One portable test case."""

    name: str
    task: str
    files: Mapping[str, str]
    expect: Mapping[str, Any]
    ignored: tuple[str, ...] = ()  # expect keys that can't be checked from outside an agent


@dataclass(frozen=True, slots=True)
class CaseResult:
    """How one target agent did on one case from one suite."""

    suite: str
    target: str
    case: str
    status: str  # pass | fail | error | timeout | unchecked
    detail: str = ""
    seconds: float = 0.0
    output: str = ""


@dataclass(slots=True)
class Matrix:
    """All results, plus the summary the CLI prints."""

    agents: list[str]
    results: list[CaseResult] = field(default_factory=list)

    def cell(self, target: str, suite: str) -> tuple[int, int]:
        """(passed, checked) for one target on one suite; unchecked cases are not counted."""
        rows = [r for r in self.results if r.target == target and r.suite == suite]
        checked = [r for r in rows if r.status != "unchecked"]
        return sum(r.status == "pass" for r in checked), len(checked)

    def render(self) -> str:
        """Fixed-width table: one row per target agent, one column per suite."""
        width = max(8, *(len(a) for a in self.agents)) + 2
        head = "target \\ suite".ljust(width) + "".join(a.rjust(width) for a in self.agents)
        lines = [head, "-" * len(head)]
        for target in self.agents:
            cells = []
            for suite in self.agents:
                passed, checked = self.cell(target, suite)
                cells.append((f"{passed}/{checked}" if checked else "-").rjust(width))
            lines.append(target.ljust(width) + "".join(cells))
        bad = [r for r in self.results if r.status not in {"pass", "unchecked"}]
        if bad:
            lines += ["", "Failures:"]
            lines += [f"  {r.target} on {r.suite}/{r.case}: {r.status} {r.detail}" for r in bad]
        skipped = sum(r.status == "unchecked" for r in self.results)
        if skipped:
            lines.append(f"\n{skipped} case run(s) had no checkable expectation (unchecked).")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        """Machine-readable results (no agent output, which may be large or sensitive)."""
        return {
            "version": 1,
            "agents": self.agents,
            "results": [
                {k: getattr(r, k) for k in ("suite", "target", "case", "status", "detail")}
                | {"seconds": round(r.seconds, 3)}
                for r in self.results
            ],
        }


# --- loading ------------------------------------------------------------------------------


def load_manifest(path: Path) -> list[Agent]:
    """Parse and validate the owner's manifest. Relative suite paths are relative to it."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CrossTestError(f"cannot read manifest {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != MANIFEST_VERSION:
        raise CrossTestError(f"manifest must be a JSON object with version {MANIFEST_VERSION}")
    entries = raw.get("agents")
    if not isinstance(entries, list) or not entries or len(entries) > MAX_AGENTS:
        raise CrossTestError(f"manifest needs 1..{MAX_AGENTS} agents")
    agents = [_agent(e, path.parent) for e in entries]
    names = [a.name for a in agents]
    if len(set(names)) != len(names):
        raise CrossTestError("agent names must be unique")
    return agents


def _agent(e: object, base: Path) -> Agent:
    if not isinstance(e, dict):
        raise CrossTestError("each agent must be an object")
    name, suite, command = e.get("name"), e.get("suite"), e.get("command")
    if not isinstance(name, str) or not _NAME.match(name):
        raise CrossTestError(f"invalid agent name {name!r}")
    if not isinstance(suite, str) or not suite:
        raise CrossTestError(f"{name}: suite must be a path")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(c, str) and c for c in command)
    ):
        raise CrossTestError(f"{name}: command must be a non-empty list of strings (no shell)")
    if not any(p in c for c in command for p in ("{task}", "{task_file}")):
        raise CrossTestError(f"{name}: command must use {{task}} or {{task_file}}")
    timeout = e.get("timeout", DEFAULT_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise CrossTestError(f"{name}: timeout must be a positive number of seconds")
    unknown = set(e) - {"name", "suite", "command", "timeout"}
    if unknown:
        raise CrossTestError(f"{name}: unknown key(s) {sorted(unknown)}")
    return Agent(name, (base / suite).resolve(), tuple(command), float(timeout))


def load_suite(directory: Path) -> list[Case]:
    """Every ``*.json`` case in ``directory``, sorted by file name. Missing folder -> []."""
    if not directory.is_dir():
        return []
    paths = sorted(directory.glob("*.json"))
    if len(paths) > MAX_CASES_PER_SUITE:
        raise CrossTestError(f"{directory}: more than {MAX_CASES_PER_SUITE} cases")
    cases = []
    for p in paths:
        try:
            cases.append(parse_case(json.loads(p.read_text(encoding="utf-8"))))
        except (json.JSONDecodeError, CrossTestError) as exc:
            raise CrossTestError(f"{p.name}: {exc}") from exc
    return cases


def parse_case(spec: object) -> Case:
    """Validate one case spec (scar-compatible)."""
    if not isinstance(spec, dict) or spec.get("version") != CASE_VERSION:
        raise CrossTestError(f"case must be a JSON object with version {CASE_VERSION}")
    name, task, files = spec.get("name"), spec.get("task"), spec.get("files", {})
    if not isinstance(name, str) or not _NAME.match(name):
        raise CrossTestError(f"invalid case name {name!r}")
    if not isinstance(task, str) or not task.strip():
        raise CrossTestError(f"{name}: task must be a non-empty string")
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in files.items()
    ):
        raise CrossTestError(f"{name}: files must map paths to text")
    if sum(len(v) for v in files.values()) > MAX_SEED_BYTES:
        raise CrossTestError(f"{name}: seed files exceed {MAX_SEED_BYTES} chars")
    for rel in files:
        _safe_rel(rel, name)
    expect = spec.get("expect", {})
    if not isinstance(expect, dict):
        raise CrossTestError(f"{name}: expect must be an object")
    _validate_expect(expect, name)
    ignored = tuple(sorted(set(expect) - CHECKED_KEYS))
    return Case(name, task, dict(files), dict(expect), ignored)


def _safe_rel(rel: str, name: str) -> str:
    p = PurePosixPath(rel)
    if not rel or p.is_absolute() or ".." in p.parts or "\\" in rel or "\x00" in rel:
        raise CrossTestError(f"{name}: path escapes the workspace: {rel!r}")
    return p.as_posix()


def _validate_expect(e: Mapping[str, Any], name: str) -> None:
    for key in ("files", "contains"):
        v = e.get(key, {})
        if not isinstance(v, dict) or not all(
            isinstance(k, str) and isinstance(x, str) for k, x in v.items()
        ):
            raise CrossTestError(f"{name}: expect.{key} must map paths to strings")
        for rel in v:
            _safe_rel(rel, name)
    absent = e.get("absent", [])
    if not isinstance(absent, list) or not all(isinstance(x, str) for x in absent):
        raise CrossTestError(f"{name}: expect.absent must be a list of paths")
    for rel in absent:
        _safe_rel(rel, name)
    code = e.get("exit_code", 0)
    if isinstance(code, bool) or not isinstance(code, int):
        raise CrossTestError(f"{name}: expect.exit_code must be an integer")


# --- running ------------------------------------------------------------------------------


def build_argv(command: Sequence[str], task: str, task_file: Path, workspace: Path) -> list[str]:
    """Fill the placeholders in each argv element. No shell is ever involved."""
    values = {"{task}": task, "{task_file}": str(task_file), "{workspace}": str(workspace)}
    out = []
    for part in command:
        filled = part
        for key, value in values.items():
            filled = filled.replace(key, value)
        out.append(filled)
    return out


def subprocess_runner(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int | None, str]:
    """Run without a shell in its own process group; kill the whole group on timeout."""
    try:
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        return -1, f"could not start {argv[0]!r}: {exc}"
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        return None, out[-MAX_OUTPUT:]
    return proc.returncode, out[-MAX_OUTPUT:]


def check_case(case: Case, workspace: Path, exit_code: int) -> tuple[str, str]:
    """(status, detail) for a finished run. Only the workspace and exit code are observed."""
    e = case.expect
    if not set(e) & CHECKED_KEYS:
        ignored = ", ".join(case.ignored) or "empty"
        return "unchecked", f"no checkable expectation ({ignored})"

    def read(rel: str) -> str | None:
        p = workspace / rel
        try:
            return p.read_text(encoding="utf-8") if p.is_file() and not p.is_symlink() else None
        except (OSError, UnicodeDecodeError):
            return None

    problems = []
    if "exit_code" in e and exit_code != e["exit_code"]:
        problems.append(f"exit code {exit_code}, expected {e['exit_code']}")
    problems += [f"{rel} differs" for rel, want in e.get("files", {}).items() if read(rel) != want]
    problems += [
        f"{rel} lacks {needle!r}"
        for rel, needle in e.get("contains", {}).items()
        if needle not in (read(rel) or "")
    ]
    problems += [f"{rel} exists" for rel in e.get("absent", []) if (workspace / rel).exists()]
    return ("fail", "; ".join(problems)[:300]) if problems else ("pass", "")


def run_case(
    suite: str,
    target: Agent,
    case: Case,
    runner: Runner = subprocess_runner,
    clock: Clock | None = None,
) -> CaseResult:
    """Run ``target`` on ``case`` in a fresh temp workspace and check the result."""
    clk = clock or SystemClock()
    with tempfile.TemporaryDirectory(prefix=f"crosstest-{target.name}-") as tmp:
        ws = Path(tmp) / "workspace"
        ws.mkdir()
        for rel, text in case.files.items():
            dest = ws / _safe_rel(rel, case.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        task_file = Path(tmp) / TASK_FILE  # outside the workspace, so checks never see it
        task_file.write_text(case.task, encoding="utf-8")
        argv = build_argv(target.command, case.task, task_file, ws)
        start = clk.monotonic()
        code, output = runner(argv, ws, target.timeout)
        seconds = clk.monotonic() - start
        if code is None:
            return CaseResult(
                suite, target.name, case.name, "timeout", f"after {target.timeout:g}s", seconds,
                output,
            )  # fmt: skip
        if code == -1 and output.startswith("could not start"):
            return CaseResult(suite, target.name, case.name, "error", output[:300], seconds)
        status, detail = check_case(case, ws, code)
        return CaseResult(suite, target.name, case.name, status, detail, seconds, output)


def plan(
    agents: Sequence[Agent], targets: set[str] | None, suites: set[str] | None
) -> list[tuple[Agent, Agent, Case]]:
    """Every (suite owner, target, case) triple to run, in a stable order."""
    chosen_t = [a for a in agents if not targets or a.name in targets]
    chosen_s = [a for a in agents if not suites or a.name in suites]
    return [(s, t, c) for s in chosen_s for c in load_suite(s.suite) for t in chosen_t]


def run_matrix(
    agents: Sequence[Agent],
    targets: set[str] | None = None,
    suites: set[str] | None = None,
    runner: Runner = subprocess_runner,
    clock: Clock | None = None,
    on_result: Callable[[CaseResult], None] | None = None,
) -> Matrix:
    """Run the whole plan. Each case is independent; one agent failing stops nothing else."""
    matrix = Matrix([a.name for a in agents])
    for suite, target, case in plan(agents, targets, suites):
        result = run_case(suite.name, target, case, runner, clock)
        matrix.results.append(result)
        if on_result:
            on_result(result)
    return matrix
