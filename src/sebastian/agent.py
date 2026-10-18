"""Sebastian's core loop: think -> act (gated tools) -> observe, until done or out of budget."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from sebastian.approval import ApprovalGate, DenyApprover
from sebastian.budget import Budget, Governor
from sebastian.checkpoint import Checkpoints
from sebastian.clock import Clock, IdSource, SystemClock, UuidSource
from sebastian.context import ContextPolicy, cap_result, compact
from sebastian.llm.base import LLM, LLMError
from sebastian.memory import Episode, MemoryStore
from sebastian.prompts import PLAN_INSTRUCTION, REPLAN_NUDGE, SYSTEM_PROMPT, build_system_prompt
from sebastian.session import Session, SessionError, SessionStore
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
    Usage,
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


def _usage(u: Usage) -> dict[str, int]:
    """Token counts for traces. The key avoids "token", which redaction treats as a secret."""
    return {"input": u.input_tokens, "output": u.output_tokens}


RESUME_NOTE = (
    "<resumed>This run stopped early ({reason}) and has been resumed with a fresh budget. "
    "Continue from where you left off; do not redo finished work.</resumed>"
)


def _close_open_turn(messages: list[Message]) -> list[Message]:
    """Copy of ``messages`` that ends on a user turn, so the model can continue.

    A run killed mid-turn may end on assistant tool calls with no results; those calls are
    answered with an "interrupted" error. A final assistant turn without tool calls means the
    run was answered, which ``resume`` refuses earlier.
    """
    out = list(messages)
    last = out[-1]
    if last.role is Role.ASSISTANT:
        if not last.tool_calls:
            raise SessionError("the saved conversation already ends with a final answer")
        out.append(
            Message(
                Role.USER,
                tool_results=tuple(
                    ToolResult(c.id, "ERROR: interrupted before this call ran", is_error=True)
                    for c in last.tool_calls
                ),
            )
        )
    return out


def _call_key(call: ToolCall) -> str:
    return call.name + "\x00" + json.dumps(call.arguments, sort_keys=True, default=str)


@dataclass
class Sebastian:
    """The agent: runs one task to completion inside its budget, approval gate and tools."""

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
    checkpoints: Checkpoints | None = None  # shared with the file tools, enables ``undo``
    sessions: SessionStore | None = None  # saves each step's conversation, enables ``resume``
    workspace: str = ""  # recorded in sessions so a resume targets the same directory

    def run(self, task: str) -> RunResult:
        """Run ``task`` and return how it ended. Always terminates (budget-bounded)."""
        if not task.strip():
            raise ValueError("task must not be empty")
        run_id = self._new_run()
        governor = Governor(self.budget, self.clock)
        recalled = self.memory.recall(task, self.recall_k) if self.memory else []
        self.tracer.emit(run_id, "run_start", task=task, recalled=len(recalled))
        result: RunResult | None = None
        plan = ""
        if self.plan:
            plan, result = self._make_plan(run_id, task, governor)
        session = Session(
            run_id, task, build_system_prompt(recalled, plan), workspace=self.workspace
        )
        session.messages = [Message(Role.USER, text=task)]
        return self._drive(session, governor, result)

    def resume(self, run_id: str) -> RunResult:
        """Continue a stopped run from its saved conversation, under a fresh budget.

        The continuation is a new run (new id, own trace and checkpoints) that records
        ``resumed_from``. Completed runs, and runs saved for another workspace, are refused.
        """
        if self.sessions is None:
            raise SessionError("resume needs a session store")
        old = self.sessions.load(run_id)
        if old.status == StopReason.COMPLETED.value:
            raise SessionError(f"run {run_id} already completed; start a new run instead")
        if self.workspace and old.workspace and old.workspace != self.workspace:
            raise SessionError(f"run {run_id} used workspace {old.workspace}, not {self.workspace}")
        messages = _close_open_turn(old.messages)
        why = old.detail or old.status
        messages[-1] = replace(
            messages[-1],
            text=(messages[-1].text + "\n" if messages[-1].text else "")
            + RESUME_NOTE.format(reason=why),
        )
        new_id = self._new_run()
        self.tracer.emit(new_id, "run_start", task=old.task, recalled=0, resumed_from=run_id)
        session = Session(
            new_id, old.task, old.system, messages, workspace=old.workspace, resumed_from=run_id
        )
        return self._drive(session, Governor(self.budget, self.clock), None)

    def _drive(self, session: Session, governor: Governor, result: RunResult | None) -> RunResult:
        """The think -> act -> observe loop shared by ``run`` and ``resume``."""
        run_id, system, messages = session.run_id, session.system, session.messages
        failed: set[str] = set()
        tool_list = list(self.tools)
        error_streak = 0
        escalate = False
        self._save(session, messages, result)
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
                usage=_usage(resp.usage),
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
            self._save(session, messages, None)

        self._save(session, messages, result)
        self._remember(session.task, result)
        return result

    def _save(self, session: Session, messages: list[Message], result: RunResult | None) -> None:
        if self.sessions is None:
            return
        session.messages = messages
        if result is not None:
            session.status, session.detail = result.stop_reason.value, result.detail
        self.sessions.save(session)

    def _new_run(self) -> str:
        run_id = self.ids.new_id()
        if self.checkpoints is not None:
            self.checkpoints.begin(run_id)
        return run_id

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
        self.tracer.emit(run_id, "plan", text=resp.text, usage=_usage(resp.usage))
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
            usage=_usage(governor.usage),
        )
        return RunResult(run_id, reason, answer, governor.steps, governor.usage, detail)

    def _remember(self, task: str, result: RunResult) -> None:
        if self.memory is None:
            return
        summary = result.answer or result.detail
        self.memory.record(
            Episode(task, result.stop_reason.value, summary[:1000], self.clock.now())
        )
