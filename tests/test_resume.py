from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from sebastian import Budget, Sebastian, StopReason
from sebastian.approval import AllowApprover, ApprovalGate
from sebastian.checkpoint import Checkpoints
from sebastian.cli import main
from sebastian.clock import FakeClock, SeededIdSource
from sebastian.llm import ScriptedLLM
from sebastian.session import (
    Session,
    SessionError,
    SessionStore,
    message_from_json,
    message_to_json,
)
from sebastian.tools import ToolRegistry, Workspace, file_tools
from sebastian.trace import MemoryTracer
from sebastian.types import LLMResponse, Message, Role, ToolCall, ToolResult


def w(cid: str, path: str, content: str) -> ToolCall:
    return ToolCall(cid, "write_file", {"path": path, "content": content})


def make(tmp_path: Path, script: list[object], seed: int, steps: int = 20) -> Sebastian:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    store = Checkpoints(tmp_path / "ck")
    return Sebastian(
        llm=ScriptedLLM(script),  # type: ignore[arg-type]
        tools=ToolRegistry(file_tools(Workspace(ws), store)),
        gate=ApprovalGate(AllowApprover()),
        budget=Budget(max_steps=steps),
        tracer=MemoryTracer(),
        clock=FakeClock(),
        ids=SeededIdSource(seed),
        checkpoints=store,
        sessions=SessionStore(tmp_path / "sessions"),
        workspace=str(ws.resolve()),
    )


def test_message_round_trip() -> None:
    m = Message(Role.ASSISTANT, "hi", (ToolCall("1", "read_file", {"path": "a"}),), ())
    u = Message(Role.USER, "", (), (ToolResult("1", "x", is_error=True),))
    for msg in (m, u):
        assert message_from_json(json.loads(json.dumps(message_to_json(msg)))) == msg
    with pytest.raises(SessionError, match="invalid stored message"):
        message_from_json({"role": "robot"})
    with pytest.raises(SessionError, match="text must be"):
        message_from_json({"role": "user", "text": 3, "tool_calls": [], "tool_results": []})


def test_budget_stop_then_resume_finishes_the_job(tmp_path: Path) -> None:
    first = make(
        tmp_path,
        [LLMResponse("a", (w("1", "a.txt", "1"),)), LLMResponse("b", (w("2", "b.txt", "2"),))],
        seed=1,
        steps=2,
    )
    r1 = first.run("write a.txt, b.txt and c.txt")
    assert r1.stop_reason is StopReason.BUDGET_EXHAUSTED
    saved = SessionStore(tmp_path / "sessions").load(r1.run_id)
    assert saved.status == "budget_exhausted"
    assert len(saved.messages) == 5  # task + 2 x (assistant, results)

    second = make(
        tmp_path,
        [LLMResponse("c", (w("3", "c.txt", "3"),)), LLMResponse("all three written")],
        seed=2,
    )
    r2 = second.resume(r1.run_id)
    assert r2.stop_reason is StopReason.COMPLETED
    assert r2.run_id != r1.run_id
    assert r2.steps == 2  # fresh budget
    ws = tmp_path / "ws"
    assert [(ws / f).read_text() for f in ("a.txt", "b.txt", "c.txt")] == ["1", "2", "3"]
    sent = second.llm.calls[0][1]  # type: ignore[attr-defined]
    assert sent[0].text == "write a.txt, b.txt and c.txt"
    assert "<resumed>" in sent[-1].text
    assert "step limit" in sent[-1].text
    assert sent[-1].tool_results  # history kept intact
    events = second.tracer.events  # type: ignore[attr-defined]
    assert events[0]["resumed_from"] == r1.run_id
    s2 = SessionStore(tmp_path / "sessions").load(r2.run_id)
    assert (s2.status, s2.resumed_from) == ("completed", r1.run_id)


def test_completed_runs_are_not_resumable(tmp_path: Path) -> None:
    r = make(tmp_path, [LLMResponse("done")], seed=1).run("t")
    with pytest.raises(SessionError, match="already completed"):
        make(tmp_path, [], seed=2).resume(r.run_id)


def test_interrupted_turn_is_closed(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    ws = str((tmp_path / "ws").resolve())
    (tmp_path / "ws").mkdir()
    killed = Session(
        "killed1",
        "t",
        "sys",
        [Message(Role.USER, "t"), Message(Role.ASSISTANT, "", (w("9", "x.txt", "x"),))],
        workspace=ws,
    )
    store.save(killed)
    agent = make(tmp_path, [LLMResponse("ok")], seed=3)
    assert agent.resume("killed1").stop_reason is StopReason.COMPLETED
    sent = agent.llm.calls[0][1]  # type: ignore[attr-defined]
    assert sent[-1].tool_results[0].content.startswith("ERROR: interrupted")
    answered = Session("ans1", "t", "s", [Message(Role.USER, "t"), Message(Role.ASSISTANT, "fin")])
    store.save(answered)
    with pytest.raises(SessionError, match="final answer"):
        make(tmp_path, [], seed=4).resume("ans1")


def test_resume_guards(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    store.save(Session("other1", "t", "s", [Message(Role.USER, "t")], workspace="/elsewhere"))
    with pytest.raises(SessionError, match="used workspace /elsewhere"):
        make(tmp_path, [], seed=1).resume("other1")
    with pytest.raises(SessionError, match="no saved session"):
        make(tmp_path, [], seed=1).resume("missing")
    with pytest.raises(SessionError, match="invalid run id"):
        store.load("../x")
    bare = Sebastian(llm=ScriptedLLM([]), tools=ToolRegistry())
    with pytest.raises(SessionError, match="needs a session store"):
        bare.resume("x")


@pytest.mark.parametrize(
    "payload",
    [
        "nope",
        json.dumps({"version": 9}),
        json.dumps({"version": 1, "run_id": 1}),
        json.dumps(
            {
                "version": 1,
                "run_id": "r",
                "task": "t",
                "system": "s",
                "status": "running",
                "detail": "",
                "workspace": "",
                "resumed_from": "",
                "messages": [],
            }
        ),
    ],
)
def test_corrupt_sessions_rejected(tmp_path: Path, payload: str) -> None:
    (tmp_path / "r.json").write_text(payload)
    with pytest.raises(SessionError):
        SessionStore(tmp_path).load("r")


def test_cli_run_then_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(home))
    monkeypatch.setenv("SEBASTIAN_MAX_STEPS", "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    ws = tmp_path / "ws"
    ws.mkdir()
    step = [LLMResponse("look", (ToolCall("1", "list_dir", {}),))]
    assert (
        main(
            ["run", "list it", "-w", str(ws), "--no-memory"],
            llm_factory=lambda m: ScriptedLLM(step),
        )
        == 1
    )
    run_id = capsys.readouterr().err.split("run_id=")[1].split()[0]
    monkeypatch.setenv("SEBASTIAN_MAX_STEPS", "5")
    done = [LLMResponse("ws is empty")]
    args = ["resume", run_id, "-w", str(ws), "--no-memory"]
    assert main(args, llm_factory=lambda m: ScriptedLLM(done)) == 0
    assert "ws is empty" in capsys.readouterr().out
    bad = ["resume", "nope", "-w", str(ws), "--no-memory"]
    assert main(bad, llm_factory=lambda m: ScriptedLLM(done)) == 1
    assert "resume: no saved session" in capsys.readouterr().err
