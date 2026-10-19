"""Eval suite for Sebastian: capability and prompt-injection cases, offline or live."""

from sebastian.evals.cases import ALL, CAPABILITY, CRITIC, INJECTION
from sebastian.evals.harness import (
    CaseResult,
    EvalCase,
    Outcome,
    SuiteResult,
    compare,
    run_case,
    run_suite,
)

__all__ = [
    "ALL",
    "CAPABILITY",
    "CRITIC",
    "INJECTION",
    "CaseResult",
    "EvalCase",
    "Outcome",
    "SuiteResult",
    "compare",
    "run_case",
    "run_suite",
]
