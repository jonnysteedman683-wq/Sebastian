from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from sebastian import Budget, Sebastian, StopReason
from sebastian.approval import AllowApprover, ApprovalGate
from sebastian.clock import FakeClock, SeededIdSource
from sebastian.context import (
    ELIDED_PREFIX,
    ContextPolicy,
    cap_result,
    compact,
    history_chars,
)
from sebastian.llm import ScriptedLLM
from sebastian.tools import ToolRegistry, Workspace, file_tools
from sebastian.trace import MemoryTracer
from sebastian.types import LLMResponse, Message, Role, ToolCall, ToolResult

SMALL = ContextPolicy(max_result_chars=300, max_history_chars=2_000, keep_recent=2)


def turn(i: int, size: int) -> list[Message]:
    call = ToolCall(f"c{i}", "read_file", {"path": f"f{i}"})
    return [
        Message(Role.ASSISTANT, text=f"step {i}", tool_calls=(call,)),
        Message(Role.USER, tool_results=(ToolResult(f"c{i}", "x" * size),)),
    ]


def history(n: int, size: int) -> list[Message]:
    out = [Message(Role.USER, text="the task")]
    for i in range(n):
        out += turn(i, size)
    return out


def test_policy_rejects_tiny_limits() -> None:
    with pytest.raises(ValueError, match="too small"):
        ContextPolicy(max_result_chars=10)
    with pytest.raises(ValueError, match="too small"):
        ContextPolicy(keep_recent=-1)


def test_cap_result_keeps_head_and_tail() -> None:
    text = "HEAD" + "m" * 5_000 + "TAIL"
    capped = cap_result(ToolResult("c1", text, is_error=True), 300)
    assert len(capped.content) <= 300
    assert capped.content.startswith("HEAD")
    assert capped.content.endswith("TAIL")
    assert "chars truncated" in capped.content
    assert capped.call_id == "c1"
    assert capped.is_error


def test_cap_result_leaves_short_results_alone() -> None:
    r = ToolResult("c1", "short")
    assert cap_result(r, 300) is r


def test_compact_noop_under_limit() -> None:
    msgs = history(2, 100)
    out, elided = compact(msgs, SMALL)
    assert out == msgs
    assert elided == 0


def test_compact_elides_oldest_first_and_fits() -> None:
    msgs = history(10, 290)
    assert history_chars(msgs) > SMALL.max_history_chars
    out, elided = compact(msgs, SMALL)
    assert elided > 0
    assert history_chars(out) <= SMALL.max_history_chars
    assert out[0] == msgs[0]  # task untouched
    assert out[2].tool_results[0].content.startswith(ELIDED_PREFIX)  # oldest stubbed
    assert out[-1] == msgs[-1]  # recent turns untouched
    # pairing preserved: same call ids in the same places
    assert [r.call_id for m in out for r in m.tool_results] == [
        r.call_id for m in msgs for r in m.tool_results
    ]
    assert [m.text for m in out] == [m.text for m in msgs]


def test_compact_is_idempotent() -> None:
    once, _ = compact(history(10, 290), SMALL)
    twice, elided = compact(once, SMALL)
    assert twice == once or elided == 0


def test_compact_cannot_touch_protected_turns() -> None:
    # Everything is "recent": nothing may be elided even though it is over budget.
    policy = ContextPolicy(max_result_chars=300, max_history_chars=1_000, keep_recent=50)
    msgs = history(5, 290)
    out, elided = compact(msgs, policy)
    assert elided == 0
    assert out == msgs


def test_agent_compacts_long_runs(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("y" * 10_000)
    calls = [ToolCall(f"c{i}", "read_file", {"path": "big.txt"}) for i in range(12)]
    seen: list[int] = []

    def reading(i: int) -> object:
        def step(history: Sequence[Message]) -> LLMResponse:
            seen.append(history_chars(history))
            return LLMResponse(f"read {i}", (calls[i],))

        return step

    script: list[object] = [reading(i) for i in range(12)]
    script.append(LLMResponse("done"))
    tracer = MemoryTracer()
    agent = Sebastian(
        llm=ScriptedLLM(script),  # type: ignore[arg-type]
        tools=ToolRegistry(file_tools(Workspace(tmp_path))),
        gate=ApprovalGate(AllowApprover()),
        budget=Budget(max_steps=50),
        tracer=tracer,
        clock=FakeClock(),
        ids=SeededIdSource(1),
        context=SMALL,
    )
    result = agent.run("read big.txt repeatedly")
    assert result.stop_reason is StopReason.COMPLETED
    assert max(seen) <= SMALL.max_history_chars
    assert "context_compacted" in tracer.names()
