"""Publish a finished run's changes: push straight to a branch (``--push-main``) or open a PR.

Publishing is done by the harness after the run, never by the agent: the model gets no git
tool and no credentials, so a prompt injection cannot push. Rules:

- The workspace must be the top level of a git repo with a clean tree when the run starts
  (``check_ready``), so only the run's own changes are committed.
- Only a run that ended ``completed`` is published. ``--push-main`` implies ``--gauntlet``
  in the CLI, so "completed" there means the checks and the critic passed.
- A change to ``.gitattributes``, ``.gitmodules`` or ``.github/`` is never published (git
  filters, submodules and CI with secrets); commit those by hand.
- Direct pushes are fast-forward only: no force, no rebase. If the remote moved, the commit
  stays local and the push is reported as failed.
- Host git runs with the same hardening as ``evolve`` (no hooks, no fsmonitor, no system
  config). Nothing is ever merged by this module.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sebastian.evolve import GIT_CONFIG_FILES, GIT_HARDENING
from sebastian.types import RunResult, StopReason

AUTHOR = ("-c", "user.name=Sebastian", "-c", "user.email=sebastian@users.noreply.github.com")
# The remote is the owner's own config (the agent cannot write .git), so a local-path remote
# is allowed for this one user-initiated fetch/push, as in evolve.
REMOTE_OK = ("-c", "protocol.file.allow=always")
BLOCKED_PREFIXES = (".github/",)
MAX_BODY = 4000


class PublishError(Exception):
    """Publishing could not start or did not finish."""


@dataclass(slots=True)
class PublishReport:
    """What publishing did."""

    mode: str
    published: bool = False
    reason: str = ""
    branch: str = ""
    base: str = ""
    commit: str = ""
    changed: list[str] = field(default_factory=list)


def _git(repo: Path, *args: str) -> str:
    exe = shutil.which("git")
    if exe is None:
        raise PublishError("git is not installed")
    proc = subprocess.run(
        [exe, *GIT_HARDENING, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0"),
    )
    if proc.returncode != 0:
        raise PublishError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def check_ready(workspace: Path, mode: str, branch: str) -> None:
    """Refuse to start a publishing run unless the workspace is a clean repo top level.

    For ``main`` mode the checked-out branch must be ``branch``.
    """
    root = workspace.resolve()
    if shutil.which("git") is None:
        raise PublishError("git is not installed")
    try:
        top = Path(_git(root, "rev-parse", "--show-toplevel").strip()).resolve()
    except PublishError as exc:
        raise PublishError(f"{root} is not a git repository") from exc
    if top != root:
        raise PublishError(f"the workspace must be the repository top level ({top})")
    if _git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise PublishError("the working tree has uncommitted changes; commit or stash them first")
    current = _current_branch(root)
    if mode == "main" and current != branch:
        raise PublishError(f"--push-main needs branch {branch!r} checked out (on {current!r})")
    if not _git(root, "remote").split():
        raise PublishError("the repository has no remote to push to")


def _current_branch(root: Path) -> str:
    name = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if name == "HEAD":
        raise PublishError("HEAD is detached; check out a branch first")
    return name


def _subject(task: str) -> str:
    line = " ".join(task.split())
    return line if len(line) <= 60 else line[:57].rstrip() + "..."


def _message(result: RunResult, task: str) -> str:
    return (
        f"sebastian: {_subject(task)}\n\n"
        f"Run {result.run_id} ({result.stop_reason.value}, steps={result.steps}, "
        f"tokens={result.usage.total}). Timeline: sebastian show {result.run_id}\n"
    )


def _stage(root: Path) -> list[str]:
    _git(root, "add", "-A")
    return [p for p in _git(root, "diff", "--cached", "--name-only").splitlines() if p]


def _blocked(changed: list[str]) -> list[str]:
    return [p for p in changed if p in GIT_CONFIG_FILES or p.startswith(BLOCKED_PREFIXES)]


def publish(
    workspace: Path,
    result: RunResult,
    task: str,
    *,
    mode: str,
    branch: str = "main",
    remote: str = "origin",
    opener: Callable[[Path, str, str, str, str], None] | None = None,
) -> PublishReport:
    """Commit the run's changes and push them (``mode="main"``) or open a PR (``"pr"``).

    ``opener(root, head, base, title, body)`` opens the PR; tests pass a fake, the CLI passes
    ``open_pr``. Raises ``PublishError`` if a git step fails after the commit was made.
    """
    if mode not in {"main", "pr"}:
        raise PublishError(f"unknown publish mode {mode!r}")
    report = PublishReport(mode)
    root = workspace.resolve()
    refusal = _prepare(root, result, report, mode=mode, branch=branch, remote=remote)
    if refusal:
        report.reason = refusal
        return report

    _git(root, *AUTHOR, "commit", "-q", "-m", _message(result, task))
    report.commit = _git(root, "rev-parse", "HEAD").strip()
    if mode == "main":
        # Fast-forward only: no --force. A protected branch or a race rejects the push.
        _git(root, *REMOTE_OK, "push", "-q", remote, f"HEAD:refs/heads/{report.base}")
        report.published = True
        report.reason = f"pushed {report.commit[:12]} to {remote}/{report.base}"
        return report

    _git(root, *REMOTE_OK, "push", "-q", "-u", remote, report.branch)
    if opener is not None:
        body = (
            f"Opened by `sebastian run --pr` (run `{result.run_id}`).\n\n"
            f"**Task:** {task}\n\n**Result:** {result.answer}"
        )[:MAX_BODY]
        opener(root, report.branch, report.base, f"sebastian: {_subject(task)}", body)
    report.published = True
    report.reason = f"pushed branch {report.branch}" + (" and opened a PR" if opener else "")
    return report


def _prepare(
    root: Path, result: RunResult, report: PublishReport, *, mode: str, branch: str, remote: str
) -> str:
    """Stage the run's changes and pick the branch. Returns a refusal reason, or ""."""
    if result.stop_reason is not StopReason.COMPLETED:
        return f"run ended {result.stop_reason.value}; nothing published"
    report.changed = _stage(root)
    if not report.changed:
        return "the run changed no files; nothing published"
    blocked = _blocked(report.changed)
    if blocked:
        _git(root, "reset", "-q")
        return "refused: the run changed " + ", ".join(blocked) + "; commit by hand"
    base = _current_branch(root)
    if mode == "pr":
        report.branch = _free_branch(root, f"sebastian/run-{result.run_id[:8]}")
        report.base = base
        _git(root, "switch", "-q", "-c", report.branch)
        return ""
    return _prepare_main(root, report, base=base, branch=branch, remote=remote)


def _prepare_main(root: Path, report: PublishReport, *, base: str, branch: str, remote: str) -> str:
    """Direct push: right branch checked out and not behind the remote."""
    if base != branch:
        _git(root, "reset", "-q")
        return f"refused: on {base!r}, not {branch!r}; nothing published"
    report.branch = report.base = base
    _git(root, *REMOTE_OK, "fetch", "-q", remote, base)
    behind = _git(root, "rev-list", "--count", f"HEAD..{remote}/{base}").strip()
    if behind != "0":
        _git(root, "reset", "-q")
        return f"refused: {remote}/{base} has {behind} new commit(s); pull first"
    return ""


def _free_branch(root: Path, name: str) -> str:
    taken = set(_git(root, "branch", "--format=%(refname:short)").split())
    if name not in taken:
        return name
    n = 2
    while f"{name}-{n}" in taken:
        n += 1
    return f"{name}-{n}"


def open_pr(root: Path, head: str, base: str, title: str, body: str) -> None:
    """Open a PR with the GitHub CLI (never merges)."""
    exe = shutil.which("gh")
    if exe is None:
        raise PublishError("gh is not installed; the branch was pushed, open the PR manually")
    proc = subprocess.run(
        [exe, "pr", "create", "--head", head, "--base", base, "--title", title, "--body", body],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise PublishError(f"gh pr create failed: {proc.stderr.strip()[:300]}")


_SAFE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")


def valid_branch(name: str) -> str:
    """Argparse type for ``--push-main BRANCH``: plain names only, no refspec tricks."""
    if not _SAFE_BRANCH.match(name) or name.startswith(("-", "/")) or ".." in name:
        raise ValueError(name)
    return name
