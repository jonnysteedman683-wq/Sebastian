"""Shared fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from test_watch import V1, commit, git


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Throwaway git repo shaped like Sebastian (v1)."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, V1, "v1")
    return r


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Print the eval score when the eval suite ran (``pytest -m eval``)."""
    from test_evals import SCORES

    if SCORES:
        terminalreporter.section("sebastian eval")
        for line in SCORES:
            terminalreporter.write_line(line)
