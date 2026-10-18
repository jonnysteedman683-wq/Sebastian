from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from sebastian import Budget, Sebastian, StopReason
from sebastian.approval import AllowApprover, ApprovalGate, DenyApprover
from sebastian.clock import FakeClock, SeededIdSource
from sebastian.llm import LLMError, ScriptedLLM
from sebastian.memory import MemoryStore
from sebastian.tools import Permission, ToolRegistry, Workspace, file_tools, make_tool
from sebastian.trace import MemoryTracer
from sebastian.types import LLMResponse, Message, ToolCall, Usage


def call(cid: str, name: str, **args: object) -> ToolCall:
    return ToolCall(cid, name, dict(args))


def make_agent(
    tmp_path: Path,
    script: list[object],
    *,
    allow: bool = True,
    budget: Budget | None = None,
    memory: MemoryStore | None = None,
    clock: FakeClock | None = None,
) -> tuple[Sebastian, ScriptedLLM, MemoryTracer]:
    llm = ScriptedLLM(script)  # type: ignore[arg-type]
    tracer = MemoryTracer()
    agent = Sebastian(
        llm=llm,
        tools=ToolRegistry(file_tools(Workspace(tmp_path))),
        gate=ApprovalGate(AllowApprover() if allow else DenyApprover()),
        budget=budget or Budget(),
        memory=memory,
        tracer=tracer,
        clock=clock or FakeClock(),
        ids=SeededIdSource(1),
    )
    return agent, llm, tracer


def test_completes_task_using_tools(tmp_path: Path) -> None:
    agent, llm, tracer = make_agent(
        tmp_path,
        [
            LLMResponse("w", (call("1", "write_file", path="a.txt", content="ok"),), Usage(5, 5)),
            LLMResponse("r", (call("2", "read_file", path="a.txt"),), Usage(5, 5)),
            LLMResponse("done", usage=Usage(1, 1)),
        ],
    )
    result = agent.run("make a.txt")
    assert result.stop_reason is StopReason.COMPLETED
    assert result.answer == "done"
    assert result.steps == 3
    assert result.usage.total == 22
    assert (tmp_path / "a.txt").read_text() == "ok"
    # the model saw the read_file result on its third call
    last_history = llm.calls[2][1]
    assert last_history[-1].tool_results[0].content == "ok"
    assert tracer.names()[0] == "run_start"
    assert tracer.names()[-1] == "run_end"


def test_run_is_deterministic(tmp_path: Path) -> None:
    def run_once(sub: str) -> str:
        d = tmp_path / sub
        d.mkdir()
        agent, _, _ = make_agent(d, [LLMResponse("x")])
        return agent.run("t").run_id

    assert run_once("a") == run_once("b")


def test_empty_task_rejected(tmp_path: Path) -> None:
    agent, _, _ = make_agent(tmp_path, [])
    with pytest.raises(ValueError, match="empty"):
        agent.run("   ")


def test_approval_denied_is_reported_not_executed(tmp_path: Path) -> None:
    agent, llm, tracer = make_agent(
        tmp_path,
        [
            LLMResponse("", (call("1", "write_file", path="x", content="y"),)),
            LLMResponse("could not write"),
        ],
        allow=False,
    )
    result = agent.run("write x")
    assert not (tmp_path / "x").exists()
    res = llm.calls[1][1][-1].tool_results[0]
    assert res.is_error
    assert "denied" in res.content
    assert "approval_denied" in tracer.names()
    assert result.stop_reason is StopReason.COMPLETED


def test_read_tools_do_not_need_approval(tmp_path: Path) -> None:
    (tmp_path / "f").write_text("data")
    agent, llm, _ = make_agent(
        tmp_path,
        [LLMResponse("", (call("1", "read_file", path="f"),)), LLMResponse("ok")],
        allow=False,
    )
    agent.run("read f")
    assert llm.calls[1][1][-1].tool_results[0].content == "data"


def test_unknown_tool_and_bad_args_and_escape(tmp_path: Path) -> None:
    agent, llm, _ = make_agent(
        tmp_path,
        [
            LLMResponse(
                "",
                (
                    call("1", "nope"),
                    call("2", "read_file"),
                    call("3", "read_file", path="../../etc/passwd"),
                ),
            ),
            LLMResponse("done"),
        ],
    )
    agent.run("t")
    results = llm.calls[1][1][-1].tool_results
    assert all(r.is_error for r in results)
    assert "unknown tool" in results[0].content
    assert "missing" in results[1].content
    assert "escapes" in results[2].content


def test_repeated_failed_call_is_blocked(tmp_path: Path) -> None:
    bad = call("1", "read_file", path="missing.txt")
    agent, llm, _ = make_agent(
        tmp_path,
        [LLMResponse("", (bad,)), LLMResponse("", (bad,)), LLMResponse("gave up")],
    )
    agent.run("t")
    second = llm.calls[2][1][-1].tool_results[0]
    assert "already failed" in second.content


def test_tool_crash_does_not_crash_agent(tmp_path: Path) -> None:
    def boom() -> str:
        """Always fails."""
        raise RuntimeError("kaboom")

    agent, llm, _ = make_agent(
        tmp_path, [LLMResponse("", (call("1", "boom"),)), LLMResponse("handled")]
    )
    agent.tools.register(make_tool(boom, permission=Permission.READ))
    assert agent.run("t").answer == "handled"
    assert "RuntimeError: kaboom" in llm.calls[1][1][-1].tool_results[0].content


def test_step_budget_stops_run(tmp_path: Path) -> None:
    loop = LLMResponse("", (call("1", "list_dir"),))

    def again(_: Sequence[Message]) -> LLMResponse:
        return loop

    agent, _, _ = make_agent(tmp_path, [again] * 10, budget=Budget(max_steps=3))
    result = agent.run("loop forever")
    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert result.steps == 3
    assert "step limit" in result.detail


def test_token_and_time_budgets(tmp_path: Path) -> None:
    agent, _, _ = make_agent(
        tmp_path,
        [LLMResponse("", (call("1", "list_dir"),), Usage(60, 50))] * 3,
        budget=Budget(max_tokens=100),
    )
    assert "token limit" in agent.run("t").detail

    clock = FakeClock()

    def slow(_: Sequence[Message]) -> LLMResponse:
        clock.advance(10)
        return LLMResponse("", (call("1", "list_dir"),))

    agent2, _, _ = make_agent(tmp_path, [slow] * 3, budget=Budget(max_seconds=15), clock=clock)
    assert "time limit" in agent2.run("t").detail


def test_llm_error_ends_run_cleanly(tmp_path: Path) -> None:
    agent, _, tracer = make_agent(tmp_path, [])
    result = agent.run("t")
    assert result.stop_reason is StopReason.ERROR
    assert "exhausted" in result.detail
    assert tracer.events[-1]["stop"] == "error"
    assert isinstance(LLMError("x"), Exception)


def test_memory_is_recorded_and_recalled(tmp_path: Path) -> None:
    mem = MemoryStore(tmp_path / "mem.json")
    agent, _, _ = make_agent(tmp_path, [LLMResponse("built the parser")], memory=mem)
    agent.run("build a csv parser")
    assert len(mem) == 1

    agent2, llm2, _ = make_agent(tmp_path, [LLMResponse("ok")], memory=mem)
    agent2.run("fix the csv parser bug")
    system_prompt = llm2.calls[0][0]
    assert "<recalled_memory>" in system_prompt
    assert "built the parser" in system_prompt


def test_tools_advertised_to_model(tmp_path: Path) -> None:
    agent, llm, _ = make_agent(tmp_path, [LLMResponse("ok")])
    agent.run("t")
    assert set(llm.calls[0][2]) == {
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "search_files",
    }


def test_budget_warning_once_per_limit(tmp_path: Path) -> None:
    clock = FakeClock()

    def step(_: Sequence[Message]) -> LLMResponse:
        clock.advance(3)
        return LLMResponse("", (call("1", "list_dir"),), Usage(10, 10))

    # 5 steps of 20 tokens and 3s: steps cross 80% at step 4, tokens (100) at step 4,
    # time (15s) at 12s (step 4); the run then ends on the step limit.
    agent, _, tracer = make_agent(
        tmp_path,
        [step] * 5,
        budget=Budget(max_steps=5, max_tokens=100, max_seconds=15),
        clock=clock,
    )
    result = agent.run("t")
    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    warnings = [e for e in tracer.events if e["event"] == "budget_warning"]
    assert sorted(w["limit"] for w in warnings) == ["seconds", "steps", "tokens"]
    steps_warning = next(w for w in warnings if w["limit"] == "steps")
    assert steps_warning == {
        "run_id": result.run_id,
        "event": "budget_warning",
        "limit": "steps",
        "used": 4,
        "max": 5,
    }
    assert tracer.names().index("budget_warning") < tracer.names().index("run_end")


def test_no_budget_warning_below_threshold(tmp_path: Path) -> None:
    agent, _, tracer = make_agent(tmp_path, [LLMResponse("ok", (), Usage(1, 1))])
    agent.run("t")
    assert "budget_warning" not in tracer.names()


def test_run_end_usage_survives_redaction(tmp_path: Path) -> None:
    agent, _, tracer = make_agent(tmp_path, [LLMResponse("ok", (), Usage(7, 3))])
    agent.run("t")
    assert tracer.events[-1]["usage"] == {"input": 7, "output": 3}


def test_step_and_plan_usage_survive_redaction(tmp_path: Path) -> None:
    agent, _, tracer = make_agent(
        tmp_path,
        [LLMResponse("1. plan", usage=Usage(3, 4)), LLMResponse("done", usage=Usage(5, 6))],
    )
    agent.plan = True
    agent.run("t")
    by_name = {e["event"]: e for e in tracer.events}
    assert by_name["plan"]["usage"] == {"input": 3, "output": 4}
    assert by_name["llm_step"]["usage"] == {"input": 5, "output": 6}
    assert by_name["run_end"]["usage"] == {"input": 8, "output": 10}
    assert "[REDACTED]" not in str(tracer.events)
