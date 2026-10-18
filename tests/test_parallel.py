from __future__ import annotations

import threading
from pathlib import Path

from sebastian import Sebastian
from sebastian.approval import AllowApprover, ApprovalGate
from sebastian.clock import FakeClock, SeededIdSource
from sebastian.llm import ScriptedLLM
from sebastian.tools import Permission, ToolError, ToolRegistry, make_tool
from sebastian.trace import MemoryTracer
from sebastian.types import LLMResponse, Role, ToolCall


def registry(barrier: threading.Barrier | None, order: list[str]) -> ToolRegistry:
    def probe(name: str) -> str:
        """Read-only probe that waits for its siblings when a barrier is set."""
        if barrier is not None:
            barrier.wait(timeout=2)
        order.append(name)
        if name == "bad":
            raise ToolError("bad probe")
        return f"probe {name}"

    def note(text: str) -> str:
        """Write-class tool."""
        order.append(f"note:{text}")
        return "noted"

    return ToolRegistry(
        [make_tool(probe, permission=Permission.READ), make_tool(note, permission=Permission.WRITE)]
    )


def run(
    reg: ToolRegistry, calls: tuple[ToolCall, ...], **kw: object
) -> tuple[ScriptedLLM, MemoryTracer]:
    llm = ScriptedLLM([LLMResponse("go", calls), LLMResponse("done")])
    tracer = MemoryTracer()
    Sebastian(
        llm=llm,
        tools=reg,
        gate=ApprovalGate(AllowApprover()),
        tracer=tracer,
        clock=FakeClock(),
        ids=SeededIdSource(1),
        **kw,  # type: ignore[arg-type]
    ).run("t")
    return llm, tracer


def probes(*names: str) -> tuple[ToolCall, ...]:
    return tuple(ToolCall(f"c{i}", "probe", {"name": n}) for i, n in enumerate(names))


def results(llm: ScriptedLLM) -> list[str]:
    turn = next(m for m in llm.calls[-1][1] if m.role is Role.USER and m.tool_results)
    return [r.content for r in turn.tool_results]


def test_read_calls_run_concurrently_and_keep_order(tmp_path: Path) -> None:
    order: list[str] = []
    llm, tracer = run(registry(threading.Barrier(3), order), probes("a", "bad", "c"))
    # A barrier of 3 only releases if all three ran at the same time.
    assert results(llm) == ["probe a", "ERROR: bad probe", "probe c"]
    assert "tools_parallel" in tracer.names()
    assert tracer.names().count("tool_ok") == 2
    assert tracer.names().count("tool_error") == 1


def test_writes_force_sequential_order(tmp_path: Path) -> None:
    order: list[str] = []
    calls = (
        ToolCall("1", "probe", {"name": "a"}),
        ToolCall("2", "note", {"text": "x"}),
        ToolCall("3", "probe", {"name": "b"}),
    )
    llm, tracer = run(registry(None, order), calls)
    assert order == ["a", "note:x", "b"]
    assert "tools_parallel" not in tracer.names()
    assert results(llm) == ["probe a", "noted", "probe b"]


def test_parallel_can_be_disabled(tmp_path: Path) -> None:
    order: list[str] = []
    _, tracer = run(registry(None, order), probes("a", "b"), parallel_reads=False)
    assert order == ["a", "b"]
    assert "tools_parallel" not in tracer.names()


def test_failed_precheck_is_excluded_from_parallel_batch(tmp_path: Path) -> None:
    order: list[str] = []
    calls = (*probes("a", "b"), ToolCall("x", "nope", {}))
    llm, _ = run(registry(threading.Barrier(2), order), calls)
    assert results(llm) == ["probe a", "probe b", "ERROR: unknown tool 'nope'"]
