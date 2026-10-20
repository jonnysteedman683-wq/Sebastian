"""Read-only git access. Only plumbing that reads objects; nothing is checked out or executed."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class GitError(RuntimeError):
    """A git command failed or a ref could not be resolved."""


@dataclass(frozen=True, slots=True)
class Commit:
    """One commit: its full SHA and subject line."""

    sha: str
    subject: str


class Git:
    """Read-only git queries against one repository, through the git executable."""

    def __init__(self, repo: str | Path) -> None:
        exe = shutil.which("git")
        if exe is None:
            raise GitError("git executable not found")
        self._exe = exe
        self.repo = Path(repo)

    def _run(self, *args: str) -> str:
        proc = subprocess.run(
            [self._exe, "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def resolve(self, ref: str) -> str:
        """The full commit SHA for ``ref``; raises ``GitError`` if it is not a commit."""
        sha = self._run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
        if not _SHA.match(sha):
            raise GitError(f"cannot resolve {ref!r}")
        return sha

    def exists(self, sha: str) -> bool:
        """True when ``sha`` resolves to a commit in this repository."""
        try:
            self.resolve(sha)
        except GitError:
            return False
        return True

    def is_ancestor(self, older: str, newer: str) -> bool:
        """True when ``older`` is an ancestor of ``newer`` (or the same commit)."""
        exe = self._exe
        proc = subprocess.run(
            [exe, "-C", str(self.repo), "merge-base", "--is-ancestor", older, newer],
            capture_output=True,
            check=False,
        )
        return proc.returncode == 0

    def show(self, sha: str, path: str) -> str | None:
        """File contents at a commit, or None if the file does not exist there."""
        try:
            return self._run("show", f"{sha}:{path}")
        except GitError:
            return None

    def files(self, sha: str, prefix: str) -> list[str]:
        """Paths of the files under ``prefix`` in commit ``sha``."""
        out = self._run("ls-tree", "-r", "--name-only", sha, "--", prefix)
        return [line for line in out.splitlines() if line]

    def commits(self, from_sha: str | None, to_sha: str, limit: int = 20) -> list[Commit]:
        """Commits after ``from_sha`` up to ``to_sha``, oldest first.

        With no ``from_sha``, the last ``limit`` commits up to ``to_sha``.
        """
        rng = [f"{from_sha}..{to_sha}"] if from_sha else [f"-{limit}", to_sha]
        out = self._run("log", "--reverse", "--format=%H%x1f%s", *rng)
        commits = []
        for line in out.splitlines():
            sha, _, subject = line.partition("\x1f")
            commits.append(Commit(sha, subject))
        return commits

    def diff_text(self, from_sha: str | None, to_sha: str, max_bytes: int = 200_000) -> str:
        """Unified diff for the range, truncated to ``max_bytes`` (for the watcher to read)."""
        args = ["diff", from_sha, to_sha] if from_sha else ["show", "--format=", to_sha]
        out = self._run(*args)
        if len(out.encode()) <= max_bytes:
            return out
        cut = out.encode()[:max_bytes].decode(errors="ignore")
        return cut + f"\n...[diff truncated at {max_bytes} bytes]\n"

    def changed_files(self, from_sha: str | None, to_sha: str) -> list[str]:
        """Paths changed between two commits; every file in ``to_sha`` if ``from_sha`` is None."""
        if from_sha is None:
            return self.files(to_sha, ".")
        out = self._run("diff", "--name-only", from_sha, to_sha)
        return [line for line in out.splitlines() if line]
