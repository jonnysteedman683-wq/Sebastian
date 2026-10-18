"""Ouroboros: Sebastian improves its own codebase, one backlog item at a time.

One cycle (``sebastian evolve``):

1. Take the first unchecked, unlocked item from ``docs/BACKLOG.md`` (the same rule Jules uses).
2. Create a fresh git worktree on a new branch from the base ref, plus a baseline worktree.
3. Run the agent in the candidate worktree under a *scoped approver*: it may write any file
   except guarded paths (read from ``.github/workflows/guard.yml``, so there is one source of
   truth) and may run only the allowlisted check commands. Everything else is denied.
4. Gates, all deterministic, in order: the diff is non-empty; it touches no guarded path; every
   check command passes; the eval score is at least the baseline's.
5. Only if every gate passes is the change committed. Pushing the branch and opening a PR are
   opt-in. Nothing is ever merged: a human (or a later trust level) does that.

A rejected attempt leaves no branch behind; its diff is saved under
``$SEBASTIAN_HOME/evolve/`` for inspection.
"""

from __future__ import annotations

import os
import re
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sebastian.agent import Sebastian
from sebastian.approval import ApprovalGate
from sebastian.budget import Budget
from sebastian.clock import Clock, SystemClock
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
from sebastian.tools.builtin import SANDBOX_WORKDIR, bwrap_argv, bwrap_usable
from sebastian.trace import NullTracer, Tracer
from sebastian.types import StopReason, ToolCall

# Fallback when guard.yml cannot be read: the guarded set from AGENTS.md.
DEFAULT_GUARDED = (
    r"^(\.github/|src/sebastian/watch/|schemas/repo_change_report\.schema\.json$|"
    r"schemas/README\.md$|docs/watch/|docs/WORK_ORDER\.md$|AGENTS\.md$|CLAUDE\.md$)"
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

    def summary(self) -> str:
        """One readable block for the CLI."""
        if self.item is None:
            return "evolve: no unlocked backlog item left"
        verdict = "ACCEPTED" if self.accepted else "REJECTED"
        lines = [f"{verdict} {self.item.id}: {self.item.title}", f"  reason: {self.reason}"]
        if self.branch:
            lines.append(f"  branch: {self.branch}")
        if self.sandbox:
            lines.append(f"  gate sandbox: {self.sandbox}")
        if self.resumes:
            lines.append(f"  agent resumed: {self.resumes}x after hitting its budget")
        if self.gates:
            gates = ", ".join(f"{k}={'ok' if v else 'FAIL'}" for k, v in self.gates.items())
            lines.append(f"  gates: {gates}")
        if self.baseline_score is not None or self.candidate_score is not None:
            base, cand = _pct(self.baseline_score), _pct(self.candidate_score)
            lines.append(f"  eval: baseline={base} candidate={cand}")
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
            ok = rel is not None and not self.guarded.search(rel)
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


def _git(repo: Path, *args: str) -> str:
    exe = shutil.which("git")
    if exe is None:
        raise EvolveError("git is not installed")
    proc = subprocess.run(
        [exe, "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise EvolveError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def resolve_sandbox(mode: str) -> str | None:
    """Path to a usable bwrap for ``mode`` (auto | bwrap | none); None means unsandboxed."""
    if mode not in {"auto", "bwrap", "none"}:
        raise EvolveError(f"unknown sandbox mode {mode!r}; use auto, bwrap or none")
    if mode == "none":
        return None
    exe = shutil.which("bwrap")
    if exe is not None and bwrap_usable(exe):
        return exe
    if mode == "bwrap":
        raise EvolveError("sandbox 'bwrap' required but bubblewrap is missing or blocked here")
    return None


def python_roots() -> list[str]:
    """Read-only paths the interpreter and installed tools (ruff, mypy, pytest) live under."""
    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix}
    roots.update(site.getsitepackages())
    roots.add(site.getusersitepackages())
    roots.add(sysconfig.get_path("scripts"))  # console scripts: pytest, ruff, mypy
    roots.add(os.path.join(site.getuserbase(), "bin"))  # pip install --user scripts
    roots.add(str(Path(os.path.realpath(sys.executable)).parent))
    return sorted(r for r in roots if r and r != "/")


def _run(
    argv: Sequence[str], cwd: Path, bwrap: str | None = None, timeout: float = CHECK_TIMEOUT
) -> tuple[bool, str]:
    """Run a gate command in ``cwd``; inside bubblewrap when ``bwrap`` is given."""
    cmd = list(argv)
    if bwrap is None:
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(cwd / "src"), env.get("PYTHONPATH")]))
    else:
        exe = cmd[0] if os.path.isabs(cmd[0]) else shutil.which(cmd[0]) or cmd[0]
        inner = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "LC_ALL"}}
        inner |= {"HOME": SANDBOX_WORKDIR, "PYTHONPATH": f"{SANDBOX_WORKDIR}/src"}
        cmd = bwrap_argv(bwrap, cwd, exe, cmd[1:], inner, extra_ro=python_roots())
        env = dict(os.environ)  # bwrap's own env; --clearenv keeps it out of the sandbox
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
    sandbox: str = "auto"  # gate commands: auto (bwrap if usable) | bwrap (required) | none
    max_resumes: int = (
        1  # budget-stopped agent runs continue this many times, each with a fresh budget
    )
    home: Path = field(default_factory=lambda: Path.home() / ".sebastian")


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

    bwrap = resolve_sandbox(cfg.sandbox)
    report.sandbox = "bwrap" if bwrap else "none"
    clock = clock or SystemClock()
    report.branch = _free_branch(root, f"sebastian/evolve-{item.id.lower()}-{int(clock.now())}")
    guarded = load_guarded(root)
    scratch = Path(tempfile.mkdtemp(prefix="sebastian-evolve-"))
    cand, base = scratch / "candidate", scratch / "baseline"
    _git(root, "worktree", "add", "-q", "-b", report.branch, str(cand), base_sha)
    _git(root, "worktree", "add", "-q", "--detach", str(base), base_sha)
    keep_branch = False
    try:
        approver = ScopedApprover(cand, guarded, cfg.agent_commands)
        ws = Workspace(cand)
        tools = ToolRegistry(file_tools(ws))
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
                _git(cand, "push", "-q", "-u", "origin", report.branch)
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
    """Run the gates in order, recording each; stop at the first failure."""
    report.gates["non_empty"] = bool(report.changed)
    if not report.changed:
        report.reason = f"no changes (run stopped: {report.run_stop})"
        return False
    hits = [p for p in report.changed if guarded.search(p)]
    report.gates["guarded_paths"] = not hits
    if hits:
        report.reason = f"touches guarded paths: {', '.join(hits)}"
        return False
    for argv in cfg.checks:
        ok, out = _run(argv, cand, bwrap)
        name = " ".join(Path(a).name if i == 0 else a for i, a in enumerate(argv))
        report.gates[f"check:{name}"] = ok
        if not ok:
            report.reason = f"check failed: {name}\n{out[-1500:]}"
            return False
    if cfg.eval_cmd:
        report.baseline_score = parse_score(_run(cfg.eval_cmd, base, bwrap)[1])
        report.candidate_score = parse_score(_run(cfg.eval_cmd, cand, bwrap)[1])
        ok = report.candidate_score is not None and (
            report.baseline_score is None or report.candidate_score >= report.baseline_score
        )
        report.gates["eval_no_regression"] = ok
        if not ok:
            report.reason = "eval score regressed or could not be read"
            return False
    report.accepted = True
    report.reason = "all gates passed" + (" (pushed)" if cfg.push else " (committed locally)")
    return True


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
        "Gates passed: non-empty diff, no guarded paths, ruff, format, mypy, pytest, "
        "eval score >= baseline. Review before merging."
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
