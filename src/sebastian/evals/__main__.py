"""``python -m sebastian.evals [--live] [--model M] [--min-score F] [--gauntlet | --compare]``.

Live runs give code-running cases bubblewrap or refuse them; ``--unsafe-unsandboxed-checkers``
runs them on the host anyway (scrubbed env, network on). With no usable bubblewrap a live run
exits 2 before any model call instead of scoring the refused cases (B48).

Offline by default (scripted, no network). ``--gauntlet`` runs every case under the Gauntlet;
``--compare`` runs plain and Gauntlet and prints them side by side (B11e).
``--live`` needs the ``anthropic`` extra and ``ANTHROPIC_API_KEY``; it costs tokens.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Sequence

from sebastian import history
from sebastian.config import Config
from sebastian.evals.cases import ALL
from sebastian.evals.harness import SuiteResult, compare, injection_cases, is_live, run_suite
from sebastian.evals.sandbox import UNAVAILABLE, UNSAFE_WARNING, checker_sandbox
from sebastian.evals.scar import ScarError, load_scars
from sebastian.gauntlet import GauntletPolicy
from sebastian.llm import make_llm
from sebastian.llm.base import LLM


def main(argv: Sequence[str] | None = None, live_llm: Callable[[str], LLM] | None = None) -> int:
    """Run the eval suite and print the summary. Exit 1 below ``--min-score``."""
    p = argparse.ArgumentParser(prog="python -m sebastian.evals")
    p.add_argument("--live", action="store_true", help="run against a real model")
    p.add_argument("--model", help="override SEBASTIAN_MODEL (live only)")
    p.add_argument("--min-score", type=float, default=1.0, help="exit 1 below this (0-1)")
    p.add_argument("--scars", default="evals/scars", help="directory of scar cases (live-only)")
    p.add_argument("--no-record", action="store_true", help="don't add a live run to history")
    p.add_argument(
        "--unsafe-unsandboxed-checkers",
        action="store_true",
        help="run code-executing checkers and commands without bubblewrap (host process, "
        "scrubbed env, network on); live runs refuse those cases otherwise",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--gauntlet", action="store_true", help="run every case under the Gauntlet")
    mode.add_argument(
        "--compare", action="store_true", help="run plain and Gauntlet, print side by side"
    )
    args = p.parse_args(argv)
    cfg = Config.from_env()

    factory: Callable[[], LLM] | None = None
    if args.live:
        model = args.model or cfg.model
        if live_llm is None:  # pragma: no cover - needs a real backend

            def factory() -> LLM:
                return make_llm(model, cfg.backend, cfg.base_url, cfg.api_key_env)
        else:

            def factory() -> LLM:
                return live_llm(model)

    try:
        scars = load_scars(args.scars)
    except ScarError as exc:
        print(f"evals: {exc}", file=sys.stderr)
        return 2
    cases = [*ALL, *scars]
    unsafe = args.unsafe_unsandboxed_checkers
    if unsafe:
        print(UNSAFE_WARNING, file=sys.stderr)
    gauntlet_runs = args.gauntlet or args.compare
    sandboxed = [
        c for c in cases if (c.runs_code or c.commands) and (gauntlet_runs or not c.gauntlet_only)
    ]
    # Building a client makes no model call; it tells us whether the harness will treat it as live.
    live = factory is not None and is_live(factory())
    if live and sandboxed and checker_sandbox(live=True, unsafe=unsafe) == UNAVAILABLE:
        print(
            f"evals: bubblewrap is unusable here, so {len(sandboxed)} live case(s) that run "
            "model code or commands would be refused; install bubblewrap or pass "
            "--unsafe-unsandboxed-checkers",
            file=sys.stderr,
        )
        return 2
    runs: list[tuple[str, SuiteResult]] = []
    if not args.gauntlet:
        runs.append(("eval", run_suite(cases, factory, unsafe_checkers=unsafe)))
    if gauntlet_runs:
        runs.append(("gauntlet", run_suite(cases, factory, GauntletPolicy(), unsafe)))
    if args.compare:
        print(compare(runs[0][1], runs[1][1]))
    else:
        print(runs[0][1].summary())
    if args.live and not args.no_record:
        for kind, suite in runs:
            _record(cfg, kind, args.model or cfg.model, suite)
    return 0 if all(suite.score >= args.min_score for _, suite in runs) else 1


def _record(cfg: Config, kind: str, model: str, suite: SuiteResult) -> None:
    hostile = injection_cases(suite.results)
    history.append(
        cfg.home / history.HISTORY_FILE,
        history.Record(
            time.time(),
            kind,
            model,
            sum(r.passed for r in suite.results),
            len(suite.results),
            sum(r.resisted for r in hostile),
            len(hostile),
        ),
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
