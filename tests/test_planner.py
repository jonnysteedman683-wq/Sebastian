from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sebastian import Budget, Sebastian, StopReason
from sebastian.approval import AllowApprover, ApprovalGate
from sebastian.clock import FakeClock, SeededIdSource
from sebastian.llm import ScriptedLLM
from sebastian.llm.scripted import Step
from sebastian.prompts import PLAN_INSTRUCTION, build_system_prompt
from sebastian.tools import ToolRegistry, Workspace, file_tools
from sebastian.trace import MemoryTracer
from sebastian.types import LLMResponse, Message, Role, ToolCall, ToolResult, Usage


def agent(
    tmp_path: Path, script: Sequence[Step], **kw: Any
) -> tuple[Sebastian, ScriptedLLM, MemoryTracer]:
    llm = ScriptedLLM(script)
    tracer = MemoryTracer()
    a = Sebastian(
        llm=llm,
        tools=ToolRegistry(file_tools(Workspace(tmp_path))),
        gate=ApprovalGate(AllowApprover()),
        budget=Budget(max_steps=20),
        tracer=tracer,
        clock=FakeClock(),
        ids=SeededIdSource(1),
        **kw,
    )
    return a, llm, tracer


def test_plan_is_tool_free_and_feeds_the_system_prompt(tmp_path: Path) -> None:
    a, llm, tracer = agent(
        tmp_path,
        [
            LLMResponse("1. write a.txt\n2. read it back", usage=Usage(4, 6)),
            LLMResponse("w", (ToolCall("1", "write_file", {"path": "a.txt", "content": "x"}),)),
            LLMResponse("done"),
        ],
        plan=True,
    )
    result = a.run("make a.txt")
    assert result.stop_reason is StopReason.COMPLETED
    assert result.steps == 3  # planning counts against the budget
    assert result.usage.total == 10
    plan_sys, plan_msgs, plan_tools = llm.calls[0]
    assert plan_tools == ()
    assert plan_sys.endswith(PLAN_INSTRUCTION)
    assert [m.text for m in plan_msgs] == ["make a.txt"]
    act_sys = llm.calls[1][0]
    assert "<plan>" in act_sys
    assert "1. write a.txt" in act_sys
    assert llm.calls[1][2]  # tools available after planning
    assert tracer.names()[:2] == ["run_start", "plan"]


def test_no_plan_by_default(tmp_path: Path) -> None:
    a, llm, tracer = agent(tmp_path, [LLMResponse("done")])
    a.run("t")
    assert "<plan>" not in llm.calls[0][0]
    assert "plan" not in tracer.names()


def test_planning_failure_stops_with_error(tmp_path: Path) -> None:
    a, _, _ = agent(tmp_path, [], plan=True)
    result = a.run("t")
    assert result.stop_reason is StopReason.ERROR
    assert result.detail.startswith("planning:")


def test_plan_with_recall_and_blank_plan() -> None:
    from sebastian.memory import Episode

    ep = Episode("old", "completed", "notes", 0.0)
    both = build_system_prompt([ep], "1. go")
    assert both.index("<plan>") < both.index("<recalled_memory>")
    assert "<plan>" not in build_system_prompt([], "   ")


def test_replan_nudge_after_consecutive_errors(tmp_path: Path) -> None:
    bad = [ToolCall(f"c{i}", "read_file", {"path": f"missing{i}"}) for i in range(4)]
    a, llm, tracer = agent(
        tmp_path,
        [LLMResponse("try", (c,)) for c in bad] + [LLMResponse("giving up")],
        replan_after_errors=3,
    )
    a.run("read files")
    assert tracer.names().count("replan_nudge") == 1
    history = llm.calls[-1][1]
    nudged = [m for m in history if m.role is Role.USER and "<replan>" in m.text]
    assert len(nudged) == 1
    assert nudged[0].tool_results  # nudge rides on the tool-results turn


def test_replan_can_be_disabled(tmp_path: Path) -> None:
    bad = [ToolCall(f"c{i}", "read_file", {"path": f"missing{i}"}) for i in range(4)]
    a, _, tracer = agent(
        tmp_path,
        [LLMResponse("try", (c,)) for c in bad] + [LLMResponse("x")],
        replan_after_errors=0,
    )
    a.run("read files")
    assert "replan_nudge" not in tracer.names()


def test_success_resets_error_streak(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("ok")
    miss = ToolCall("m", "read_file", {"path": "nope"})
    script = [
        LLMResponse("a", (ToolCall("1", "read_file", {"path": "n1"}),)),
        LLMResponse("b", (ToolCall("2", "read_file", {"path": "n2"}),)),
        LLMResponse("c", (ToolCall("3", "read_file", {"path": "ok.txt"}),)),
        LLMResponse("d", (miss,)),
        LLMResponse("done"),
    ]
    a, _, tracer = agent(tmp_path, script, replan_after_errors=3)
    a.run("t")
    assert "replan_nudge" not in tracer.names()


anthropic = pytest.importorskip("anthropic")


def test_anthropic_orders_tool_results_before_text_and_omits_empty_tools() -> None:
    from sebastian.llm.anthropic import AnthropicLLM

    resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="plan")],
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    calls: list[dict[str, Any]] = []

    def create(**kw: Any) -> Any:
        calls.append(kw)
        return resp

    llm = AnthropicLLM(
        "claude-sonnet-5-5", client=SimpleNamespace(messages=SimpleNamespace(create=create))
    )
    history = [
        Message(Role.USER, "task"),
        Message(Role.ASSISTANT, "x", (ToolCall("t0", "list_dir", {}),)),
        Message(Role.USER, "<replan>..", tool_results=(ToolResult("t0", "e", is_error=True),)),
    ]
    llm.complete("sys", history, [])
    assert "tools" not in calls[0]
    content = calls[0]["messages"][2]["content"]
    assert [b["type"] for b in content] == ["tool_result", "text"]
    assert calls[0]["messages"][1]["content"][0]["type"] == "text"
