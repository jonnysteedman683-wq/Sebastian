"""Sebastian's core loop: think -> act (gated tools) -> observe, until done or out of budget."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sebastian.approval import ApprovalGate, DenyApprover
from sebastian.budget import Budget, Governor
from sebastian.clock import Clock, IdSource, SystemClock, UuidSource
from sebastian.context import ContextPolicy, cap_result, compact
from sebastian.llm.base import LLM, LLMError
from sebastian.memory import Episode, MemoryStore
from sebastian.prompts import build_system_prompt
from sebastian.tools.base import ToolError, ToolRegistry
from sebastian.tools.workspace import WorkspaceError
from sebastian.trace import NullTracer, Tracer
from sebastian.types import (
    Message,
    Role,
    RunResult,
    StopReason,
    ToolCall,
    ToolResult,
)


def _call_key(call: ToolCall) -> str:
    return call.name + "\x00" + json.dumps(call.arguments, sort_keys=True, default=str)


@dataclass
class Sebastian:
    llm: LLM
    tools: ToolRegistry
    gate: ApprovalGate = field(default_factory=lambda: ApprovalGate(DenyApprover()))
    budget: Budget = field(default_factory=Budget)
    memory: MemoryStore | None = None
    tracer: Tracer = field(default_factory=NullTracer)
    clock: Clock = field(default_factory=SystemClock)
    ids: IdSource = field(default_factory=UuidSource)
    recall_k: int = 3
    context: ContextPolicy = field(default_factory=ContextPolicy)

    def run(self, task: str) -> RunResult:
        if not task.strip():
            raise ValueError("task must not be empty")
        run_id = self.ids.new_id()
        governor = Governor(self.budget, self.clock)
        recalled = self.memory.recall(task, self.recall_k) if self.memory else []
        system = build_system_prompt(recalled)
        messages: list[Message] = [Message(Role.USER, text=task)]
        failed: set[str] = set()
        tool_list = list(self.tools)
        self.tracer.emit(run_id, "run_start", task=task, recalled=len(recalled))

        result: RunResult | None = None
        while result is None:
            self._warn(run_id, governor)
            reason = governor.exhausted()
            if reason:
                result = self._finish(run_id, StopReason.BUDGET_EXHAUSTED, "", governor, reason)
                break
            messages, elided = compact(messages, self.context)
            if elided:
                self.tracer.emit(run_id, "context_compacted", elided=elided)
            try:
                resp = self.llm.complete(system, messages, tool_list)
            except LLMError as exc:
                result = self._finish(run_id, StopReason.ERROR, "", governor, str(exc))
                break
            governor.record_step(resp.usage)
            self.tracer.emit(
                run_id,
                "llm_step",
                step=governor.steps,
                text=resp.text,
                tool_calls=[c.name for c in resp.tool_calls],
                tokens=resp.usage.total,
            )
            messages.append(Message(Role.ASSISTANT, text=resp.text, tool_calls=resp.tool_calls))
            if not resp.tool_calls:
                self._warn(run_id, governor)
                result = self._finish(run_id, StopReason.COMPLETED, resp.text, governor)
                break
            results = tuple(
                cap_result(self._execute(run_id, call, failed), self.context.max_result_chars)
                for call in resp.tool_calls
            )
            messages.append(Message(Role.USER, tool_results=results))

        self._remember(task, result)
        return result

    def _warn(self, run_id: str, governor: Governor) -> None:
        for limit, used, maximum in governor.new_warnings():
            self.tracer.emit(run_id, "budget_warning", limit=limit, used=used, max=maximum)

    def _execute(self, run_id: str, call: ToolCall, failed: set[str]) -> ToolResult:
        def fail(msg: str) -> ToolResult:
            failed.add(_call_key(call))
            self.tracer.emit(run_id, "tool_error", tool=call.name, error=msg)
            return ToolResult(call.id, f"ERROR: {msg}", is_error=True)

        tool = self.tools.get(call.name)
        if tool is None:
            return fail(f"unknown tool {call.name!r}")
        if _call_key(call) in failed:
            return fail("identical call already failed this run; change approach")
        if not self.gate.check(tool, call):
            self.tracer.emit(run_id, "approval_denied", tool=call.name, args=call.arguments)
            return fail("denied by approval gate; do not retry this action")
        try:
            output = tool.invoke(call.arguments)
        except (ToolError, WorkspaceError) as exc:
            return fail(str(exc))
        except Exception as exc:
            return fail(f"{type(exc).__name__}: {exc}")
        self.tracer.emit(run_id, "tool_ok", tool=call.name, chars=len(output))
        return ToolResult(call.id, output)

    def _finish(
        self,
        run_id: str,
        reason: StopReason,
        answer: str,
        governor: Governor,
        detail: str = "",
    ) -> RunResult:
        self.tracer.emit(
            run_id,
            "run_end",
            stop=reason.value,
            steps=governor.steps,
            detail=detail,
            # "usage", not "tokens": keys containing "token" are redacted as secrets.
            usage={"input": governor.usage.input_tokens, "output": governor.usage.output_tokens},
        )
        return RunResult(run_id, reason, answer, governor.steps, governor.usage, detail)

    def _remember(self, task: str, result: RunResult) -> None:
        if self.memory is None:
            return
        summary = result.answer or result.detail
        self.memory.record(
            Episode(task, result.stop_reason.value, summary[:1000], self.clock.now())
        )
