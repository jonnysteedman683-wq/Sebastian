"""Sebastian's core loop: think -> act (gated tools) -> observe, until done or out of budget."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from sebastian.approval import ApprovalGate, DenyApprover
from sebastian.budget import Budget, Governor
from sebastian.clock import Clock, IdSource, SystemClock, UuidSource
from sebastian.context import ContextPolicy, cap_result, compact
from sebastian.llm.base import LLM, LLMError
from sebastian.memory import Episode, MemoryStore
from sebastian.prompts import PLAN_INSTRUCTION, REPLAN_NUDGE, SYSTEM_PROMPT, build_system_prompt
from sebastian.tools.base import Permission, Tool, ToolError, ToolRegistry
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

MAX_PARALLEL_READS = 8


def _invoke(tool: Tool, call: ToolCall) -> tuple[str, str | None]:
    """Run a tool body. Returns ``(output, error)``; never raises, never traces."""
    try:
        return tool.invoke(call.arguments), None
    except (ToolError, WorkspaceError) as exc:
        return "", str(exc)
    except Exception as exc:
        return "", f"{type(exc).__name__}: {exc}"


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
    plan: bool = False
    replan_after_errors: int = 3
    parallel_reads: bool = True
    planner_llm: LLM | None = None  # planning phase; defaults to ``llm``
    escalation_llm: LLM | None = None  # the step right after a replan nudge; defaults to ``llm``

    def run(self, task: str) -> RunResult:
        if not task.strip():
            raise ValueError("task must not be empty")
        run_id = self.ids.new_id()
        governor = Governor(self.budget, self.clock)
        recalled = self.memory.recall(task, self.recall_k) if self.memory else []
        messages: list[Message] = [Message(Role.USER, text=task)]
        failed: set[str] = set()
        tool_list = list(self.tools)
        self.tracer.emit(run_id, "run_start", task=task, recalled=len(recalled))

        result: RunResult | None = None
        plan = ""
        if self.plan:
            plan, result = self._make_plan(run_id, task, governor)
        system = build_system_prompt(recalled, plan)
        error_streak = 0
        escalate = False
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
                model, escalate = self._pick_llm(run_id, escalate, governor.steps + 1), False
                resp = model.complete(system, messages, tool_list)
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
                cap_result(r, self.context.max_result_chars)
                for r in self._execute_all(run_id, resp.tool_calls, failed)
            )
            error_streak = error_streak + len(results) if all(r.is_error for r in results) else 0
            nudge = ""
            if self.replan_after_errors and error_streak >= self.replan_after_errors:
                nudge = REPLAN_NUDGE.format(n=error_streak)
                self.tracer.emit(run_id, "replan_nudge", errors=error_streak)
                error_streak = 0
                escalate = True
            messages.append(Message(Role.USER, text=nudge, tool_results=results))

        self._remember(task, result)
        return result

    def _pick_llm(self, run_id: str, escalate: bool, step: int) -> LLM:
        if escalate and self.escalation_llm is not None:
            self.tracer.emit(run_id, "escalated", step=step)
            return self.escalation_llm
        return self.llm

    def _make_plan(
        self, run_id: str, task: str, governor: Governor
    ) -> tuple[str, RunResult | None]:
        """Planning phase: one tool-free model call. Returns ``(plan, early_result)``."""
        try:
            resp = (self.planner_llm or self.llm).complete(
                SYSTEM_PROMPT + PLAN_INSTRUCTION, [Message(Role.USER, text=task)], []
            )
        except LLMError as exc:
            return "", self._finish(run_id, StopReason.ERROR, "", governor, f"planning: {exc}")
        governor.record_step(resp.usage)
        self.tracer.emit(run_id, "plan", text=resp.text, tokens=resp.usage.total)
        return resp.text, None

    def _warn(self, run_id: str, governor: Governor) -> None:
        for limit, used, maximum in governor.new_warnings():
            self.tracer.emit(run_id, "budget_warning", limit=limit, used=used, max=maximum)

    def _execute_all(
        self, run_id: str, calls: tuple[ToolCall, ...], failed: set[str]
    ) -> tuple[ToolResult, ...]:
        """Run one turn's tool calls; results keep call order.

        Checks (unknown tool, repeat failure, approval) always run sequentially in the main
        thread. When every surviving call is READ-only and there are several, the tool bodies run
        concurrently; anything that writes or executes runs strictly in order. Tracing and the
        failure set are only touched from the main thread.
        """
        prepared = [self._prepare(run_id, call, failed) for call in calls]
        jobs = [(i, p) for i, p in enumerate(prepared) if isinstance(p, Tool)]
        parallel = (
            self.parallel_reads
            and len(jobs) > 1
            and all(t.permission < Permission.WRITE for _, t in jobs)
        )
        outcomes: dict[int, tuple[str, str | None]] = {}
        if parallel:
            with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_READS, len(jobs))) as pool:
                futures = {i: pool.submit(_invoke, t, calls[i]) for i, t in jobs}
                outcomes = {i: f.result() for i, f in futures.items()}
            self.tracer.emit(run_id, "tools_parallel", count=len(jobs))
        else:
            outcomes = {i: _invoke(t, calls[i]) for i, t in jobs}
        results: list[ToolResult] = []
        for i, p in enumerate(prepared):
            if isinstance(p, ToolResult):
                results.append(p)
                continue
            output, error = outcomes[i]
            if error is not None:
                results.append(self._fail(run_id, calls[i], failed, error))
            else:
                self.tracer.emit(run_id, "tool_ok", tool=calls[i].name, chars=len(output))
                results.append(ToolResult(calls[i].id, output))
        return tuple(results)

    def _fail(self, run_id: str, call: ToolCall, failed: set[str], msg: str) -> ToolResult:
        failed.add(_call_key(call))
        self.tracer.emit(run_id, "tool_error", tool=call.name, error=msg)
        return ToolResult(call.id, f"ERROR: {msg}", is_error=True)

    def _prepare(self, run_id: str, call: ToolCall, failed: set[str]) -> Tool | ToolResult:
        """Return the tool to run, or the error result that replaces running it."""
        tool = self.tools.get(call.name)
        if tool is None:
            return self._fail(run_id, call, failed, f"unknown tool {call.name!r}")
        if _call_key(call) in failed:
            return self._fail(
                run_id, call, failed, "identical call already failed this run; change approach"
            )
        if not self.gate.check(tool, call):
            self.tracer.emit(run_id, "approval_denied", tool=call.name, args=call.arguments)
            return self._fail(
                run_id, call, failed, "denied by approval gate; do not retry this action"
            )
        return tool

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
