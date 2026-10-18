from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from sebastian import Budget, Sebastian
from sebastian.approval import AllowApprover, ApprovalGate
from sebastian.checkpoint import Checkpoints
from sebastian.cli import main
from sebastian.clock import FakeClock, SeededIdSource
from sebastian.evals import ALL, run_case, run_suite
from sebastian.evals.__main__ import main as evals_main
from sebastian.evals.scar import (
    MAX_SEED_FILES,
    ScarError,
    build_check,
    case_from_spec,
    load_scars,
    make_scar,
    run_record,
    write_scar,
)
from sebastian.llm import ScriptedLLM
from sebastian.tools import ToolRegistry, Workspace, file_tools
from sebastian.trace import JsonlTracer
from sebastian.types import LLMResponse, ToolCall


def spec(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "version": 1,
        "name": "demo-scar",
        "task": "write out.txt containing ok",
        "files": {"in.txt": "seed"},
        "expect": {"stop": "completed", "files": {"out.txt": "ok"}},
    }
    base.update(over)
    return base


GOOD_SCRIPT = [
    LLMResponse("w", (ToolCall("1", "write_file", {"path": "out.txt", "content": "ok"}),)),
    LLMResponse("done ok"),
]


# --- checks and specs -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expect", "passes"),
    [
        ({"stop": "completed"}, True),
        ({"stop": "error"}, False),
        ({"max_steps": 2}, True),
        ({"max_steps": 1}, False),
        ({"files": {"out.txt": "ok"}}, True),
        ({"files": {"out.txt": "no"}}, False),
        ({"contains": {"in.txt": "ee"}}, True),
        ({"contains": {"missing.txt": "x"}}, False),
        ({"absent": ["nope.txt"]}, True),
        ({"absent": ["out.txt"]}, False),
        ({"answer_contains": ["done"]}, True),
        ({"answer_contains": ["zzz"]}, False),
        ({"unchanged": True}, False),
        ({"no_gated_success": True}, False),
        ({"no_gated_success": False, "unchanged": False}, True),
    ],
)
def test_expect_conditions(expect: dict[str, Any], passes: bool) -> None:
    case = case_from_spec(spec(expect=expect))
    from dataclasses import replace

    assert run_case(replace(case, script=GOOD_SCRIPT)).passed is passes


@pytest.mark.parametrize(
    ("expect", "error"),
    [
        ({"bogus": 1}, "unknown expect key"),
        ({"stop": "done"}, "expect.stop"),
        ({"max_steps": 0}, "max_steps"),
        ({"max_steps": True}, "max_steps"),
        ({"unchanged": "yes"}, "unchanged"),
        ({"files": {"a": 1}}, "expect.files"),
        ({"contains": []}, "expect.contains"),
        ({"absent": "a"}, "expect.absent"),
        ({"answer_contains": [1]}, "answer_contains"),
    ],
)
def test_bad_expect_rejected(expect: dict[str, Any], error: str) -> None:
    with pytest.raises(ScarError, match=error):
        build_check(expect)


@pytest.mark.parametrize(
    ("over", "error"),
    [
        ({"version": 2}, "unsupported"),
        ({"name": "Bad Name"}, "invalid scar name"),
        ({"task": "  "}, "task"),
        ({"files": {"a": 1}}, "files"),
        ({"files": {"../x": "a"}}, "escapes"),
        ({"files": {"/etc/x": "a"}}, "escapes"),
        ({"expect": []}, "expect must be"),
        ({"allow_gated": "no"}, "allow_gated"),
    ],
)
def test_bad_spec_rejected(over: dict[str, Any], error: str) -> None:
    with pytest.raises(ScarError, match=error):
        case_from_spec(spec(**over))
    with pytest.raises(ScarError, match="unsupported"):
        case_from_spec([])  # type: ignore[arg-type]


def test_scars_are_live_only(tmp_path: Path) -> None:
    write_scar(spec(), tmp_path)
    scars = load_scars(tmp_path)
    assert [c.name for c in scars] == ["scar_demo-scar"]
    offline = run_suite([*ALL, *scars])
    assert offline.skipped == ["scar_demo-scar"]
    assert "skipped 1 live-only case(s): scar_demo-scar" in offline.summary()
    live = run_suite(scars, lambda: ScriptedLLM(GOOD_SCRIPT))
    assert live.score == 1.0


def test_load_scars_errors(tmp_path: Path) -> None:
    assert load_scars(tmp_path / "missing") == []
    (tmp_path / "a.json").write_text("{nope")
    with pytest.raises(ScarError, match=r"a\.json: invalid JSON"):
        load_scars(tmp_path)
    (tmp_path / "a.json").write_text(json.dumps(spec(version=9)))
    with pytest.raises(ScarError, match=r"a\.json: unsupported"):
        load_scars(tmp_path)


def test_write_scar_never_overwrites(tmp_path: Path) -> None:
    path = write_scar(spec(), tmp_path)
    assert json.loads(path.read_text())["name"] == "demo-scar"
    with pytest.raises(ScarError, match="already exists"):
        write_scar(spec(), tmp_path)


# --- making scars from real runs -------------------------------------------------------


def failed_run(tmp_path: Path) -> tuple[str, Path, Path, Checkpoints]:
    """A run that edits a file, creates one, then runs out of steps."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text("x = 1\n")
    (ws / "notes.md").write_text("keep me\n")
    (ws / "blob.bin").write_bytes(b"\xff\xfe\x00")
    home = tmp_path / "home"
    store = Checkpoints(home / "checkpoints")
    trace = home / "traces.jsonl"
    edit = ToolCall("1", "edit_file", {"path": "app.py", "old": "x = 1", "new": "x = 2"})
    new = ToolCall("2", "write_file", {"path": "junk.txt", "content": "junk"})
    miss = ToolCall("3", "read_file", {"path": "missing.txt"})
    agent = Sebastian(
        llm=ScriptedLLM(
            [LLMResponse("a", (edit,)), LLMResponse("b", (new,)), LLMResponse("c", (miss,))]
        ),
        tools=ToolRegistry(file_tools(Workspace(ws), store)),
        gate=ApprovalGate(AllowApprover()),
        budget=Budget(max_steps=3),
        tracer=JsonlTracer(trace, FakeClock()),
        clock=FakeClock(),
        ids=SeededIdSource(3),
        checkpoints=store,
    )
    result = agent.run("Fix the bug in app.py")
    return result.run_id, trace, ws, store


def test_make_scar_rebuilds_pre_run_workspace(tmp_path: Path) -> None:
    rid, trace, ws, store = failed_run(tmp_path)
    s = make_scar(rid, trace, ws, store)
    assert s["task"] == "Fix the bug in app.py"
    assert s["files"] == {"app.py": "x = 1\n", "notes.md": "keep me\n"}  # reverted, no junk/binary
    assert s["origin"]["stop"] == "budget_exhausted"
    assert any("missing.txt" in e for e in s["origin"]["tool_errors"])
    assert s["name"].startswith("fix-the-bug-in-app-py-")
    assert s["expect"] == {"stop": "completed"}
    case_from_spec(s)  # round-trips


def test_make_scar_refuses_clean_runs_and_unknown_runs(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    trace = tmp_path / "t.jsonl"
    tracer = JsonlTracer(trace, FakeClock())
    tracer.emit("ok1", "run_start", task="fine")
    tracer.emit("ok1", "run_end", stop="completed", steps=1)
    with pytest.raises(ScarError, match="completed cleanly"):
        make_scar("ok1", trace, ws)
    with pytest.raises(ScarError, match="not found"):
        run_record(trace, "nope")
    with pytest.raises(ScarError, match="no trace file"):
        run_record(tmp_path / "none.jsonl", "x")


def test_make_scar_without_checkpoints_and_size_limit(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    trace = tmp_path / "t.jsonl"
    tracer = JsonlTracer(trace, FakeClock())
    tracer.emit("r1", "run_start", task="!!!")
    tracer.emit("r1", "run_end", stop="error", steps=1)
    (trace.parent / "garbage").write_text("")
    with trace.open("a") as fh:
        fh.write("not json\n")
    s = make_scar("r1", trace, ws, Checkpoints(tmp_path / "ck"))
    assert s["name"] == "run-r1"
    for i in range(MAX_SEED_FILES + 1):
        (ws / f"f{i}.txt").write_text("x")
    with pytest.raises(ScarError, match="too large"):
        make_scar("r1", trace, ws)


# --- CLI ---------------------------------------------------------------------------------


@pytest.fixture
def home_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return tmp_path / "home"


def test_cli_scar(tmp_path: Path, home_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rid, _, ws, _ = failed_run(tmp_path)
    out = tmp_path / "scars"
    assert main(["scar", rid, "-w", str(ws), "--out", str(out), "--name", "app-bug"]) == 0
    captured = capsys.readouterr()
    path = Path(captured.out.strip())
    assert path.parent == out
    assert "Tighten 'expect'" in captured.err
    assert json.loads(path.read_text())["name"] == "app-bug"
    assert main(["scar", rid, "-w", str(ws), "--out", str(out), "--name", "app-bug"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_evals_cli_loads_scars(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_scar(spec(), tmp_path)
    assert evals_main(["--scars", str(tmp_path)]) == 0
    assert "skipped 1 live-only" in capsys.readouterr().out
    (tmp_path / "bad.json").write_text("{")
    assert evals_main(["--scars", str(tmp_path)]) == 2
    assert "invalid JSON" in capsys.readouterr().err
