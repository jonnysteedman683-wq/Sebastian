"""``python -m sebastian.evals [--live] [--model M] [--min-score F]``.

Offline by default (scripted, no network). ``--live`` needs the ``anthropic`` extra and
``ANTHROPIC_API_KEY``; it costs tokens.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from sebastian.config import Config
from sebastian.evals.cases import ALL
from sebastian.evals.harness import run_suite
from sebastian.evals.scar import ScarError, load_scars
from sebastian.llm.base import LLM, RetryingLLM


def main(argv: Sequence[str] | None = None, live_llm: Callable[[str], LLM] | None = None) -> int:
    """Run the eval suite and print the summary. Exit 1 below ``--min-score``."""
    p = argparse.ArgumentParser(prog="python -m sebastian.evals")
    p.add_argument("--live", action="store_true", help="run against a real model")
    p.add_argument("--model", help="override SEBASTIAN_MODEL (live only)")
    p.add_argument("--min-score", type=float, default=1.0, help="exit 1 below this (0-1)")
    p.add_argument("--scars", default="evals/scars", help="directory of scar cases (live-only)")
    args = p.parse_args(argv)

    factory: Callable[[], LLM] | None = None
    if args.live:
        model = args.model or Config.from_env().model
        if live_llm is None:  # pragma: no cover - needs the SDK and a key
            from sebastian.llm.anthropic import AnthropicLLM  # noqa: PLC0415

            def factory() -> LLM:
                return RetryingLLM(AnthropicLLM(model))
        else:

            def factory() -> LLM:
                return live_llm(model)

    try:
        scars = load_scars(args.scars)
    except ScarError as exc:
        print(f"evals: {exc}", file=sys.stderr)
        return 2
    suite = run_suite([*ALL, *scars], factory)
    print(suite.summary())
    return 0 if suite.score >= args.min_score else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
