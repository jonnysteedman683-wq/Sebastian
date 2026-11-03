"""B56: ``sebastian diff <run_id>``."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from sebastian.checkpoint import CheckpointError, Checkpoints
from sebastian.cli import main
from sebastian.rundiff import resolve_run, run_diff
from sebastian.tools.workspace import Workspace

RUN = "abcdef123456"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(h))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return h


def _write(store: Checkpoints, ws: Workspace, rel: str, data: bytes | None) -> None:
    """Change one file the way a file tool does, with checkpoints around the write."""
    target = ws.root / rel
    store.before_write(ws, target)
    if data is None:
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    store.after_write(ws, target)


def _run(home: Path, tmp_path: Path, run_id: str = RUN) -> tuple[Checkpoints, Workspace]:
    root = tmp_path / "ws"
    root.mkdir(exist_ok=True)
    (root / "edit.txt").write_text("one\ntwo\nthree\n")
    (root / "gone.txt").write_text("bye\n")
    (root / "same.txt").write_text("kept\n")
    ws = Workspace(root)
    store = Checkpoints(home / "checkpoints")
    store.begin(run_id)
    _write(store, ws, "edit.txt", b"one\n2\nthree\n")
    _write(store, ws, "new/made.txt", b"fresh\n")
    _write(store, ws, "gone.txt", None)
    _write(store, ws, "same.txt", b"changed\n")
    _write(store, ws, "same.txt", b"kept\n")  # changed and changed back: no diff
    return store, ws


def test_diff_shows_edits_creates_and_deletes(home: Path, tmp_path: Path) -> None:
    store, _ = _run(home, tmp_path)
    result = run_diff(store, RUN)
    assert result.run_id == RUN
    assert result.files == 3
    assert result.skipped == []
    assert result.text == (
        "--- a/edit.txt\n+++ b/edit.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+2\n three\n"
        "--- a/gone.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-bye\n"
        "--- /dev/null\n+++ b/new/made.txt\n@@ -0,0 +1 @@\n+fresh\n"
    )


def test_diff_reflects_later_changes_and_missing_newline(home: Path, tmp_path: Path) -> None:
    store, ws = _run(home, tmp_path)
    (ws.root / "edit.txt").write_text("one\ntwo\nthree")  # edited back, minus the last newline
    text = run_diff(store, RUN).text
    assert "+three\n\\ No newline at end of file\n" in text
    assert "-three\n" in text


def test_binary_and_empty_files(home: Path, tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "img.bin").write_bytes(b"\x89PNG\0\0")
    ws = Workspace(root)
    store = Checkpoints(home / "checkpoints")
    store.begin(RUN)
    _write(store, ws, "img.bin", b"\x89PNG\0\1")
    _write(store, ws, "latin.txt", "café".encode("latin-1"))
    _write(store, ws, "empty.txt", b"")
    text = run_diff(store, RUN).text
    assert "Binary files a/img.bin and b/img.bin differ\n" in text
    assert "Binary files /dev/null and b/latin.txt differ\n" in text
    assert "--- /dev/null\n+++ b/empty.txt\n" in text


def test_symlinked_file_is_skipped_not_read(home: Path, tmp_path: Path) -> None:
    store, ws = _run(home, tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET\n")
    (ws.root / "edit.txt").unlink()
    (ws.root / "edit.txt").symlink_to(secret)
    result = run_diff(store, RUN)
    assert "TOP SECRET" not in result.text
    assert [s.split(":", 1)[0] for s in result.skipped] == ["edit.txt"]


def test_prefix_resolution(home: Path, tmp_path: Path) -> None:
    _run(home, tmp_path)
    _run(home, tmp_path, "abcdef999999")
    root = home / "checkpoints"
    assert resolve_run(root, RUN) == RUN
    assert resolve_run(root, "abcdef1") == RUN
    with pytest.raises(CheckpointError, match="matches 2 runs"):
        resolve_run(root, "abcdef")
    with pytest.raises(CheckpointError, match="at least 6"):
        resolve_run(root, "abc")
    with pytest.raises(CheckpointError, match="starting with 'zzzzzz'"):
        resolve_run(root, "zzzzzz")


def test_missing_workspace(home: Path, tmp_path: Path) -> None:
    store, ws = _run(home, tmp_path)
    for p in sorted(ws.root.rglob("*"), reverse=True):
        p.rmdir() if p.is_dir() else p.unlink()
    ws.root.rmdir()
    with pytest.raises(CheckpointError, match="workspace is not available"):
        run_diff(store, RUN)


def test_cli_prints_diff(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run(home, tmp_path)
    assert main(["diff", RUN[:8]]) == 0
    out = capsys.readouterr().out
    assert out.startswith("--- a/edit.txt\n")
    assert "+++ b/new/made.txt" in out


def test_cli_no_changes_and_errors(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, ws = _run(home, tmp_path)
    assert main(["diff", "nosuchrun"]) == 1
    assert "diff: no checkpoint for a run starting with" in capsys.readouterr().err
    store.undo(RUN, ws)
    assert main(["diff", RUN]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"run {RUN} left no changes in place" in captured.err


def test_cli_skipped_file_exits_1(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, ws = _run(home, tmp_path)
    (ws.root / "gone.txt").symlink_to(tmp_path)
    assert main(["diff", RUN]) == 1
    assert "diff: skipped gone.txt:" in capsys.readouterr().err
