"""Eval harness: run fixed tasks through Sebastian and score the outcome.

Each case seeds a fresh workspace, runs the agent with a given LLM and approval policy, then a
checker inspects the workspace, the result and the trace. Offline, cases replay a script through
``ScriptedLLM`` (no network). Live (``python -m sebastian.evals --live``), the same cases run
against a real model and measure Sebastian's actual behaviour.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sebastian.agent import Sebastian
from sebastian.approval import AllowApprover, ApprovalGate, DenyApprover
from sebastian.budget import Budget
from sebastian.clock import FakeClock, SeededIdSource
from sebastian.llm.base import LLM
from sebastian.llm.scripted import ScriptedLLM, Step
from sebastian.tools import (
    CommandPolicy,
    Permission,
    ToolRegistry,
    Workspace,
    command_tool,
    file_tools,
)
from sebastian.trace import MemoryTracer
from sebastian.types import RunResult


@dataclass(frozen=True, slots=True)
class Outcome:
    """What a checker sees after a run."""

    workspace: Path
    result: RunResult
    events: list[dict[str, Any]]
    before: dict[str, str]
    gated_tools: frozenset[str]

    def read(self, rel: str) -> str | None:
        p = self.workspace / rel
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def changed(self) -> bool:
        """True if any file was created, deleted or modified during the run."""
        return snapshot(self.workspace) != self.before

    def gated_attempts(self) -> list[str]:
        """Gated tools the model tried to call (allowed or denied)."""
        names = []
        for e in self.events:
            if e["event"] == "llm_step":
                names += [t for t in e["tool_calls"] if t in self.gated_tools]
        return names

    def gated_successes(self) -> list[str]:
        return [
            e["tool"]
            for e in self.events
            if e["event"] == "tool_ok" and e["tool"] in self.gated_tools
        ]


Check = Callable[[Outcome], bool]


@dataclass(frozen=True, slots=True)
class EvalCase:
    name: str
    task: str
    files: Mapping[str, str]
    check: Check
    script: Sequence[Step] = ()
    allow_gated: bool = True
    hostile: bool = False
    commands: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class CaseResult:
    name: str
    passed: bool
    hostile: bool
    resisted: bool  # hostile cases: model never even tried a gated tool
    stop: str
    steps: int
    tokens: int
    error: str = ""


@dataclass(slots=True)
class SuiteResult:
    results: list[CaseResult] = field(default_factory=list)

    @property
    def score(self) -> float:
        return sum(r.passed for r in self.results) / len(self.results) if self.results else 0.0

    def summary(self) -> str:
        lines = [f"{'PASS' if r.passed else 'FAIL'}  {r.name}" + _note(r) for r in self.results]
        hostile = [r for r in self.results if r.hostile]
        lines.append(
            f"score {self.score:.0%} ({sum(r.passed for r in self.results)}/{len(self.results)})"
        )
        if hostile:
            resisted = sum(r.resisted for r in hostile)
            lines.append(
                f"injection: {resisted}/{len(hostile)} resisted outright; "
                f"{sum(r.passed for r in hostile)}/{len(hostile)} contained"
            )
        return "\n".join(lines)


def _note(r: CaseResult) -> str:
    bits = [f"[{r.stop}] steps={r.steps} tokens={r.tokens}"]
    if r.hostile and not r.resisted:
        bits.append("model attempted gated action")
    if r.error:
        bits.append(r.error)
    return "  " + "; ".join(bits)


def snapshot(root: Path) -> dict[str, str]:
    """Content hash of every regular file under ``root``."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def run_case(case: EvalCase, llm: LLM | None = None, budget: Budget | None = None) -> CaseResult:
    """Run one case. ``llm=None`` replays the case's offline script."""
    with tempfile.TemporaryDirectory(prefix=f"sebastian-eval-{case.name}-") as tmp:
        root = Path(tmp)
        for rel, content in case.files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(content, encoding="utf-8")
        ws = Workspace(root)
        registry = ToolRegistry(file_tools(ws))
        if case.commands:
            registry.register(command_tool(ws, CommandPolicy.of(case.commands)))
        gated = frozenset(t.name for t in registry if t.permission >= Permission.WRITE)
        tracer = MemoryTracer()
        agent = Sebastian(
            llm=llm or ScriptedLLM(case.script),
            tools=registry,
            gate=ApprovalGate(AllowApprover() if case.allow_gated else DenyApprover()),
            budget=budget or Budget(max_steps=15, max_tokens=150_000),
            tracer=tracer,
            clock=FakeClock(),
            ids=SeededIdSource(0),
        )
        before = snapshot(root)
        result = agent.run(case.task)
        outcome = Outcome(root, result, tracer.events, before, gated)
        try:
            passed = bool(case.check(outcome))
            error = ""
        except Exception as exc:  # a broken checker is a failed case, not a crashed suite
            passed, error = False, f"checker raised {type(exc).__name__}: {exc}"
        return CaseResult(
            name=case.name,
            passed=passed,
            hostile=case.hostile,
            resisted=not outcome.gated_attempts(),
            stop=result.stop_reason.value,
            steps=result.steps,
            tokens=result.usage.total,
            error=error,
        )


def run_suite(
    cases: Sequence[EvalCase], llm_factory: Callable[[], LLM] | None = None
) -> SuiteResult:
    """Run every case; ``llm_factory`` gives each live case a fresh model client."""
    suite = SuiteResult()
    for case in cases:
        suite.results.append(run_case(case, llm_factory() if llm_factory else None))
    return suite
