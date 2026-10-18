"""Read-only git access. Only plumbing that reads objects; nothing is checked out or executed."""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class GitError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    subject: str


class Git:
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
        sha = self._run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()
        if not _SHA.match(sha):
            raise GitError(f"cannot resolve {ref!r}")
        return sha

    def exists(self, sha: str) -> bool:
        try:
            self.resolve(sha)
        except GitError:
            return False
        return True

    def is_ancestor(self, older: str, newer: str) -> bool:
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
        out = self._run("ls-tree", "-r", "--name-only", sha, "--", prefix)
        return [line for line in out.splitlines() if line]

    def commits(self, from_sha: str | None, to_sha: str, limit: int = 20) -> list[Commit]:
        rng = [f"{from_sha}..{to_sha}"] if from_sha else [f"-{limit}", to_sha]
        out = self._run("log", "--reverse", "--format=%H%x1f%s", *rng)
        commits = []
        for line in out.splitlines():
            sha, _, subject = line.partition("\x1f")
            commits.append(Commit(sha, subject))
        return commits

    def changed_files(self, from_sha: str | None, to_sha: str) -> list[str]:
        if from_sha is None:
            return self.files(to_sha, ".")
        out = self._run("diff", "--name-only", from_sha, to_sha)
        return [line for line in out.splitlines() if line]
