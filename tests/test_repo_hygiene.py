"""Repository settings that keep parallel PRs mergeable."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(GIT is None, reason="needs git")


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env_args = [
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "-c",
        "commit.gpgsign=false",
    ]
    return subprocess.run(
        [str(GIT), "-C", str(repo), *env_args, *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_changelog_uses_union_merge() -> None:
    out = _git(ROOT, "check-attr", "merge", "--", "CHANGELOG.md").stdout
    assert out.strip().endswith("merge: union"), out


def test_parallel_changelog_entries_merge_cleanly(tmp_path: Path) -> None:
    """Two branches that each add an Unreleased line merge without a conflict."""
    repo = tmp_path / "r"
    repo.mkdir()
    assert _git(repo, "init", "-q", "-b", "main").returncode == 0
    shutil.copy(ROOT / ".gitattributes", repo / ".gitattributes")
    log = repo / "CHANGELOG.md"
    log.write_text("# Changelog\n\n## [Unreleased]\n- old entry\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    for branch, line in (("a", "- entry A"), ("b", "- entry B")):
        _git(repo, "checkout", "-q", "-b", branch, "main")
        log.write_text(f"# Changelog\n\n## [Unreleased]\n{line}\n- old entry\n")
        _git(repo, "commit", "-q", "-am", branch)
    _git(repo, "checkout", "-q", "a")
    merged = _git(repo, "merge", "--no-edit", "b")
    assert merged.returncode == 0, merged.stdout + merged.stderr
    text = log.read_text()
    assert "- entry A" in text
    assert "- entry B" in text
    assert "<<<<<<<" not in text
    assert text.count("- old entry") == 1
