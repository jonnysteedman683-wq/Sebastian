"""B72: publishing a finished run (``--pr`` / ``--push-main``) on throwaway git repos."""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sebastian import cli
from sebastian.cli import main
from sebastian.publish import (
    PublishError,
    check_ready,
    open_pr,
    publish,
    valid_branch,
)
from sebastian.types import RunResult, StopReason, Usage

GIT = shutil.which("git") or "git"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [GIT, "-c", "protocol.file.allow=always", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=dict(os.environ, GIT_CONFIG_NOSYSTEM="1"),
    ).stdout


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    r = tmp_path / "remote.git"
    subprocess.run([GIT, "init", "-q", "--bare", "-b", "main", str(r)], check=True)
    return r


@pytest.fixture
def ws(tmp_path: Path, remote: Path) -> Path:
    """A clean clone with one commit pushed to ``remote``."""
    w = tmp_path / "ws"
    w.mkdir()
    git(w, "init", "-q", "-b", "main")
    (w / "app.py").write_text("x = 1\n")
    git(w, "add", "-A")
    git(w, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    git(w, "remote", "add", "origin", str(remote))
    git(w, "push", "-q", "-u", "origin", "main")
    return w


def done(stop: StopReason = StopReason.COMPLETED) -> RunResult:
    return RunResult("abcdef1234567890", stop, "Fixed it.", 3, Usage(10, 5))


def test_push_main_commits_and_fast_forwards(ws: Path, remote: Path) -> None:
    (ws / "app.py").write_text("x = 2\n")
    (ws / "new.txt").write_text("hi\n")
    report = publish(ws, done(), "Bump x", mode="main")
    assert report.published, report.reason
    assert sorted(report.changed) == ["app.py", "new.txt"]
    assert git(remote, "rev-parse", "main").strip() == report.commit
    log = git(remote, "log", "-1", "--format=%an|%s", "main").strip()
    assert log == "Sebastian|sebastian: Bump x"
    assert "abcdef1234567890" in git(remote, "log", "-1", "--format=%b", "main")
    assert git(ws, "status", "--porcelain") == ""


@pytest.mark.parametrize(
    "stop", [StopReason.NEEDS_HELP, StopReason.ERROR, StopReason.BUDGET_EXHAUSTED]
)
def test_unfinished_runs_are_never_published(ws: Path, remote: Path, stop: StopReason) -> None:
    before = git(remote, "rev-parse", "main")
    (ws / "app.py").write_text("x = 2\n")
    report = publish(ws, done(stop), "t", mode="main")
    assert not report.published
    assert stop.value in report.reason
    assert git(remote, "rev-parse", "main") == before
    assert git(ws, "log", "--oneline").count("\n") == 1


def test_no_changes_publishes_nothing(ws: Path) -> None:
    report = publish(ws, done(), "t", mode="pr")
    assert not report.published
    assert "no files" in report.reason
    assert git(ws, "branch", "--format=%(refname:short)").split() == ["main"]


@pytest.mark.parametrize("path", [".github/workflows/ci.yml", ".gitattributes", ".gitmodules"])
def test_ci_and_git_config_changes_are_refused(ws: Path, remote: Path, path: str) -> None:
    before = git(remote, "rev-parse", "main")
    (ws / path).parent.mkdir(parents=True, exist_ok=True)
    (ws / path).write_text("pwned\n")
    (ws / "app.py").write_text("x = 2\n")
    report = publish(ws, done(), "t", mode="main")
    assert not report.published
    assert path in report.reason
    assert git(remote, "rev-parse", "main") == before
    assert git(ws, "diff", "--cached", "--name-only") == ""  # index reset, files left alone
    assert (ws / "app.py").read_text() == "x = 2\n"


def test_push_main_refuses_when_remote_moved(ws: Path, remote: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    (other / "b.txt").write_text("b\n")
    git(other, "add", "-A")
    git(other, "-c", "user.name=o", "-c", "user.email=o@o", "commit", "-q", "-m", "other")
    git(other, "push", "-q", "origin", "main")
    moved = git(remote, "rev-parse", "main")
    (ws / "app.py").write_text("x = 2\n")
    report = publish(ws, done(), "t", mode="main")
    assert not report.published
    assert "1 new commit" in report.reason
    assert git(remote, "rev-parse", "main") == moved
    assert git(ws, "log", "--oneline").count("\n") == 1  # nothing committed locally


def test_push_main_refuses_other_branch(ws: Path) -> None:
    git(ws, "switch", "-q", "-c", "dev")
    (ws / "app.py").write_text("x = 2\n")
    report = publish(ws, done(), "t", mode="main", branch="main")
    assert not report.published
    assert "not 'main'" in report.reason


def test_rejected_push_raises_and_keeps_local_commit(ws: Path, remote: Path) -> None:
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho protected >&2\nexit 1\n")
    hook.chmod(0o755)
    (ws / "app.py").write_text("x = 2\n")
    with pytest.raises(PublishError, match="push"):
        publish(ws, done(), "t", mode="main")
    assert git(ws, "log", "-1", "--format=%an").strip() == "Sebastian"


def test_pr_mode_pushes_branch_and_opens_pr(ws: Path, remote: Path) -> None:
    calls: list[tuple[Any, ...]] = []
    main_before = git(remote, "rev-parse", "main")
    (ws / "app.py").write_text("x = 2\n")
    report = publish(ws, done(), "Fix the parser", mode="pr", opener=lambda *a: calls.append(a))
    assert report.published
    assert report.branch == "sebastian/run-abcdef12"
    assert git(remote, "rev-parse", report.branch).strip() == report.commit
    assert git(remote, "rev-parse", "main") == main_before  # main untouched
    root, head, base, title, body = calls[0]
    assert (root, head, base) == (ws.resolve(), report.branch, "main")
    assert title == "sebastian: Fix the parser"
    assert "abcdef1234567890" in body
    assert "Fixed it." in body


def test_pr_branch_name_never_collides(ws: Path) -> None:
    git(ws, "branch", "sebastian/run-abcdef12")
    (ws / "app.py").write_text("x = 2\n")
    report = publish(ws, done(), "t", mode="pr")
    assert report.branch == "sebastian/run-abcdef12-2"
    assert report.reason == "pushed branch sebastian/run-abcdef12-2"


def test_long_task_subject_is_truncated(ws: Path, remote: Path) -> None:
    (ws / "app.py").write_text("x = 2\n")
    publish(ws, done(), "word " * 40, mode="main")
    subject = git(remote, "log", "-1", "--format=%s", "main").strip()
    assert subject.endswith("...")
    assert len(subject) <= len("sebastian: ") + 60


def test_unknown_mode(ws: Path) -> None:
    with pytest.raises(PublishError, match="unknown"):
        publish(ws, done(), "t", mode="merge")


def test_check_ready_accepts_clean_repo(ws: Path) -> None:
    check_ready(ws, "main", "main")
    check_ready(ws, "pr", "main")


def test_check_ready_refusals(ws: Path, tmp_path: Path) -> None:
    (ws / "dirty.txt").write_text("x")
    with pytest.raises(PublishError, match="uncommitted"):
        check_ready(ws, "pr", "main")
    (ws / "dirty.txt").unlink()
    (ws / "sub").mkdir()
    (ws / "sub" / "f").write_text("x")
    git(ws, "add", "-A")
    git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "sub")
    with pytest.raises(PublishError, match="top level"):
        check_ready(ws / "sub", "pr", "main")
    git(ws, "switch", "-q", "-c", "dev")
    with pytest.raises(PublishError, match="'main' checked out"):
        check_ready(ws, "main", "main")
    check_ready(ws, "pr", "main")  # PR mode works from any branch
    git(ws, "switch", "-q", "--detach")
    with pytest.raises(PublishError, match="detached"):
        check_ready(ws, "pr", "main")
    git(ws, "switch", "-q", "main")
    git(ws, "remote", "remove", "origin")
    with pytest.raises(PublishError, match="no remote"):
        check_ready(ws, "pr", "main")
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(PublishError, match="not a git repository"):
        check_ready(plain, "pr", "main")


def test_missing_git(ws: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sebastian.publish.shutil.which", lambda _: None)
    with pytest.raises(PublishError, match="git is not installed"):
        check_ready(ws, "pr", "main")


@pytest.mark.parametrize("bad", ["--force", "a..b", "+main", "main:other", "/main", "-x", "a b"])
def test_valid_branch_rejects_refspec_tricks(bad: str) -> None:
    with pytest.raises(ValueError, match=re.escape(bad)):
        valid_branch(bad)


def test_valid_branch_accepts_plain_names() -> None:
    assert valid_branch("main") == "main"
    assert valid_branch("release/v1.2") == "release/v1.2"


def _fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\necho "$@" > {log}\necho boom >&2\nexit {code}\n')
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def test_open_pr_calls_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = _fake_gh(tmp_path, monkeypatch, 0)
    open_pr(tmp_path, "feat", "main", "title", "body")
    assert log.read_text().split() == [
        "pr", "create", "--head", "feat", "--base", "main", "--title", "title", "--body", "body",
    ]  # fmt: skip


def test_open_pr_failure_and_missing_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_gh(tmp_path, monkeypatch, 1)
    with pytest.raises(PublishError, match="boom"):
        open_pr(tmp_path, "feat", "main", "t", "b")
    monkeypatch.setattr("sebastian.publish.shutil.which", lambda _: None)
    with pytest.raises(PublishError, match="gh is not installed"):
        open_pr(tmp_path, "feat", "main", "t", "b")


# --- CLI wiring ----------------------------------------------------------------------------


class FakeAgent:
    """Stands in for ``Sebastian``: records its kwargs and edits the workspace."""

    seen: dict[str, Any] = {}  # noqa: RUF012 - shared on purpose to inspect after main()
    stop = StopReason.COMPLETED

    def __init__(self, **kwargs: Any) -> None:
        FakeAgent.seen = kwargs

    def run(self, task: str) -> RunResult:
        root = Path(FakeAgent.seen["workspace"])
        (root / "app.py").write_text("x = 3\n")
        return done(FakeAgent.stop)


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(cli, "Sebastian", FakeAgent)
    FakeAgent.stop = StopReason.COMPLETED
    opened: list[tuple[Any, ...]] = []
    monkeypatch.setattr(cli, "open_pr", lambda *a: opened.append(a))
    return opened


def _model(_: str) -> Any:
    return object()


def test_cli_push_main_implies_gauntlet_and_pushes(
    ws: Path, remote: Path, cli_env: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["run", "Bump x", "-w", str(ws), "--push-main"], llm_factory=_model)
    assert code == 0
    assert FakeAgent.seen["gauntlet"] is not None
    assert git(remote, "show", "main:app.py") == "x = 3\n"
    assert "publish: pushed" in capsys.readouterr().err


def test_cli_pr_opens_pr_without_forcing_gauntlet(
    ws: Path, remote: Path, cli_env: list[tuple[Any, ...]]
) -> None:
    assert main(["run", "Bump x", "-w", str(ws), "--pr"], llm_factory=_model) == 0
    assert FakeAgent.seen["gauntlet"] is None
    assert cli_env[0][1] == "sebastian/run-abcdef12"
    assert git(remote, "show", "sebastian/run-abcdef12:app.py") == "x = 3\n"


def test_cli_refuses_dirty_workspace_before_running(
    ws: Path, cli_env: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    FakeAgent.seen = {}
    (ws / "dirty").write_text("x")
    assert main(["run", "t", "-w", str(ws), "--push-main"], llm_factory=_model) == 2
    assert FakeAgent.seen == {}  # the agent was never built
    assert "uncommitted" in capsys.readouterr().err


def test_cli_failed_run_is_not_published(
    ws: Path, remote: Path, cli_env: list[tuple[Any, ...]]
) -> None:
    FakeAgent.stop = StopReason.NEEDS_HELP
    before = git(remote, "rev-parse", "main")
    assert main(["run", "t", "-w", str(ws), "--push-main"], llm_factory=_model) == 1
    assert git(remote, "rev-parse", "main") == before


def test_cli_publish_error_exits_1(
    ws: Path, remote: Path, cli_env: list[tuple[Any, ...]], capsys: pytest.CaptureFixture[str]
) -> None:
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    assert main(["run", "t", "-w", str(ws), "--push-main"], llm_factory=_model) == 1
    err = capsys.readouterr().err
    assert "publish: git" in err
    assert "push" in err


def test_cli_nothing_to_publish_exits_1(
    ws: Path, cli_env: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeAgent, "run", lambda self, task: done())
    assert main(["run", "t", "-w", str(ws), "--pr"], llm_factory=_model) == 1


def test_cli_pr_and_push_main_are_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["run", "t", "--pr", "--push-main"])
    assert "not allowed with" in capsys.readouterr().err


def test_cli_bad_branch_name(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["run", "t", "--push-main=--force"])
    assert "invalid" in capsys.readouterr().err
