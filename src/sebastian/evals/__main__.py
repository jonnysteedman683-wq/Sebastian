"""``python -m sebastian.evals [--live] [--model M] [--min-score F] [--gauntlet | --compare]``.

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
from sebastian.evals.harness import SuiteResult, compare, run_suite
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
                return make_llm(model, cfg.backend, cfg.base_url)
        else:

            def factory() -> LLM:
                return live_llm(model)

    try:
        scars = load_scars(args.scars)
    except ScarError as exc:
        print(f"evals: {exc}", file=sys.stderr)
        return 2
    cases = [*ALL, *scars]
    runs: list[tuple[str, SuiteResult]] = []
    if not args.gauntlet:
        runs.append(("eval", run_suite(cases, factory)))
    if args.gauntlet or args.compare:
        runs.append(("gauntlet", run_suite(cases, factory, GauntletPolicy())))
    if args.compare:
        print(compare(runs[0][1], runs[1][1]))
    else:
        print(runs[0][1].summary())
    if args.live and not args.no_record:
        for kind, suite in runs:
            _record(cfg, kind, args.model or cfg.model, suite)
    return 0 if all(suite.score >= args.min_score for _, suite in runs) else 1


def _record(cfg: Config, kind: str, model: str, suite: SuiteResult) -> None:
    hostile = [r for r in suite.results if r.hostile]
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
