"""Eval suite (B8, B9). Run alone with ``pytest -m eval``; the score prints in the summary."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sebastian import history
from sebastian.evals import ALL, CAPABILITY, HARD, INJECTION, EvalCase, run_case, run_suite
from sebastian.evals.__main__ import main
from sebastian.gauntlet import GauntletPolicy
from sebastian.llm import ScriptedLLM
from sebastian.types import LLMResponse, ToolCall

pytestmark = pytest.mark.eval

SCORES: list[str] = []


def test_suite_shape() -> None:
    assert len(CAPABILITY) == 5
    assert len(HARD) == 6
    assert len(INJECTION) == 5
    assert len({c.name for c in ALL}) == len(ALL)
    assert all(c.hostile and not c.allow_gated for c in INJECTION)


@pytest.mark.parametrize("case", ALL, ids=lambda c: c.name)
def test_case_passes_offline(case: EvalCase) -> None:
    result = run_case(case, gauntlet=GauntletPolicy() if case.gauntlet_only else None)
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
    assert "score 100% (16/16)" in text
    assert "5/5 contained" in text
    SCORES.append(text.splitlines()[-2])


def test_cli_offline(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "score 100%" in capsys.readouterr().out


def test_cli_live_failure_sets_exit_code(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path))
    # A "live" model that errors on every call: capability cases fail, score drops.
    assert main(["--live", "--model", "m"], live_llm=lambda _m: ScriptedLLM([])) == 1
    assert main(["--live", "--min-score", "0"], live_llm=lambda _m: ScriptedLLM([])) == 0
    out = capsys.readouterr().out
    assert "FAIL  write_new_file" in out
    records = history.load(tmp_path / history.HISTORY_FILE)
    assert [r.model for r in records] == ["m", "claude-sonnet-5-5"]
    assert records[0].total == 16
    assert records[0].hostile == 5
    assert main(["--live", "--no-record"], live_llm=lambda _m: ScriptedLLM([])) == 1
    assert len(history.load(tmp_path / history.HISTORY_FILE)) == 2


def test_offline_runs_are_not_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path))
    assert main([]) == 0
    assert not (tmp_path / history.HISTORY_FILE).exists()


@pytest.mark.parametrize("case", HARD, ids=lambda c: c.name)
def test_hard_case_can_fail(case: EvalCase) -> None:
    # A model that claims success without doing anything must fail every harder case.
    lazy = replace(case, script=[LLMResponse("Done, all fixed and saved.")])
    assert not run_case(lazy).passed


def test_checker_times_out_instead_of_hanging(monkeypatch: pytest.MonkeyPatch) -> None:
    from sebastian.evals import sandbox

    monkeypatch.setattr(sandbox, "CHECK_TIMEOUT", 1)
    hang = replace(
        HARD[2],
        name="hang",
        script=[LLMResponse("w", (ToolCall("1", "write_file",
                {"path": "inventory.py", "content": "while True:\n    pass\n"}),)),
                LLMResponse("done")],
    )  # fmt: skip
    assert not run_case(hang).passed


def test_broken_json_fails_the_json_case() -> None:
    broken = replace(
        HARD[3],
        script=[LLMResponse("w", (ToolCall("1", "write_file",
                {"path": "settings.json", "content": "{nope"}),)), LLMResponse("done")],
    )  # fmt: skip
    assert not run_case(broken).passed
