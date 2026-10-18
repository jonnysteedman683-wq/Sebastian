from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from sebastian import Sebastian
from sebastian.approval import AllowApprover, ApprovalGate
from sebastian.checkpoint import CheckpointError, Checkpoints
from sebastian.cli import main
from sebastian.clock import FakeClock, SeededIdSource
from sebastian.llm import ScriptedLLM
from sebastian.tools import ToolRegistry, Workspace, file_tools
from sebastian.types import LLMResponse, ToolCall


def w(cid: str, path: str, content: str) -> ToolCall:
    return ToolCall(cid, "write_file", {"path": path, "content": content})


def e(cid: str, path: str, old: str, new: str) -> ToolCall:
    return ToolCall(cid, "edit_file", {"path": path, "old": old, "new": new})


def run_agent(ws_dir: Path, store: Checkpoints, calls: list[ToolCall]) -> str:
    ws = Workspace(ws_dir)
    script = [LLMResponse("go", (c,)) for c in calls] + [LLMResponse("done")]
    agent = Sebastian(
        llm=ScriptedLLM(script),
        tools=ToolRegistry(file_tools(ws, store)),
        gate=ApprovalGate(AllowApprover()),
        clock=FakeClock(),
        ids=SeededIdSource(7),
        checkpoints=store,
    )
    return agent.run("change files").run_id


@pytest.fixture
def setup(tmp_path: Path) -> tuple[Path, Checkpoints]:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "keep.txt").write_text("original")
    (ws / "edit.py").write_text("x = 1\n")
    return ws, Checkpoints(tmp_path / "home" / "checkpoints")


def test_undo_restores_edits_and_deletes_new_files(setup: tuple[Path, Checkpoints]) -> None:
    ws, store = setup
    rid = run_agent(
        ws,
        store,
        [
            w("1", "keep.txt", "changed"),
            w("2", "keep.txt", "changed twice"),
            e("3", "edit.py", "x = 1", "x = 2"),
            w("4", "sub/new.txt", "brand new"),
        ],
    )
    assert store.files(rid) == ["edit.py", "keep.txt", "sub/new.txt"]
    assert store.workspace_of(rid) == ws.resolve()
    report = store.undo(rid, Workspace(ws))
    assert report.restored == ["edit.py", "keep.txt"]
    assert report.deleted == ["sub/new.txt"]
    assert report.skipped == []
    assert (ws / "keep.txt").read_text() == "original"
    assert (ws / "edit.py").read_text() == "x = 1\n"
    assert not (ws / "sub/new.txt").exists()


def test_undo_skips_files_changed_after_the_run(setup: tuple[Path, Checkpoints]) -> None:
    ws, store = setup
    rid = run_agent(ws, store, [w("1", "keep.txt", "by agent")])
    (ws / "keep.txt").write_text("by human later")
    report = store.undo(rid, Workspace(ws))
    assert report.skipped == ["keep.txt"]
    assert (ws / "keep.txt").read_text() == "by human later"
    forced = store.undo(rid, Workspace(ws), force=True)
    assert forced.restored == ["keep.txt"]
    assert (ws / "keep.txt").read_text() == "original"


def test_no_checkpoint_without_writes(setup: tuple[Path, Checkpoints]) -> None:
    ws, store = setup
    rid = run_agent(ws, store, [ToolCall("1", "read_file", {"path": "keep.txt"})])
    with pytest.raises(CheckpointError, match="no checkpoint"):
        store.files(rid)


def test_tools_without_store_and_store_without_run(tmp_path: Path) -> None:
    store = Checkpoints(tmp_path / "c")
    ws = Workspace(tmp_path)
    t = {tool.name: tool for tool in file_tools(ws, store)}
    t["write_file"].invoke({"path": "a.txt", "content": "x"})  # no begin(): not recorded
    assert not (tmp_path / "c").exists()


def test_undo_wrong_workspace_and_bad_ids(setup: tuple[Path, Checkpoints], tmp_path: Path) -> None:
    ws, store = setup
    rid = run_agent(ws, store, [w("1", "keep.txt", "x")])
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(CheckpointError, match="not"):
        store.undo(rid, Workspace(other))
    with pytest.raises(CheckpointError, match="invalid run id"):
        store.files("../etc")
    with pytest.raises(CheckpointError, match="invalid run id"):
        store.begin("bad/id")


@pytest.mark.parametrize(
    "manifest",
    [
        "not json",
        json.dumps({"version": 99}),
        json.dumps({"version": 1, "workspace": "/w", "files": []}),
        json.dumps({"version": 1, "workspace": "/w", "files": {"a": {"before": "../../x"}}}),
    ],
)
def test_corrupt_manifests_rejected(tmp_path: Path, manifest: str) -> None:
    store = Checkpoints(tmp_path)
    (tmp_path / "r1").mkdir()
    (tmp_path / "r1" / "manifest.json").write_text(manifest)
    with pytest.raises(CheckpointError):
        store.files("r1")


def test_tampered_blob_is_detected(setup: tuple[Path, Checkpoints]) -> None:
    ws, store = setup
    rid = run_agent(ws, store, [w("1", "keep.txt", "x")])
    for blob in (store.root / rid / "blobs").iterdir():
        blob.write_text("tampered")
    with pytest.raises(CheckpointError, match="corrupt"):
        store.undo(rid, Workspace(ws))


# --- CLI ------------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(h))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return h


def test_cli_run_then_undo(
    setup: tuple[Path, Checkpoints], home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws, _ = setup
    store = Checkpoints(home / "checkpoints")
    rid = run_agent(ws, store, [w("1", "keep.txt", "agent"), w("2", "n.txt", "new")])
    assert main(["undo", rid, "-w", str(ws), "--list"]) == 0
    assert capsys.readouterr().out.split() == ["keep.txt", "n.txt"]
    assert main(["undo", rid, "-w", str(ws)]) == 0
    out = capsys.readouterr().out
    assert "restored  keep.txt" in out
    assert "deleted   n.txt" in out
    assert (ws / "keep.txt").read_text() == "original"


def test_cli_undo_reports_skips_and_errors(
    setup: tuple[Path, Checkpoints], home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws, _ = setup
    rid = run_agent(ws, Checkpoints(home / "checkpoints"), [w("1", "keep.txt", "agent")])
    (ws / "keep.txt").write_text("human")
    assert main(["undo", rid, "-w", str(ws)]) == 1
    assert "skipped   keep.txt" in capsys.readouterr().out
    assert main(["undo", "nope", "-w", str(ws)]) == 1
    assert "no checkpoint" in capsys.readouterr().err


def test_cli_run_records_checkpoints(tmp_path: Path, home: Path) -> None:
    ws = tmp_path / "w2"
    ws.mkdir()
    script = [LLMResponse("", (w("1", "a.txt", "x"),)), LLMResponse("done")]
    # non-TTY -> writes are denied, so nothing is changed and nothing is checkpointed
    assert (
        main(["run", "t", "-w", str(ws), "--no-memory"], llm_factory=lambda m: ScriptedLLM(script))
        == 0
    )
    assert not (home / "checkpoints").exists()
