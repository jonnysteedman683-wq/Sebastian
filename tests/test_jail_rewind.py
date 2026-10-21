"""P1-09: checkpoint rewind and the critic diff never leave the workspace through a symlink."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sebastian.checkpoint import CheckpointError, Checkpoints
from sebastian.gauntlet import workspace_diff
from sebastian.tools import Workspace, WorkspaceError, file_tools


def setup(tmp_path: Path) -> tuple[Path, Path, Checkpoints]:
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    (ws / "pkg").mkdir(parents=True)
    outside.mkdir()
    (ws / "pkg" / "mod.py").write_text("original\n")
    store = Checkpoints(tmp_path / "ck")
    store.begin("run-1")
    write = {t.name: t for t in file_tools(Workspace(ws), store)}["write_file"]
    write.invoke({"path": "pkg/mod.py", "content": "changed\n"})
    write.invoke({"path": "pkg/new.py", "content": "created\n"})
    return ws, outside, store


def swap_for_symlink(ws: Path, outside: Path) -> None:
    """A command replaced the tracked directory with a link to somewhere else."""
    (outside / "mod.py").write_text("victim\n")
    (outside / "new.py").write_text("victim new\n")
    shutil.rmtree(ws / "pkg")
    (ws / "pkg").symlink_to(outside, target_is_directory=True)


def test_resolve_strict_refuses_symlinks(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "real")  # inside the jail, but still a link
    (tmp_path / "flink").symlink_to(tmp_path / "real" / "f")
    ws = Workspace(tmp_path)
    assert ws.resolve_strict("real/f") == tmp_path / "real" / "f"
    assert ws.resolve_strict("./real/f") == tmp_path / "real" / "f"
    assert ws.resolve_strict("real/missing") == tmp_path / "real" / "missing"
    for bad in ("link/f", "flink", "../x", "real/../../x"):
        with pytest.raises(WorkspaceError):
            ws.resolve_strict(bad)


def test_rewind_refuses_a_symlinked_directory(tmp_path: Path) -> None:
    ws, outside, store = setup(tmp_path)
    swap_for_symlink(ws, outside)
    with pytest.raises(CheckpointError, match="symlink"):
        store.rewind()
    assert (outside / "mod.py").read_text() == "victim\n"  # nothing written outside
    assert (outside / "new.py").exists()  # nothing deleted outside


def test_rewind_still_works_without_symlinks(tmp_path: Path) -> None:
    ws, _, store = setup(tmp_path)
    report = store.rewind()
    assert (ws / "pkg" / "mod.py").read_text() == "original\n"
    assert not (ws / "pkg" / "new.py").exists()
    assert report.restored == ["pkg/mod.py"]
    assert report.deleted == ["pkg/new.py"]


def test_diff_skips_files_behind_a_symlink(tmp_path: Path) -> None:
    ws, outside, store = setup(tmp_path)
    originals = store.originals("run-1")
    swap_for_symlink(ws, outside)
    diff = workspace_diff(originals, ws)
    assert "victim" not in diff  # never read from outside the workspace
    assert "skipped pkg/mod.py" in diff
    assert "skipped pkg/new.py" in diff
