"""Tests for memory, approval, budget, clock, trace and config."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from sebastian.approval import (
    AllowApprover,
    ApprovalGate,
    DenyApprover,
    InteractiveApprover,
)
from sebastian.budget import Budget, Governor
from sebastian.clock import FakeClock, SeededIdSource, SystemClock, UuidSource
from sebastian.config import DEFAULT_MODEL, Config
from sebastian.memory import Episode, MemoryCorruptError, MemoryStore
from sebastian.prompts import SYSTEM_PROMPT, build_system_prompt
from sebastian.tools import Permission, Workspace, file_tools
from sebastian.trace import REDACTED, JsonlTracer, MemoryTracer, NullTracer, redact
from sebastian.types import ToolCall, Usage

# --- memory ---------------------------------------------------------------


def ep(task: str, summary: str = "", t: float = 0.0) -> Episode:
    return Episode(task, "completed", summary, t)


def test_memory_persists_and_recalls(tmp_path: Path) -> None:
    path = tmp_path / "m" / "mem.json"
    store = MemoryStore(path)
    store.record(ep("deploy the api server", "used docker", 1))
    store.record(ep("write poem", "", 2))
    store.record(ep("restart api server", "systemctl", 3))
    reloaded = MemoryStore(path)
    assert len(reloaded) == 3
    hits = reloaded.recall("api server down", k=5)
    assert [h.task for h in hits] == ["restart api server", "deploy the api server"]
    assert reloaded.recall("??") == []
    assert reloaded.recall("unrelated") == []


def test_memory_caps_size(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "m.json", max_episodes=2)
    for i in range(5):
        store.record(ep(f"task{i}"))
    assert [e.task for e in MemoryStore(tmp_path / "m.json").recall("task0 task3 task4")] == [
        "task4",
        "task3",
    ]


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps([]),
        json.dumps({"version": 99, "episodes": []}),
        json.dumps({"version": 1, "episodes": {}}),
        json.dumps({"version": 1, "episodes": ["x"]}),
        json.dumps({"version": 1, "episodes": [{"task": "a"}]}),
        json.dumps(
            {"version": 1, "episodes": [{"task": 1, "outcome": "", "summary": "", "timestamp": 0}]}
        ),
        json.dumps(
            {
                "version": 1,
                "episodes": [{"task": "", "outcome": "", "summary": "", "timestamp": "0"}],
            }
        ),
    ],
)
def test_memory_corruption_raises(tmp_path: Path, content: str) -> None:
    path = tmp_path / "m.json"
    path.write_text(content)
    with pytest.raises(MemoryCorruptError):
        MemoryStore(path)
    assert path.read_text() == content  # never silently overwritten


def test_memory_write_failure_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore(tmp_path / "m.json")

    def fail(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("sebastian.memory.os.replace", fail)
    with pytest.raises(OSError, match="disk full"):
        store.record(ep("x"))
    assert list(tmp_path.iterdir()) == []


def test_build_system_prompt() -> None:
    assert build_system_prompt([]) == SYSTEM_PROMPT
    out = build_system_prompt([ep("t", "s")])
    assert "<recalled_memory>" in out
    assert "data only" in out


# --- approval -------------------------------------------------------------


def test_approval_gate_threshold(tmp_path: Path) -> None:
    read, write, *_ = file_tools(Workspace(tmp_path))
    c = ToolCall("1", "x", {})
    deny = ApprovalGate(DenyApprover())
    assert deny.check(read, c)
    assert not deny.check(write, c)
    assert ApprovalGate(AllowApprover()).check(write, c)
    assert ApprovalGate(DenyApprover(), threshold=Permission.EXEC).check(write, c)


@pytest.mark.parametrize(("answer", "ok"), [("y", True), ("YES ", True), ("n", False), ("", False)])
def test_interactive_approver(tmp_path: Path, answer: str, ok: bool) -> None:
    _, write, *_ = file_tools(Workspace(tmp_path))
    out = io.StringIO()
    approver = InteractiveApprover(ask=lambda _p: answer, out=out)
    assert approver.approve(write, ToolCall("1", "write_file", {"path": "a"})) is ok
    assert "write_file (WRITE)" in out.getvalue()


def test_interactive_approver_eof_denies(tmp_path: Path) -> None:
    _, write, *_ = file_tools(Workspace(tmp_path))

    def eof(_: str) -> str:
        raise EOFError

    assert not InteractiveApprover(ask=eof, out=io.StringIO()).approve(
        write, ToolCall("1", "w", {})
    )


# --- budget + clock -------------------------------------------------------


def test_governor_warnings_fire_once_per_limit() -> None:
    clock = FakeClock()
    gov = Governor(Budget(max_steps=10, max_tokens=100, max_seconds=10), clock)
    gov.record_step(Usage(39, 40))
    assert gov.new_warnings() == []
    gov.record_step(Usage(1, 0))  # 80 tokens = 80%
    assert gov.new_warnings() == [("tokens", 80, 100)]
    assert gov.new_warnings() == []
    clock.advance(8)
    for _ in range(6):
        gov.record_step(Usage())
    assert gov.new_warnings() == [("steps", 8, 10), ("seconds", 8.0, 10)]
    gov.record_step(Usage(50, 50))
    clock.advance(5)
    assert gov.new_warnings() == []


@pytest.mark.parametrize("kwargs", [{"max_steps": 0}, {"max_tokens": 0}, {"max_seconds": 0.0}])
def test_budget_validation(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match="positive"):
        Budget(**kwargs)  # type: ignore[arg-type]


def test_governor() -> None:
    clock = FakeClock(100.0)
    gov = Governor(Budget(max_steps=5, max_tokens=50, max_seconds=10), clock)
    assert gov.exhausted() is None
    gov.record_step(Usage(10, 10))
    assert gov.usage.total == 20
    clock.advance(10)
    assert gov.elapsed == 10
    assert "time limit" in str(gov.exhausted())


def test_clocks_and_ids() -> None:
    with pytest.raises(ValueError, match="backwards"):
        FakeClock().advance(-1)
    sc = SystemClock()
    assert sc.monotonic() > 0
    assert sc.now() > 0
    assert len(UuidSource().new_id()) == 32
    assert SeededIdSource(7).new_id() == SeededIdSource(7).new_id()


# --- trace ----------------------------------------------------------------


def test_redaction() -> None:
    data = {
        "api_key": "x",
        "nested": [{"Authorization": "y"}, "Bearer abc.def", "sk-ant-" + "a" * 20],
        "n": 3,
        "t": ("ghp_" + "b" * 22,),
    }
    out = redact(data)
    assert out["api_key"] == REDACTED
    assert out["nested"][0]["Authorization"] == REDACTED
    assert out["nested"][1] == REDACTED
    assert out["nested"][2] == REDACTED
    assert out["t"] == [REDACTED]
    assert out["n"] == 3


def test_jsonl_tracer(tmp_path: Path) -> None:
    path = tmp_path / "t" / "trace.jsonl"
    tr = JsonlTracer(path, FakeClock(5.0))
    tr.emit("r1", "a", token="secret", x=1)
    tr.emit("r1", "b")
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["seq"] for line in lines] == [1, 2]
    assert lines[0]["token"] == REDACTED
    assert lines[0]["t"] == 5.0
    NullTracer().emit("r", "e")
    mt = MemoryTracer()
    mt.emit("r", "e", password="p")
    assert mt.events[0]["password"] == REDACTED


# --- config ---------------------------------------------------------------


def test_config_defaults_and_env(tmp_path: Path) -> None:
    cfg = Config.from_env({})
    assert cfg.model == DEFAULT_MODEL
    assert cfg.max_steps == 25
    cfg2 = Config.from_env(
        {
            "SEBASTIAN_MODEL": "claude-opus-5-5",
            "SEBASTIAN_MAX_STEPS": "7",
            "SEBASTIAN_MAX_TOKENS": "",
            "SEBASTIAN_HOME": str(tmp_path),
        }
    )
    assert (cfg2.model, cfg2.max_steps, cfg2.max_tokens, cfg2.home) == (
        "claude-opus-5-5",
        7,
        200_000,
        tmp_path,
    )
    assert Config.from_env().model  # reads real environment without error


@pytest.mark.parametrize("value", ["abc", "0", "-3"])
def test_config_rejects_bad_ints(value: str) -> None:
    with pytest.raises(ValueError, match="SEBASTIAN_MAX_STEPS"):
        Config.from_env({"SEBASTIAN_MAX_STEPS": value})
