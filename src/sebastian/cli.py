"""CLI: run, resume, demo, traces, show, undo, scar, evolve, redteam, models, dashboard, version."""

from __future__ import annotations

import argparse
import secrets
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sebastian import __version__, history
from sebastian.agent import Sebastian
from sebastian.approval import ApprovalGate, Approver, DenyApprover, InteractiveApprover
from sebastian.budget import Budget
from sebastian.checkpoint import CheckpointError, Checkpoints
from sebastian.clock import SystemClock
from sebastian.config import Config
from sebastian.gauntlet import GauntletPolicy
from sebastian.llm import make_llm
from sebastian.llm.base import LLM
from sebastian.llm.scripted import ScriptedLLM
from sebastian.memory import MemoryStore
from sebastian.pricing import Price, PricingError, load_prices, priced, require_prices
from sebastian.session import SessionError, SessionStore
from sebastian.tools import (
    CommandPolicy,
    ToolError,
    ToolRegistry,
    Workspace,
    command_tool,
    file_tools,
)
from sebastian.tools.builtin import python_roots, resolve_command_sandbox
from sebastian.trace import JsonlTracer, summarize_runs
from sebastian.types import LLMResponse, RunResult, StopReason, ToolCall

if TYPE_CHECKING:
    from sebastian.evals.leaderboard import Row


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sebastian", description="Sebastian autonomous agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run a task with a real model")
    run.add_argument("task")
    _add_run_options(run)

    resume = sub.add_parser("resume", help="continue a run that stopped early")
    resume.add_argument("run_id")
    _add_run_options(resume)

    demo = sub.add_parser("demo", help="offline scripted run (no API key) to verify install")
    demo.add_argument("--workspace", "-w", type=Path, required=True)

    traces = sub.add_parser("traces", help="list recent runs from traces.jsonl")
    traces.add_argument("-n", "--limit", type=_positive_int, default=10, help="runs to show")

    undo = sub.add_parser("undo", help="restore the files a run changed")
    undo.add_argument("run_id")
    undo.add_argument("--workspace", "-w", type=Path, default=Path.cwd())
    undo.add_argument("--list", action="store_true", help="only show the files the run changed")
    undo.add_argument(
        "--force", action="store_true", help="also restore files changed since the run"
    )

    scar = sub.add_parser("scar", help="turn a failed run into a permanent eval case")
    scar.add_argument("run_id")
    scar.add_argument("--workspace", "-w", type=Path, default=Path.cwd())
    scar.add_argument("--out", type=Path, default=Path("evals/scars"), help="scar directory")
    scar.add_argument("--name", help="scar name (default: from the task)")

    evo = sub.add_parser("evolve", help="improve Sebastian's own repo: one backlog item, gated")
    evo.add_argument("--repo", type=Path, default=Path.cwd(), help="Sebastian git checkout")
    evo.add_argument("--item", help="backlog id to attempt (default: first unlocked)")
    evo.add_argument("--base", default="HEAD", help="ref to branch from")
    evo.add_argument("--model", help="override SEBASTIAN_MODEL")
    evo.add_argument("--no-evals", action="store_true", help="skip the eval-regression gate")
    evo.add_argument("--push", action="store_true", help="push an accepted branch to origin")
    evo.add_argument("--pr", action="store_true", help="also open a PR with gh (implies --push)")
    evo.add_argument(
        "--resumes",
        type=int,
        default=1,
        help="resume a budget-stopped agent run this many times (0 disables)",
    )
    evo.add_argument(
        "--gauntlet",
        action="store_true",
        help="the inner run must pass the Gauntlet; a needs_help run is rejected",
    )
    evo.add_argument(
        "--gauntlet-revert",
        action="store_true",
        help="also undo failed Gauntlet attempts before retrying (implies --gauntlet)",
    )
    evo.add_argument("--critic-model", help="Gauntlet critic model (implies --gauntlet)")
    evo.add_argument(
        "--sandbox",
        choices=["auto", "bwrap", "none"],
        default="auto",
        help="run the gate checks under bubblewrap: auto (if usable), bwrap (required), none",
    )

    red = sub.add_parser("redteam", help="attack Sebastian with generated prompt injections")
    red.add_argument("-n", type=_positive_int, default=5, help="attacks to generate (max 20)")
    red.add_argument("--model", help="target model (default SEBASTIAN_MODEL)")
    red.add_argument("--attacker-model", help="attacker model (default: target model)")
    red.add_argument("--out", type=Path, default=Path("evals/scars"), help="where breaches go")
    red.add_argument("--no-save", action="store_true", help="report breaches without saving")
    red.add_argument(
        "--target",
        choices=["agent", "critic"],
        default="agent",
        help="agent: injections against the acting model; critic: tampered changes against the "
        "Gauntlet reviewer",
    )

    dash = sub.add_parser("dashboard", help="write an HTML page of scores, scars and runs")
    dash.add_argument("-o", "--out", type=Path, default=Path("sebastian-dashboard.html"))
    dash.add_argument("--scars", type=Path, default=Path("evals/scars"), help="scar directory")

    _add_models_parser(sub)
    _add_show_parser(sub)

    sub.add_parser("version")
    return p


def _add_models_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    mods = sub.add_parser("models", help="run the eval suite on several models and rank them")
    mods.add_argument(
        "specs",
        nargs="+",
        metavar="SPEC",
        help="[backend:]model[@base_url], e.g. "
        "claude-sonnet-5-5 or openai:qwen3@http://localhost:11434/v1",
    )
    mods.add_argument("--gauntlet", action="store_true", help="also run under the Gauntlet")
    mods.add_argument("--scars", type=Path, default=Path("evals/scars"), help="scar directory")
    mods.add_argument("--no-record", action="store_true", help="don't add results to history")
    mods.add_argument(
        "--preflight-ping",
        action="store_true",
        help="before running, send each model one tiny request (costs a few tokens)",
    )
    mods.add_argument(
        "--max-total-tokens", type=int, default=0, help="stop the whole run at this many tokens"
    )
    mods.add_argument(
        "--max-seconds", type=float, default=0.0, help="stop the whole run after this long"
    )
    mods.add_argument(
        "--max-cost-usd",
        type=float,
        default=0.0,
        help="stop the whole run at this spend (every model needs a price in prices.json)",
    )
    mods.add_argument(
        "--yes", action="store_true", help="run even if the worst-case token estimate is large"
    )
    mods.add_argument(
        "--unsafe-unsandboxed-checkers",
        action="store_true",
        help="run code-executing checkers and commands without bubblewrap (host process, "
        "scrubbed env, network on); live runs refuse those cases otherwise",
    )


def _add_show_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    show = sub.add_parser("show", help="one run's trace as an HTML (or text) timeline")
    show.add_argument("run_id", help="run id, or a unique prefix of at least 6 characters")
    show.add_argument("-o", "--out", type=Path, help="HTML file (default: sebastian-run-<id>.html)")
    show.add_argument("--text", action="store_true", help="print a text timeline instead")


def _add_run_options(run: argparse.ArgumentParser) -> None:
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
    run.add_argument(
        "--sandbox",
        choices=["auto", "bwrap", "none"],
        default="auto",
        help="isolate run_command (no network, workspace-only): auto and bwrap require "
        "bubblewrap and refuse to run without it; none runs on the host (network cut off "
        "with unshare when possible)",
    )
    run.add_argument(
        "--allow-network",
        action="store_true",
        help="with --sandbox none: let commands use the network when unshare can't cut it off",
    )
    run.add_argument("--plan-model", help="model for the planning phase (implies --plan)")
    run.add_argument("--escalation-model", help="model for the step after repeated tool failures")
    run.add_argument(
        "--max-cost",
        type=_non_negative_float,
        help="stop the run at this many USD (needs prices in $SEBASTIAN_HOME/prices.json)",
    )
    run.add_argument(
        "--gauntlet",
        action="store_true",
        help="make the run prove it is done: plan, predict checks, verify them, then a critic "
        "review; ends as needs_help if it cannot",
    )
    run.add_argument(
        "--gauntlet-revert",
        action="store_true",
        help="undo a failed Gauntlet attempt's file changes before retrying (implies --gauntlet)",
    )
    run.add_argument(
        "--critic-model", help="model for the Gauntlet critic (implies --gauntlet; default: "
        "the planning model)",
    )  # fmt: skip


def _non_negative_float(value: str) -> float:
    try:
        n = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not 0 <= n < float("inf"):
        raise argparse.ArgumentTypeError("must be zero or more")
    return n


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
        cost = f"  cost=${r.cost:.4f}" if r.cost else ""
        print(f"{r.run_id}  {started}  {r.stop}  steps={r.steps}  tokens={r.tokens}{cost}")
    return 0


def _undo(store: Checkpoints, args: argparse.Namespace) -> int:
    try:
        if args.list:
            for rel in store.files(args.run_id):
                print(rel)
            return 0
        report = store.undo(args.run_id, Workspace(args.workspace), force=args.force)
    except CheckpointError as exc:
        print(f"undo: {exc}", file=sys.stderr)
        return 1
    for rel in report.restored:
        print(f"restored  {rel}")
    for rel in report.deleted:
        print(f"deleted   {rel}")
    for rel in report.skipped:
        print(f"skipped   {rel} (changed since the run; use --force)")
    return 1 if report.skipped else 0


def _scar(cfg: Config, args: argparse.Namespace) -> int:
    from sebastian.evals.scar import ScarError, make_scar, write_scar  # noqa: PLC0415

    try:
        spec = make_scar(
            args.run_id,
            cfg.home / "traces.jsonl",
            args.workspace,
            Checkpoints(cfg.home / "checkpoints"),
            name=args.name,
        )
        path = write_scar(spec, args.out)
    except ScarError as exc:
        print(f"scar: {exc}", file=sys.stderr)
        return 1
    print(path)
    print(
        f"seeded {len(spec['files'])} file(s); default check: task completes. "
        "Tighten 'expect' (files, answer_contains) before committing.",
        file=sys.stderr,
    )
    return 0


def _evolve(cfg: Config, args: argparse.Namespace, llm_factory: Callable[[str], LLM]) -> int:
    from sebastian.evolve import EvolveConfig, EvolveError, evolve_once, open_pr  # noqa: PLC0415

    model = args.model or cfg.model
    llm = llm_factory(model)
    gauntlet = None
    if args.gauntlet or args.gauntlet_revert or args.critic_model:
        critic_model = args.critic_model or cfg.critic_model
        critic: LLM | None = None
        if critic_model and critic_model != model:
            critic = llm_factory(critic_model)
        gauntlet = GauntletPolicy(critic_llm=critic, revert_on_loop=args.gauntlet_revert)
    config = EvolveConfig(
        base=args.base,
        item_id=args.item,
        push=args.push or args.pr,
        sandbox=args.sandbox,
        max_resumes=max(args.resumes, 0),
        home=cfg.home,
        gauntlet=gauntlet,
    )
    if args.no_evals:
        config.eval_cmd = None
    try:
        report = evolve_once(
            args.repo,
            llm,
            config,
            tracer=JsonlTracer(cfg.home / "traces.jsonl", SystemClock()),
            opener=open_pr if args.pr else None,
        )
    except EvolveError as exc:
        print(f"evolve: {exc}", file=sys.stderr)
        return 2
    print(report.summary())
    return 0 if report.accepted else 1


def _redteam(cfg: Config, args: argparse.Namespace, llm_factory: Callable[[str], LLM]) -> int:
    from sebastian.evals.redteam import RedTeamError, run_redteam  # noqa: PLC0415

    target = args.model or cfg.model
    attacker = llm_factory(args.attacker_model or target)
    try:
        report = run_redteam(
            attacker,
            lambda: llm_factory(target),
            args.n,
            None if args.no_save else args.out,
            target=args.target,
        )
    except RedTeamError as exc:
        print(f"redteam: {exc}", file=sys.stderr)
        return 2
    print(report.summary())
    if report.attacks:
        resisted = len(report.attacks) - len(report.breaches)
        kind = "redteam" if args.target == "agent" else "redteam_critic"
        history.append(
            cfg.home / history.HISTORY_FILE,
            history.Record(
                time.time(), kind, target, resisted, len(report.attacks),
                resisted, len(report.attacks),
            ),
        )  # fmt: skip
    return 1 if report.breaches else 0


def _show(cfg: Config, args: argparse.Namespace) -> int:
    from sebastian.timeline import (  # noqa: PLC0415
        TimelineError,
        build,
        load_events,
        render_html,
        render_text,
    )

    try:
        run_id, events = load_events(cfg.home / "traces.jsonl", args.run_id)
    except TimelineError as exc:
        print(f"show: {exc}", file=sys.stderr)
        return 1
    entries = build(events)
    if args.text:
        print(render_text(run_id, entries))
        return 0
    out = args.out or Path(f"sebastian-run-{run_id[:12]}.html")
    out.write_text(render_html(run_id, entries, time.time()), encoding="utf-8")
    print(out)
    return 0


def _dashboard(cfg: Config, args: argparse.Namespace) -> int:
    from sebastian.dashboard import gather, render  # noqa: PLC0415

    snap = gather(cfg.home, args.scars, time.time())
    args.out.write_text(render(snap), encoding="utf-8")
    print(args.out)
    print(
        f"{len(snap.records)} scored round(s), {len(snap.runs)} run(s), {len(snap.scars)} scar(s)",
        file=sys.stderr,
    )
    return 0


def _models(
    cfg: Config,
    args: argparse.Namespace,
    llm_factory: Callable[[str], LLM] | None,
    prices: Mapping[str, Price],
) -> int:
    from sebastian.evals.cases import ALL  # noqa: PLC0415
    from sebastian.evals.harness import CASE_TOKENS  # noqa: PLC0415
    from sebastian.evals.leaderboard import (  # noqa: PLC0415
        CONFIRM_TOKENS,
        Ceilings,
        LeaderboardError,
        ModelSpec,
        parse_spec,
        preflight,
        rank,
        render,
        run_leaderboard,
        runnable,
        worst_case_tokens,
    )
    from sebastian.evals.sandbox import UNSAFE_WARNING  # noqa: PLC0415
    from sebastian.evals.scar import ScarError, load_scars  # noqa: PLC0415

    def factory(spec: ModelSpec) -> LLM:
        if llm_factory is not None:  # tests inject scripted models by name
            llm = llm_factory(spec.model)
        else:
            llm = make_llm(
                spec.model,
                spec.backend or cfg.backend,
                spec.base_url or cfg.base_url,
                spec.key_env or (cfg.api_key_env if not spec.base_url else ""),
            )
        return priced(llm, spec.model, prices)

    done: list[Row] = []
    now = time.time()
    run_id = f"lb-{int(now)}-{secrets.token_hex(3)}"

    def finished(row: Row) -> None:  # print and record each model as soon as it is done
        done.append(row)
        status = f"error: {row.error}" if row.error else f"score {row.score:.0%}"
        if row.partial:
            status += f" (partial: {row.stopped}; not recorded)"
        print(f"models: [{len(done)}/{len(specs)}] {row.spec.label}: {status}", file=sys.stderr)
        if not args.no_record and not row.error and not row.partial:
            _record_row(cfg, now, row, run_id)

    try:
        specs = [parse_spec(s) for s in args.specs]
        cases = [*ALL, *load_scars(args.scars)]
        ceilings = Ceilings(args.max_total_tokens, args.max_seconds, args.max_cost_usd)
        if args.max_cost_usd:
            require_prices([s.model for s in specs], prices)
        worst = worst_case_tokens(len(specs), cases, args.gauntlet)
        capped = min(worst, args.max_total_tokens) if args.max_total_tokens else worst
        print(
            f"models: worst case {worst:,} tokens ({len(specs)} model(s) x "
            f"{runnable(cases, args.gauntlet)} cases x {CASE_TOKENS:,})"
            + (f", capped at {capped:,}" if capped < worst else ""),
            file=sys.stderr,
        )
        if capped > CONFIRM_TOKENS and not args.yes:
            print(
                f"models: that is over {CONFIRM_TOKENS:,} tokens; pass --yes to run it, "
                "or cap it with --max-total-tokens",
                file=sys.stderr,
            )
            return 2
        problems = preflight(specs, factory, ping=args.preflight_ping)
        if problems:
            print("models: preflight failed; nothing was run:", file=sys.stderr)
            for line in problems:
                print(f"  {line}", file=sys.stderr)
            return 2
        if args.unsafe_unsandboxed_checkers:
            print(UNSAFE_WARNING, file=sys.stderr)
        rows = run_leaderboard(
            specs,
            factory,
            cases,
            gauntlet=args.gauntlet,
            unsafe_checkers=args.unsafe_unsandboxed_checkers,
            on_row=finished,
            ceilings=ceilings,
        )
    except (LeaderboardError, ScarError, PricingError) as exc:
        print(f"models: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(render(rank(done)))
        print(f"models: interrupted after {len(done)}/{len(specs)} model(s)", file=sys.stderr)
        return 130
    print(render(rows))
    return 0


def _record_row(cfg: Config, now: float, row: Row, run_id: str) -> None:
    """Add one leaderboard row's plain and Gauntlet results to history."""
    pairs = (("leaderboard", row.plain), ("leaderboard-gauntlet", row.gauntlet))
    for kind, suite in pairs:
        if suite is None:
            continue
        hostile = [r for r in suite.results if r.hostile]
        history.append(
            cfg.home / history.HISTORY_FILE,
            history.Record(
                now, kind, row.spec.label,
                sum(r.passed for r in suite.results), len(suite.results),
                sum(r.resisted for r in hostile), len(hostile), run_id,
            ),
        )  # fmt: skip


def _start(agent: Sebastian, args: argparse.Namespace) -> int:
    if args.cmd != "resume":
        return _print_result(agent.run(args.task))
    try:
        return _print_result(agent.resume(args.run_id))
    except SessionError as exc:
        print(f"resume: {exc}", file=sys.stderr)
        return 1


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


def _real_factory(cfg: Config) -> Callable[[str], LLM]:
    """Real model clients for the configured backend (``SEBASTIAN_BACKEND``, B37)."""

    def make(name: str) -> LLM:
        return make_llm(name, cfg.backend, cfg.base_url, cfg.api_key_env)

    return make


def _run_agent(
    cfg: Config,
    args: argparse.Namespace,
    make: Callable[[str], LLM],
    prices: Mapping[str, Price],
    ws: Workspace,
    registry: ToolRegistry,
    checkpoints: Checkpoints | None,
    run_info: Mapping[str, str] | None = None,
) -> int:
    """Build the agent for ``run`` / ``resume`` and start it."""
    clock = SystemClock()
    approver: Approver
    if args.unattended or not sys.stdin.isatty():
        approver = DenyApprover()
    else:
        approver = InteractiveApprover()

    model = args.model or cfg.model
    llm = make(model)
    plan_model = args.plan_model or cfg.plan_model
    escalation_model = args.escalation_model or cfg.escalation_model
    critic_model = args.critic_model or cfg.critic_model
    gauntlet = None
    if args.gauntlet or args.critic_model or args.gauntlet_revert:
        critic = make(critic_model) if critic_model and critic_model != model else None
        gauntlet = GauntletPolicy(critic_llm=critic, revert_on_loop=args.gauntlet_revert)
    max_cost = cfg.max_cost if args.max_cost is None else args.max_cost
    if max_cost:
        used = [model, plan_model, escalation_model, critic_model if gauntlet else ""]
        try:
            require_prices([m for m in used if m], prices)
        except PricingError as exc:
            print(f"sebastian: {exc}", file=sys.stderr)
            return 2

    agent = Sebastian(
        llm=llm,
        tools=registry,
        gate=ApprovalGate(approver),
        budget=Budget(cfg.max_steps, cfg.max_tokens, float(cfg.max_seconds), max_cost),
        memory=None if args.no_memory else MemoryStore(cfg.home / "memory.json"),
        tracer=JsonlTracer(cfg.home / "traces.jsonl", clock),
        clock=clock,
        checkpoints=checkpoints,
        sessions=SessionStore(cfg.home / "sessions"),
        workspace=str(ws.root),
        plan=args.plan or bool(args.plan_model),
        planner_llm=make(plan_model) if plan_model and plan_model != model else None,
        escalation_llm=(
            make(escalation_model) if escalation_model and escalation_model != model else None
        ),
        gauntlet=gauntlet,
        run_info=dict(run_info or {}),
    )
    return _start(agent, args)


def main(
    argv: Sequence[str] | None = None,
    llm_factory: Callable[[str], LLM] | None = None,
) -> int:
    """Parse ``argv`` and run a CLI command. Returns the process exit code."""
    args = _build_parser().parse_args(argv)
    if args.cmd == "version":
        print(__version__)
        return 0

    cfg = Config.from_env()
    try:
        prices = load_prices(cfg.home)
    except PricingError as exc:
        print(f"sebastian: {exc}", file=sys.stderr)
        return 2
    base_factory = llm_factory or _real_factory(cfg)

    def factory(name: str) -> LLM:
        return priced(base_factory(name), name, prices)

    tools_only: dict[str, Callable[[], int]] = {
        "traces": lambda: _print_traces(cfg.home / "traces.jsonl", args.limit),
        "undo": lambda: _undo(Checkpoints(cfg.home / "checkpoints"), args),
        "scar": lambda: _scar(cfg, args),
        "evolve": lambda: _evolve(cfg, args, factory),
        "redteam": lambda: _redteam(cfg, args, factory),
        "dashboard": lambda: _dashboard(cfg, args),
        "models": lambda: _models(cfg, args, llm_factory, prices),
        "show": lambda: _show(cfg, args),
    }
    if args.cmd in tools_only:
        return tools_only[args.cmd]()

    ws = Workspace(args.workspace)
    live = args.cmd in {"run", "resume"}
    checkpoints = Checkpoints(cfg.home / "checkpoints") if live else None
    registry = ToolRegistry(file_tools(ws, checkpoints))

    if args.cmd == "demo":
        from sebastian.approval import AllowApprover  # noqa: PLC0415

        agent = Sebastian(llm=_demo_llm(), tools=registry, gate=ApprovalGate(AllowApprover()))
        return _print_result(agent.run("Write hello.txt containing 'hello' and verify it."))

    run_info: dict[str, str] = {}
    if args.allow_cmd:
        try:
            sandbox, note = resolve_command_sandbox(args.sandbox, allow_network=args.allow_network)
        except ToolError as exc:
            print(f"sebastian: {exc}", file=sys.stderr)
            return 2
        if note:
            print(note, file=sys.stderr)
        extra = tuple(python_roots()) if sandbox == "bwrap" else ()
        registry.register(
            command_tool(ws, CommandPolicy.of(args.allow_cmd, sandbox=sandbox, extra_ro=extra))
        )
        run_info = {
            "sandbox": "bwrap" if sandbox == "bwrap" else "none",
            "network": "host" if sandbox == "none" else "isolated",
        }

    return _run_agent(cfg, args, factory, prices, ws, registry, checkpoints, run_info)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
