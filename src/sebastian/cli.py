"""Command-line entry point: ``sebastian run``, ``demo``, ``traces`` and ``version``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from sebastian import __version__
from sebastian.agent import Sebastian
from sebastian.approval import ApprovalGate, Approver, DenyApprover, InteractiveApprover
from sebastian.budget import Budget
from sebastian.clock import SystemClock
from sebastian.config import Config
from sebastian.llm.base import LLM, RetryingLLM
from sebastian.llm.scripted import ScriptedLLM
from sebastian.memory import MemoryStore
from sebastian.tools import CommandPolicy, ToolRegistry, Workspace, command_tool, file_tools
from sebastian.trace import JsonlTracer, summarize_runs
from sebastian.types import LLMResponse, RunResult, StopReason, ToolCall


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sebastian", description="Sebastian autonomous agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run a task with a real model")
    run.add_argument("task")
    run.add_argument("--workspace", "-w", type=Path, default=Path.cwd())
    run.add_argument("--model", help="override SEBASTIAN_MODEL")
    run.add_argument(
        "--allow-cmd",
        action="append",
        default=[],
        metavar="EXE",
        help="enable run_command for this executable (repeatable)",
    )
    run.add_argument(
        "--unattended",
        action="store_true",
        help="never prompt; every gated action is denied",
    )
    run.add_argument("--no-memory", action="store_true")
    run.add_argument(
        "--plan", action="store_true", help="plan before acting (one extra tool-free model call)"
    )

    demo = sub.add_parser("demo", help="offline scripted run (no API key) to verify install")
    demo.add_argument("--workspace", "-w", type=Path, required=True)

    traces = sub.add_parser("traces", help="list recent runs from traces.jsonl")
    traces.add_argument("-n", "--limit", type=_positive_int, default=10, help="runs to show")

    sub.add_parser("version")
    return p


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def _print_traces(path: Path, limit: int) -> int:
    runs = summarize_runs(path, limit)
    if not runs:
        print("(no runs)")
    for r in runs:
        started = (
            datetime.fromtimestamp(r.started, UTC).isoformat(timespec="seconds")
            if r.started is not None
            else "?"
        )
        print(f"{r.run_id}  {started}  {r.stop}  steps={r.steps}  tokens={r.tokens}")
    return 0


def _demo_llm() -> ScriptedLLM:
    return ScriptedLLM(
        [
            LLMResponse(
                "Writing the note.",
                (ToolCall("c1", "write_file", {"path": "hello.txt", "content": "hello"}),),
            ),
            LLMResponse("Verifying.", (ToolCall("c2", "read_file", {"path": "hello.txt"}),)),
            LLMResponse("Done: hello.txt contains 'hello'."),
        ]
    )


def _print_result(result: RunResult) -> int:
    print(result.answer or result.detail)
    print(
        f"[{result.stop_reason.value}] steps={result.steps} tokens={result.usage.total} "
        f"run_id={result.run_id}",
        file=sys.stderr,
    )
    return 0 if result.stop_reason is StopReason.COMPLETED else 1


def main(
    argv: Sequence[str] | None = None,
    llm_factory: Callable[[str], LLM] | None = None,
) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "version":
        print(__version__)
        return 0

    cfg = Config.from_env()
    if args.cmd == "traces":
        return _print_traces(cfg.home / "traces.jsonl", args.limit)

    clock = SystemClock()
    ws = Workspace(args.workspace)
    registry = ToolRegistry(file_tools(ws))

    if args.cmd == "demo":
        from sebastian.approval import AllowApprover  # noqa: PLC0415

        agent = Sebastian(llm=_demo_llm(), tools=registry, gate=ApprovalGate(AllowApprover()))
        return _print_result(agent.run("Write hello.txt containing 'hello' and verify it."))

    if args.allow_cmd:
        registry.register(command_tool(ws, CommandPolicy.of(args.allow_cmd)))

    approver: Approver
    if args.unattended or not sys.stdin.isatty():
        approver = DenyApprover()
    else:
        approver = InteractiveApprover()

    model = args.model or cfg.model
    if llm_factory is None:
        from sebastian.llm.anthropic import AnthropicLLM  # noqa: PLC0415

        llm: LLM = RetryingLLM(AnthropicLLM(model))
    else:
        llm = llm_factory(model)

    agent = Sebastian(
        llm=llm,
        tools=registry,
        gate=ApprovalGate(approver),
        budget=Budget(cfg.max_steps, cfg.max_tokens, float(cfg.max_seconds)),
        memory=None if args.no_memory else MemoryStore(cfg.home / "memory.json"),
        tracer=JsonlTracer(cfg.home / "traces.jsonl", clock),
        clock=clock,
        plan=args.plan,
    )
    return _print_result(agent.run(args.task))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
