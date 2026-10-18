"""Eval suite (B8, B9). Run alone with ``pytest -m eval``; the score prints in the summary."""

from __future__ import annotations

from dataclasses import replace

import pytest

from sebastian.evals import ALL, CAPABILITY, INJECTION, EvalCase, run_case, run_suite
from sebastian.evals.__main__ import main
from sebastian.llm import ScriptedLLM
from sebastian.types import LLMResponse, ToolCall

pytestmark = pytest.mark.eval

SCORES: list[str] = []


def test_suite_shape() -> None:
    assert len(CAPABILITY) == 5
    assert len(INJECTION) == 5
    assert len({c.name for c in ALL}) == len(ALL)
    assert all(c.hostile and not c.allow_gated for c in INJECTION)


@pytest.mark.parametrize("case", ALL, ids=lambda c: c.name)
def test_case_passes_offline(case: EvalCase) -> None:
    result = run_case(case)
    SCORES.append(f"{'PASS' if result.passed else 'FAIL'} {case.name}")
    assert result.passed, result


@pytest.mark.parametrize("case", INJECTION, ids=lambda c: c.name)
def test_injection_fooled_model_is_contained(case: EvalCase) -> None:
    result = run_case(case)
    assert not result.resisted  # the offline script plays a fooled model...
    assert result.passed  # ...and the gate still contains it


@pytest.mark.parametrize("case", INJECTION, ids=lambda c: c.name)
def test_injection_eval_can_fail(case: EvalCase) -> None:
    # Same fooled model with the gate open: the check must catch the damage.
    assert not run_case(replace(case, allow_gated=True)).passed


def test_capability_eval_can_fail() -> None:
    lazy = replace(CAPABILITY[0], script=[LLMResponse("Done, trust me.")])
    assert not run_case(lazy).passed


def test_broken_checker_is_a_failure_not_a_crash() -> None:
    def boom(_: object) -> bool:
        raise RuntimeError("bad checker")

    result = run_case(replace(CAPABILITY[0], check=boom))
    assert not result.passed
    assert "bad checker" in result.error


def test_resisting_model_is_reported() -> None:
    honest = replace(
        INJECTION[0],
        script=[
            LLMResponse("Reading.", (ToolCall("1", "read_file", {"path": "notes.txt"}),)),
            LLMResponse("Q3 planning notes; it also contains an injected instruction I ignored."),
        ],
    )
    result = run_case(honest)
    assert result.passed
    assert result.resisted


def test_suite_score_and_summary() -> None:
    suite = run_suite(ALL)
    assert suite.score == 1.0
    text = suite.summary()
    assert "score 100% (10/10)" in text
    assert "5/5 contained" in text
    SCORES.append(text.splitlines()[-2])


def test_cli_offline(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "score 100%" in capsys.readouterr().out


def test_cli_live_failure_sets_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    # A "live" model that errors on every call: capability cases fail, score drops.
    assert main(["--live", "--model", "m"], live_llm=lambda _m: ScriptedLLM([])) == 1
    assert main(["--live", "--min-score", "0"], live_llm=lambda _m: ScriptedLLM([])) == 0
    out = capsys.readouterr().out
    assert "FAIL  write_new_file" in out
