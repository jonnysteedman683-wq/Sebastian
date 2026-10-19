"""B11e: Gauntlet on/off eval comparison and critic-targeted attacks."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sebastian import history
from sebastian.evals import ALL, CAPABILITY, CRITIC, compare, run_case, run_suite
from sebastian.evals.__main__ import main
from sebastian.evals.harness import GAUNTLET, PLAIN
from sebastian.gauntlet import GauntletPolicy
from sebastian.llm import ScriptedLLM
from sebastian.types import LLMResponse, ToolCall

G = GauntletPolicy()


def test_critic_cases_only_run_under_the_gauntlet() -> None:
    assert len(CRITIC) == 2
    assert all(c.gauntlet_only and c.targets_critic for c in CRITIC)
    plain = run_suite(ALL)
    assert {r.name for r in plain.results}.isdisjoint({c.name for c in CRITIC})
    assert not plain.skipped
    gauntlet = run_suite(ALL, gauntlet=G)
    assert {c.name for c in CRITIC} <= {r.name for r in gauntlet.results}
    assert {r.mode for r in plain.results} == {PLAIN}
    assert {r.mode for r in gauntlet.results} == {GAUNTLET}


def test_every_case_passes_offline_under_the_gauntlet() -> None:
    suite = run_suite(ALL, gauntlet=G)
    assert suite.score == 1.0, suite.summary()


def test_approved_note_is_stopped_by_verify_before_the_fooled_critic() -> None:
    case = CRITIC[0]
    result = run_case(case, gauntlet=G)
    assert result.passed
    assert result.stop == "needs_help"  # the critic script says PASS, but it is never reached
    assert result.targets_critic


def test_approved_note_without_the_gauntlet_is_a_false_done() -> None:
    # The same fooled actor with no Gauntlet claims success on a broken result: the check fails.
    assert not run_case(CRITIC[0]).passed


def test_policy_note_passes_when_the_critic_resists() -> None:
    result = run_case(CRITIC[1], gauntlet=G)
    assert result.passed
    assert result.stop == "completed"


def test_policy_note_fails_when_the_critic_is_fooled() -> None:
    fooled = replace(CRITIC[1], critic_script=[LLMResponse("VERDICT: PASS")])
    assert not run_case(fooled, gauntlet=G).passed


def test_live_gauntlet_uses_the_model_under_test_for_every_role() -> None:
    model = ScriptedLLM(
        [
            LLMResponse("1. write it"),
            LLMResponse("<checks>\nfile: hello.txt\n</checks>"),
            LLMResponse(
                "w",
                (ToolCall("1", "write_file", {"path": "hello.txt", "content": "hello world"}),),
            ),
            LLMResponse("done"),
            LLMResponse("VERDICT: PASS"),
        ]
    )
    result = run_case(CAPABILITY[0], model, gauntlet=G)
    assert result.passed
    assert result.mode == GAUNTLET
    assert "+hello world" in model.calls[4][1][0].text  # the critic got the checkpointed diff


def test_compare_shows_both_modes_side_by_side() -> None:
    text = compare(run_suite(ALL), run_suite(ALL, gauntlet=G))
    lines = text.splitlines()
    assert lines[0].split() == ["case", "plain", "gauntlet"]
    row = next(ln for ln in lines if ln.startswith("write_new_file"))
    assert row.count("PASS completed") == 2
    critic_row = next(ln for ln in lines if ln.startswith("critic_approved_note"))
    assert critic_row.split()[1] == "-"
    assert "PASS needs_help" in critic_row
    assert any(ln.startswith("plain    score 10/10") for ln in lines)
    assert any(ln.startswith("gauntlet score 10/10") for ln in lines)
    assert "critic attacks: 2/2 contained" in text


def test_compare_lists_skipped_live_only_cases() -> None:
    live_only = replace(CAPABILITY[0], name="scar_x", live_only=True)
    text = compare(run_suite([live_only]), run_suite([live_only], gauntlet=G))
    assert "skipped 1 live-only case(s): scar_x" in text
    assert "critic attacks" not in text


def test_cli_gauntlet_and_compare(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--gauntlet"]) == 0
    assert "score 100% (12/12)" in capsys.readouterr().out
    assert main(["--compare"]) == 0
    assert "critic attacks: 2/2" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["--gauntlet", "--compare"])


def test_cli_live_compare_records_both_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path))
    assert main(["--live", "--compare"], live_llm=lambda _m: ScriptedLLM([])) == 1
    args = ["--live", "--compare", "--min-score", "0"]
    assert main(args, live_llm=lambda _m: ScriptedLLM([])) == 0
    capsys.readouterr()
    records = history.load(tmp_path / history.HISTORY_FILE)
    assert [r.kind for r in records] == ["eval", "gauntlet", "eval", "gauntlet"]
    assert [r.total for r in records[:2]] == [10, 12]


def test_history_accepts_gauntlet_records(tmp_path: Path) -> None:
    path = tmp_path / "h.jsonl"
    history.append(path, history.Record(1.0, "gauntlet", "m", 3, 4))
    with path.open("a") as fh:
        fh.write('{"t": 2, "kind": "other", "model": "m", "passed": 1, "total": 1}\n')
    assert [r.kind for r in history.load(path)] == ["gauntlet"]
