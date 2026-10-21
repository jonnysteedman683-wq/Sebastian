"""Ouroboros: Sebastian improves its own codebase, one backlog item at a time.

One cycle (``sebastian evolve``):

1. Take the first unchecked, unlocked item from ``docs/BACKLOG.md`` (the same rule Jules uses).
2. Create a fresh git worktree on a new branch from the base ref, plus a baseline worktree.
3. Run the agent in the candidate worktree under a *scoped approver*: it may write any file
   except guarded paths (read from ``.github/workflows/guard.yml``, so there is one source of
   truth) and may run only the allowlisted check commands. Everything else is denied.
4. Gates, all deterministic, in order: the diff is non-empty; it touches no guarded path (which
   includes the safety TCB, P1-03); it deletes or renames no existing test; it adds no top-level
   module under ``src/``; every check command passes against the *baseline's* tests, evals and
   config (P1-04), and the candidate's own tests too when it changed them; collected tests and
   coverage don't drop; the eval score (scored by the baseline's evals) is at least the
   baseline's, which must be readable unless ``allow_missing_baseline``.
5. Only if every gate passes is the change committed. Pushing the branch and opening a PR are
   opt-in. Nothing is ever merged: a human (or a later trust level) does that.

A rejected attempt leaves no branch behind; its diff is saved under
``$SEBASTIAN_HOME/evolve/`` for inspection.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sebastian.agent import Sebastian
from sebastian.approval import ApprovalGate
from sebastian.budget import Budget
from sebastian.checkpoint import Checkpoints
from sebastian.clock import Clock, SystemClock
from sebastian.gauntlet import GauntletPolicy
from sebastian.llm.base import LLM
from sebastian.session import SessionStore
from sebastian.tools import (
    CommandPolicy,
    Permission,
    Tool,
    ToolRegistry,
    Workspace,
    command_tool,
    file_tools,
)
from sebastian.tools.builtin import SANDBOX_WORKDIR, bwrap_argv, bwrap_usable, python_roots
from sebastian.trace import NullTracer, Tracer
from sebastian.types import StopReason, ToolCall

# Fallback when guard.yml cannot be read; must equal its `guarded=` regex (a test checks).
# P1-03: also the safety TCB, so neither evolve nor an unlabelled PR can change it.
DEFAULT_GUARDED = (
    r"^("
    r"\.github/|"
    r"src/sebastian/watch/|"
    r"src/sebastian/tools/|"
    r"src/sebastian/llm/|"
    r"src/sebastian/evals/|"
    r"src/sebastian/(approval|agent|budget|evolve|gauntlet|checkpoint|session|trace)\.py$|"
    r"pyproject\.toml$|"
    r"scripts/|"
    r"schemas/repo_change_report\.schema\.json$|"
    r"schemas/README\.md$|"
    r"docs/watch/|"
    r"docs/WORK_ORDER\.md$|"
    r"AGENTS\.md$|"
    r"CLAUDE\.md$"
    r")"
)
_GUARD_LINE = re.compile(r"""guarded='([^']+)'""")
_ITEM = re.compile(r"^- \[ \] (?:🔒\s*)?\*\*(?P<id>B\d+)\b(?P<title>[^*]*)\*\*(?P<rest>.*)$")
_SCORE = re.compile(r"^score (\d+)% \((\d+)/(\d+)\)", re.MULTILINE)
CHECK_TIMEOUT = 900.0


class EvolveError(RuntimeError):
    """A cycle could not run (not a rejected change: that is a normal outcome)."""


@dataclass(frozen=True, slots=True)
class BacklogItem:
    """One backlog entry the cycle will attempt."""

    id: str
    title: str
    text: str


@dataclass(slots=True)
class EvolveReport:
    """What one cycle did. ``accepted`` means committed (and pushed, if asked)."""

    item: BacklogItem | None
    branch: str = ""
    accepted: bool = False
    reason: str = ""
    changed: list[str] = field(default_factory=list)
    gates: dict[str, bool] = field(default_factory=dict)
    baseline_score: float | None = None
    candidate_score: float | None = None
    patch: Path | None = None
    run_stop: str = ""
    resumes: int = 0  # times the agent run was resumed after hitting its budget
    sandbox: str = ""  # how the gates ran: "bwrap" or "none"
    baseline_metrics: tuple[int | None, float | None] | None = None  # P1-04: (tests, coverage)
    candidate_metrics: tuple[int | None, float | None] | None = None

    def summary(self) -> str:
        """One readable block for the CLI."""
        if self.item is None:
            return "evolve: no unlocked backlog item left"
        verdict = "ACCEPTED" if self.accepted else "REJECTED"
        lines = [f"{verdict} {self.item.id}: {self.item.title}", f"  reason: {self.reason}"]
        if self.branch:
            lines.append(f"  branch: {self.branch}")
        if self.sandbox:
            lines.append(f"  gate sandbox: {self.sandbox} (no secrets in gate env)")
        if self.resumes:
            lines.append(f"  agent resumed: {self.resumes}x after hitting its budget")
        if self.gates:
            gates = ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in self.gates.items())
            lines.append(f"  gates: {gates}")
        if self.baseline_score is not None or self.candidate_score is not None:
            base, cand = _pct(self.baseline_score), _pct(self.candidate_score)
            lines.append(f"  eval: baseline={base} candidate={cand}")
        if self.baseline_metrics is not None or self.candidate_metrics is not None:
            lines.append(
                f"  tests/coverage: baseline={self.baseline_metrics} "
                f"candidate={self.candidate_metrics}"
            )
        if self.changed:
            lines.append(f"  changed: {', '.join(self.changed)}")
        if self.patch:
            lines.append(f"  patch: {self.patch}")
        return "\n".join(lines)


def _pct(score: float | None) -> str:
    return "n/a" if score is None else f"{score:.0%}"


# --- policy ------------------------------------------------------------------------------


def next_item(backlog: str, item_id: str | None = None) -> BacklogItem | None:
    """First unchecked item with no 🔒 anywhere in it (or the named one, also unlocked)."""
    lines = backlog.splitlines()
    for i, line in enumerate(lines):
        m = _ITEM.match(line)
        if not m or (item_id and m["id"] != item_id):
            continue
        body = [line]
        for follow in lines[i + 1 :]:
            if not follow.startswith("  "):
                break
            body.append(follow)
        text = "\n".join(body)
        if "🔒" in text:
            continue
        return BacklogItem(m["id"], m["title"].strip(" .") or m["id"], text)
    return None


def load_guarded(repo: Path) -> re.Pattern[str]:
    """Guarded-path regex from the repo's guard workflow, else the built-in default."""
    try:
        m = _GUARD_LINE.search((repo / ".github/workflows/guard.yml").read_text(encoding="utf-8"))
    except OSError:
        m = None
    return re.compile(m.group(1) if m else DEFAULT_GUARDED)


class ScopedApprover:
    """Approves only what a self-improvement cycle needs; denies everything else.

    WRITE tools: approved when ``path`` is not guarded. EXEC: approved when the executable is
    one of the allowed check commands (``run_command`` enforces the allowlist again).
    """

    def __init__(self, root: Path, guarded: re.Pattern[str], commands: frozenset[str]) -> None:
        self.root = root.resolve()
        self.guarded = guarded
        self.commands = commands
        self.denied: list[str] = []

    def approve(self, tool: Tool, call: ToolCall) -> bool:
        """Return True only for in-scope writes and allowlisted commands."""
        ok = False
        if tool.permission is Permission.WRITE:
            path = call.arguments.get("path")
            rel = self._relative(path) if isinstance(path, str) else None
            ok = (
                rel is not None
                and not self.guarded.search(rel)
                and Path(rel).name not in GIT_CONFIG_FILES
            )
        elif tool.permission is Permission.EXEC:
            cmd = call.arguments.get("command")
            words = cmd.split() if isinstance(cmd, str) else []
            ok = bool(words) and words[0] in self.commands
        if not ok:
            self.denied.append(f"{tool.name} {call.arguments}"[:200])
        return ok

    def _relative(self, path: str) -> str | None:
        """Repo-relative POSIX path after resolving ``..`` and symlinks; None if outside."""
        try:
            return (self.root / path).resolve().relative_to(self.root).as_posix()
        except (ValueError, OSError):
            return None


# --- git and commands --------------------------------------------------------------------


# P1-05: host git (outside any sandbox) runs in worktrees the agent could write to. Never run
# hooks or an fsmonitor command, never follow file:// remotes, never read /etc/gitconfig.
GIT_HARDENING = (
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "protocol.file.allow=never",
)  # fmt: skip
# Files that make git run filters/diff drivers or fetch submodules; evolve never writes them.
GIT_CONFIG_FILES = frozenset({".gitattributes", ".gitmodules"})


def _git(repo: Path, *args: str) -> str:
    exe = shutil.which("git")
    if exe is None:
        raise EvolveError("git is not installed")
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1")
    proc = subprocess.run(
        [exe, *GIT_HARDENING, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise EvolveError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def resolve_sandbox(mode: str, *, unsafe: bool = False) -> str | None:
    """Path to a usable bwrap for ``mode`` (auto | bwrap | none); None means unsandboxed.

    Fails closed (P1-02): evolve runs code the agent just wrote, so it never runs it on the
    host unless ``unsafe`` (``--unsafe-no-sandbox``) confirms that. ``auto`` without a usable
    bubblewrap and ``none`` both need it; ``bwrap`` always requires bubblewrap.
    """
    if mode not in {"auto", "bwrap", "none"}:
        raise EvolveError(f"unknown sandbox mode {mode!r}; use auto, bwrap or none")
    if mode == "none":
        if not unsafe:
            raise EvolveError(
                "--sandbox none runs agent-written code on the host; "
                "confirm with --unsafe-no-sandbox"
            )
        return None
    exe = shutil.which("bwrap")
    if exe is not None and bwrap_usable(exe):
        return exe
    if mode == "bwrap":
        raise EvolveError("sandbox 'bwrap' required but bubblewrap is missing or blocked here")
    if unsafe:
        return None
    raise EvolveError(
        "bubblewrap is missing or blocked here, so evolve will not run agent-written code "
        "(--sandbox auto fails closed); install bubblewrap, or pass --unsafe-no-sandbox"
    )


# The only variables a gate command (or bwrap itself) inherits: no API keys or tokens.
GATE_ENV_PASSTHROUGH = frozenset({"PATH", "LANG", "LC_ALL"})


def gate_env(cwd: Path) -> dict[str, str]:
    """A scrubbed environment for gate commands: no secrets from the parent process."""
    env = {k: v for k, v in os.environ.items() if k in GATE_ENV_PASSTHROUGH}
    return env | {"HOME": str(cwd), "PYTHONPATH": str(cwd / "src")}


def _run(
    argv: Sequence[str], cwd: Path, bwrap: str | None = None, timeout: float = CHECK_TIMEOUT
) -> tuple[bool, str]:
    """Run a gate command in ``cwd``; inside bubblewrap when ``bwrap`` is given.

    Either way the command never sees the parent's environment (P1-02): only ``PATH``,
    ``LANG``, ``LC_ALL``, ``HOME`` and ``PYTHONPATH`` are passed.
    """
    cmd = list(argv)
    env = gate_env(cwd)
    if bwrap is not None:
        exe = cmd[0] if os.path.isabs(cmd[0]) else shutil.which(cmd[0]) or cmd[0]
        inner = {k: v for k, v in env.items() if k in GATE_ENV_PASSTHROUGH}
        inner |= {"HOME": SANDBOX_WORKDIR, "PYTHONPATH": f"{SANDBOX_WORKDIR}/src"}
        cmd = bwrap_argv(bwrap, cwd, exe, cmd[1:], inner, extra_ro=python_roots())
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except OSError as exc:
        return False, str(exc)
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-4000:]


def parse_score(output: str) -> float | None:
    """Score from the eval runner's ``score N% (a/b)`` line."""
    m = _SCORE.search(output)
    return int(m[2]) / int(m[3]) if m and int(m[3]) else None


def default_checks() -> list[list[str]]:
    """The repo's own CI checks, run with this interpreter."""
    py = sys.executable
    return [
        [py, "-m", "ruff", "check", "."],
        [py, "-m", "ruff", "format", "--check", "."],
        [py, "-m", "mypy"],
        [py, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
    ]


def default_eval() -> list[str]:
    """Offline eval suite; prints the score line."""
    return [sys.executable, "-m", "sebastian.evals", "--min-score", "0"]


def default_metrics() -> list[str]:
    """Test suite with coverage; prints the pytest summary and the coverage total.

    No ``-q``: the repo's addopts already has one, and ``-qq`` drops the summary line.
    """
    return [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--cov=sebastian"]


_SUMMARY = re.compile(r"^.*\bin \d+(?:\.\d+)?s\b.*$", re.MULTILINE)
_COUNT = re.compile(r"(\d+) (passed|failed|skipped|xfailed|xpassed|errors?)\b")
_TOTAL_COV = re.compile(r"Total coverage: (\d+(?:\.\d+)?)%")
_TOTAL_ROW = re.compile(r"^TOTAL\s.*?(\d+(?:\.\d+)?)%\s*$", re.MULTILINE)


def parse_metrics(output: str) -> tuple[int | None, float | None]:
    """``(collected tests, coverage %)`` from pytest/pytest-cov output; None when absent."""
    count = None
    summaries = _SUMMARY.findall(output)
    if summaries:
        found = _COUNT.findall(summaries[-1])
        count = sum(int(n) for n, _ in found) if found else None
    cov = _TOTAL_COV.findall(output) or _TOTAL_ROW.findall(output)
    return count, float(cov[-1]) if cov else None


# P1-04: what decides what the gates check. The independent gates always take these from the
# baseline, so the candidate can't loosen its own tests, evals or tool configuration.
GATE_CONFIG = (
    "tests",
    "evals",
    "pyproject.toml",
    "conftest.py",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "ruff.toml",
    ".ruff.toml",
    "mypy.ini",
    ".mypy.ini",
    ".coveragerc",
)


_NOT_COPIED = (".git", ".mypy_cache", ".ruff_cache", ".pytest_cache", "__pycache__", ".coverage")


def build_overlay(cand: Path, base: Path, dest: Path, keep: tuple[str, ...] = ()) -> Path:
    """The candidate's tree with the baseline's tests, evals and gate config (P1-04).

    Names in ``keep`` stay the candidate's (the metrics gate keeps its ``tests``).
    """
    # Tool caches are dropped too: a planted .mypy_cache or __pycache__ could fake a clean run.
    shutil.copytree(cand, dest, symlinks=True, ignore=shutil.ignore_patterns(*_NOT_COPIED))
    for name in (n for n in GATE_CONFIG if n not in keep):
        target, origin = dest / name, base / name
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        elif target.exists() or target.is_symlink():
            target.unlink()
        if origin.is_dir() and not origin.is_symlink():
            shutil.copytree(origin, target, symlinks=True)
        elif origin.is_file() and not origin.is_symlink():
            shutil.copy2(origin, target)
    return dest


def new_src_modules(cand: Path, base: Path) -> list[str]:
    """Top-level names the candidate adds under ``src/`` (on PYTHONPATH, so they could shadow
    ``pytest`` or add a ``sitecustomize`` that runs inside every gate)."""

    def names(root: Path) -> set[str]:
        src = root / "src"
        if not src.is_dir():
            return set()
        return {p.name for p in src.iterdir() if p.name != "__pycache__"}

    return sorted(n for n in names(cand) - names(base) if not n.endswith(".egg-info"))


TASK_TEMPLATE = """\
You are Sebastian, improving your own codebase (this workspace is a git worktree of it).

Do exactly this backlog item and nothing else:

{item}

Rules:
- Read AGENTS.md and the code you change first. Write the test first.
- Writes to guarded paths are denied: {guarded}
- You may run only these commands with run_command: {commands}
  Run the checks (ruff check ., ruff format --check ., mypy, pytest -q) and fix failures
  before finishing. Never skip, delete or weaken a test.
- Tick the item in docs/BACKLOG.md and add a line under ## [Unreleased] in CHANGELOG.md.
- Finish with a short summary of what changed and the check results.
"""


# --- the cycle ---------------------------------------------------------------------------


@dataclass(slots=True)
class EvolveConfig:
    """Knobs for one cycle; defaults match the repo's CI."""

    base: str = "HEAD"
    item_id: str | None = None
    checks: list[list[str]] = field(default_factory=default_checks)
    eval_cmd: list[str] | None = field(default_factory=default_eval)
    agent_commands: frozenset[str] = frozenset({"pytest", "ruff", "mypy"})
    budget: Budget = field(
        default_factory=lambda: Budget(max_steps=60, max_tokens=1_500_000, max_seconds=3600)
    )
    push: bool = False
    sandbox: str = "auto"  # gate commands: auto | bwrap (both need bubblewrap) | none
    unsafe_no_sandbox: bool = False  # P1-02: the only way agent-written code runs unsandboxed
    # P1-04: run in the baseline and the candidate; collected tests and coverage must not drop.
    metrics_cmd: list[str] | None = field(default_factory=default_metrics)
    allow_missing_baseline: bool = False  # P1-04: else an unreadable baseline score rejects
    max_resumes: int = (
        1  # budget-stopped agent runs continue this many times, each with a fresh budget
    )
    home: Path = field(default_factory=lambda: Path.home() / ".sebastian")
    gauntlet: GauntletPolicy | None = None  # B34: the inner run must pass VERIFY and CRITIQUE


def evolve_once(
    repo: str | Path,
    llm: LLM,
    config: EvolveConfig | None = None,
    tracer: Tracer | None = None,
    clock: Clock | None = None,
    opener: Callable[[Path, str, BacklogItem], None] | None = None,
) -> EvolveReport:
    """Run one self-improvement cycle on ``repo``. Never merges; returns what happened."""
    cfg = config or EvolveConfig()
    root = Path(_git(Path(repo), "rev-parse", "--show-toplevel").strip())
    base_sha = _git(root, "rev-parse", "--verify", f"{cfg.base}^{{commit}}").strip()
    backlog = _git(root, "show", f"{base_sha}:docs/BACKLOG.md")
    item = next_item(backlog, cfg.item_id)
    report = EvolveReport(item)
    if item is None:
        report.reason = "no unlocked backlog item" + (f" {cfg.item_id}" if cfg.item_id else "")
        return report

    bwrap = resolve_sandbox(cfg.sandbox, unsafe=cfg.unsafe_no_sandbox)
    report.sandbox = "bwrap" if bwrap else "none"
    clock = clock or SystemClock()
    report.branch = _free_branch(root, f"sebastian/evolve-{item.id.lower()}-{int(clock.now())}")
    guarded = load_guarded(root)
    scratch = Path(tempfile.mkdtemp(prefix="sebastian-evolve-"))
    cand, base = scratch / "candidate", scratch / "baseline"
    _git(root, "worktree", "add", "-q", "-b", report.branch, str(cand), base_sha)
    _git(root, "worktree", "add", "-q", "--detach", str(base), base_sha)
    keep_branch = False
    pointer = _git_pointer(cand)  # P1-05: the worktree's .git file, checked after the run
    try:
        approver = ScopedApprover(cand, guarded, cfg.agent_commands)
        # The Gauntlet's critic needs the run's diff, and revert-on-loop needs the originals.
        checkpoints = Checkpoints(scratch / "checkpoints") if cfg.gauntlet else None
        tools = _agent_tools(cand, cfg, bwrap, checkpoints)
        agent = Sebastian(
            llm=llm,
            tools=tools,
            gate=ApprovalGate(approver),
            budget=cfg.budget,
            tracer=tracer or NullTracer(),
            clock=clock,
            plan=True,
            sessions=SessionStore(scratch / "sessions"),
            workspace=str(cand.resolve()),
            checkpoints=checkpoints,
            gauntlet=cfg.gauntlet,
        )
        task = TASK_TEMPLATE.format(
            item=item.text, guarded=guarded.pattern, commands=", ".join(sorted(cfg.agent_commands))
        )
        result = agent.run(task)
        while (
            result.stop_reason is StopReason.BUDGET_EXHAUSTED and report.resumes < cfg.max_resumes
        ):
            report.resumes += 1
            result = agent.resume(result.run_id)
        report.run_stop = result.stop_reason.value

        if _pointer_tampered(report, cand, pointer):
            return report
        _git(cand, "add", "-A")
        report.changed = [
            p for p in _git(cand, "diff", "--cached", "--name-only").splitlines() if p
        ]
        keep_branch = _gate(report, cand, base, guarded, cfg, bwrap)
        if keep_branch:
            _git(
                cand,
                "-c", "user.name=Sebastian",
                "-c", "user.email=sebastian@users.noreply.github.com",
                "commit", "-q", "-m", f"feat: {item.title} ({item.id}, evolve)",
            )  # fmt: skip
            if cfg.push:
                # The remote is the owner's own config (the agent can't reach .git), so a
                # local-path origin is allowed for this one user-initiated push.
                _git(cand, "-c", "protocol.file.allow=user", "push", "-q", "-u", "origin",
                     report.branch)  # fmt: skip
                if opener is not None:
                    opener(cand, report.branch, item)
        else:
            report.patch = _save_patch(cand, cfg.home, report.branch)
        return report
    finally:
        _git(root, "worktree", "remove", "--force", str(cand))
        _git(root, "worktree", "remove", "--force", str(base))
        shutil.rmtree(scratch, ignore_errors=True)
        if not keep_branch:
            _git(root, "branch", "-q", "-D", report.branch)


def _git_pointer(cand: Path) -> tuple[bytes, str]:
    """The candidate worktree's ``.git`` file and its SHA-256 (empty if not a plain file)."""
    dotgit = cand / ".git"
    data = dotgit.read_bytes() if dotgit.is_file() and not dotgit.is_symlink() else b""
    return data, hashlib.sha256(data).hexdigest()


def _agent_tools(
    cand: Path, cfg: EvolveConfig, bwrap: str | None, checkpoints: Checkpoints | None
) -> ToolRegistry:
    """File tools plus ``run_command`` for the cycle's agent, jailed to the candidate."""
    ws = Workspace(cand)
    tools = ToolRegistry(file_tools(ws, checkpoints))
    tools.register(
        command_tool(
            ws,
            CommandPolicy.of(
                cfg.agent_commands,
                timeout_seconds=CHECK_TIMEOUT,
                env=(("PYTHONPATH", "src"),),
                # Same sandbox as the gates: the agent's own commands run its new code too.
                sandbox="bwrap" if bwrap else "none",
                extra_ro=tuple(python_roots()) if bwrap else (),
            ),
        )
    )
    return tools


def _pointer_tampered(report: EvolveReport, cand: Path, pointer: tuple[bytes, str]) -> bool:
    """True (and the gate recorded) if the agent rewrote the worktree's ``.git`` (P1-05).

    Host git never runs in such a worktree; the original pointer is put back only so the
    worktree can be removed.
    """
    if _git_pointer(cand) == pointer:
        return False
    dotgit = cand / ".git"
    if dotgit.is_dir() and not dotgit.is_symlink():
        shutil.rmtree(dotgit)
    else:
        dotgit.unlink(missing_ok=True)
    dotgit.write_bytes(pointer[0])
    report.gates["git_intact"] = False
    report.reason = "the agent changed the worktree's .git pointer; aborted without running git"
    return True


def _free_branch(root: Path, name: str) -> str:
    existing = set(_git(root, "branch", "--format=%(refname:short)").split())
    candidate, n = name, 1
    while candidate in existing:
        n += 1
        candidate = f"{name}-{n}"
    return candidate


def _gate(
    report: EvolveReport,
    cand: Path,
    base: Path,
    guarded: re.Pattern[str],
    cfg: EvolveConfig,
    bwrap: str | None = None,
) -> bool:
    """Run the gates in order, recording each; stop at the first failure.

    P1-04: checks and the candidate's eval run in an *overlay*: the candidate's tree with the
    baseline's tests, evals and gate config, so the candidate can't pass by weakening them. The
    candidate's own tests run as an extra gate when it changed ``tests/``.
    """
    if report.run_stop == StopReason.NEEDS_HELP.value:
        # B34: the inner run could not prove its own work; the outer gates never rescue that.
        report.gates["gauntlet"] = False
        report.reason = "inner run needs help: the Gauntlet could not prove the item done"
        return False
    report.gates["non_empty"] = bool(report.changed)
    if not report.changed:
        report.reason = f"no changes (run stopped: {report.run_stop})"
        return False
    overlay = cand.parent / "overlay"
    steps: tuple[Callable[[], bool], ...] = (
        lambda: _scope_gate(report, cand, guarded),
        lambda: _layout_gate(report, cand, base),
        lambda: _checks_gate(report, build_overlay(cand, base, overlay), cfg, bwrap, "check"),
        lambda: (
            not any(p.startswith("tests/") for p in report.changed)
            or _checks_gate(report, cand, cfg, bwrap, "own")
        ),
        lambda: _metrics_gate(report, cand, base, cfg, bwrap),
        lambda: _eval_gate(report, overlay, base, cfg, bwrap),
    )
    if not all(step() for step in steps):
        return False
    report.accepted = True
    report.reason = "all gates passed" + (" (pushed)" if cfg.push else " (committed locally)")
    return True


def _layout_gate(report: EvolveReport, cand: Path, base: Path) -> bool:
    added = new_src_modules(cand, base)
    report.gates["src_layout"] = not added
    if added:
        report.reason = f"adds top-level modules under src/ (could hijack the gates): {added}"
    return not added


def _checks_gate(
    report: EvolveReport, tree: Path, cfg: EvolveConfig, bwrap: str | None, prefix: str
) -> bool:
    for argv in cfg.checks:
        ok, out = _run(argv, tree, bwrap)
        name = " ".join(Path(a).name if i == 0 else a for i, a in enumerate(argv))
        report.gates[f"{prefix}:{name}"] = ok
        if not ok:
            where = "baseline tests" if prefix == "check" else "candidate's own tests"
            report.reason = f"check failed ({where}): {name}\n{out[-1500:]}"
            return False
    return True


def _metrics_gate(
    report: EvolveReport, cand: Path, base: Path, cfg: EvolveConfig, bwrap: str | None
) -> bool:
    if not cfg.metrics_cmd:
        return True
    # The candidate's tests, but the baseline's config: a coverage `omit` can't hide a drop.
    tree = build_overlay(cand, base, cand.parent / "metrics", keep=("tests",))
    b = parse_metrics(_run(cfg.metrics_cmd, base, bwrap)[1])
    c = parse_metrics(_run(cfg.metrics_cmd, tree, bwrap)[1])
    report.baseline_metrics, report.candidate_metrics = b, c
    problems = []
    for label, old, new in (("collected tests", b[0], c[0]), ("coverage", b[1], c[1])):
        if new is None:
            problems.append(f"{label}: candidate unreadable")
        elif old is None:
            if not cfg.allow_missing_baseline:
                problems.append(f"{label}: baseline unreadable (--allow-missing-baseline)")
        elif new < old:
            problems.append(f"{label} dropped {old} -> {new}")
    report.gates["no_test_or_coverage_drop"] = not problems
    if problems:
        report.reason = "; ".join(problems)
    return not problems


def _eval_gate(
    report: EvolveReport, overlay: Path, base: Path, cfg: EvolveConfig, bwrap: str | None
) -> bool:
    if not cfg.eval_cmd:
        return True
    report.baseline_score = parse_score(_run(cfg.eval_cmd, base, bwrap)[1])
    report.candidate_score = parse_score(_run(cfg.eval_cmd, overlay, bwrap)[1])
    if report.baseline_score is None and not cfg.allow_missing_baseline:
        report.gates["eval_no_regression"] = False
        report.reason = (
            "baseline eval score could not be read; refusing to pass blind "
            "(--allow-missing-baseline to accept)"
        )
        return False
    ok = report.candidate_score is not None and (
        report.baseline_score is None or report.candidate_score >= report.baseline_score
    )
    report.gates["eval_no_regression"] = ok
    if not ok:
        report.reason = "eval score regressed or could not be read"
    return ok


def _scope_gate(report: EvolveReport, cand: Path, guarded: re.Pattern[str]) -> bool:
    """No guarded path in the diff, and no existing test deleted or renamed (P1-03)."""
    hits = [p for p in report.changed if guarded.search(p)]
    report.gates["guarded_paths"] = not hits
    if hits:
        report.reason = f"touches guarded paths: {', '.join(hits)}"
        return False
    gitfiles = [p for p in report.changed if Path(p).name in GIT_CONFIG_FILES]
    report.gates["git_files"] = not gitfiles
    if gitfiles:
        report.reason = f"touches git config files: {', '.join(gitfiles)}"
        return False
    lost = removed_tests(cand)
    report.gates["tests_kept"] = not lost
    if lost:
        report.reason = f"deletes or renames existing tests: {', '.join(lost)}"
        return False
    return True


def removed_tests(cand: Path) -> list[str]:
    """Existing files under ``tests/`` that the staged change deletes or renames (P1-03)."""
    out = _git(cand, "diff", "--cached", "--name-status", "-M", "--", "tests/")
    lost = []
    for line in out.splitlines():
        status, _, paths = line.partition("\t")
        if status[:1] in {"D", "R"}:
            lost.append(paths.split("\t")[0])
    return lost


def _save_patch(cand: Path, home: Path, branch: str) -> Path | None:
    diff = _git(cand, "diff", "--cached")
    if not diff:
        return None
    out = home / "evolve" / (branch.replace("/", "_") + ".patch")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(diff, encoding="utf-8")
    return out


def open_pr(worktree: Path, branch: str, item: BacklogItem) -> None:
    """Open a PR for an accepted branch with the GitHub CLI (never merges)."""
    exe = shutil.which("gh")
    if exe is None:
        raise EvolveError("gh is not installed; the branch was pushed, open the PR manually")
    body = (
        f"Automated by `sebastian evolve` for {item.id}.\n\n{item.text}\n\n"
        "Gates passed: non-empty diff, no guarded paths, no deleted tests, ruff, format, mypy "
        "and pytest against the baseline's tests and config, no drop in test count or "
        "coverage, eval score >= baseline. Review before merging."
    )
    proc = subprocess.run(
        [
            exe,
            "pr",
            "create",
            "--head",
            branch,
            "--title",
            f"feat: {item.title} ({item.id})",
            "--body",
            body,
        ],
        cwd=worktree,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise EvolveError(f"gh pr create failed: {proc.stderr.strip()[:300]}")
