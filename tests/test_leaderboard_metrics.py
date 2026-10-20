"""P3-12: repeats with spread, latency, cost and a stop-reason breakdown on the leaderboard."""

from __future__ import annotations

import io
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from sebastian import history
from sebastian.cli import main
from sebastian.evals.cases import CAPABILITY
from sebastian.evals.harness import CaseResult, SuiteResult
from sebastian.evals.leaderboard import (
    LeaderboardError,
    ModelSpec,
    Row,
    positions,
    render,
    run_leaderboard,
)
from sebastian.llm.base import LLM, LLMError
from sebastian.llm.scripted import ScriptedLLM
from sebastian.tools.base import Tool
from sebastian.types import LLMResponse, Message, Usage

FIRST = CAPABILITY[0]


def solver() -> LLM:
    return ScriptedLLM(FIRST.script)


def lazy() -> LLM:
    return ScriptedLLM([LLMResponse("Done.")] * 10)


def flaky() -> Callable[[], LLM]:
    """Solves the case on odd calls only: a model that is right 2 times out of 3."""
    calls = iter(range(100))
    return lambda: solver() if next(calls) % 2 == 0 else lazy()


def test_repeats_show_mean_and_range_and_ties() -> None:
    makers: dict[str, Callable[[], LLM]] = {"flaky": flaky(), "steady": solver, "bad": lazy}
    specs = [ModelSpec("flaky"), ModelSpec("steady"), ModelSpec("bad")]
    rows = run_leaderboard(specs, lambda s: makers[s.model](), [FIRST], repeats=3)
    by = {r.spec.model: r for r in rows}
    assert len(by["flaky"].plain_runs) == 3
    assert by["flaky"].score == pytest.approx(2 / 3)
    assert by["flaky"].score_range == (0.0, 1.0)
    assert by["steady"].score_range == (1.0, 1.0)
    assert [r.spec.model for r in rows] == ["steady", "flaky", "bad"]
    assert positions(rows) == ["1", "=1", "=1"]  # flaky's 0-100% overlaps both neighbours
    table = render(rows)
    assert "0%-100%" in table
    assert "=1" in table


def test_no_tie_when_ranges_do_not_overlap() -> None:
    rows = run_leaderboard(
        [ModelSpec("good"), ModelSpec("bad")],
        lambda s: solver() if s.model == "good" else lazy(),
        [FIRST],
        repeats=2,
    )
    assert positions(rows) == ["1", "2"]


def test_error_heavy_model_shows_its_error_count() -> None:
    class Broken:
        def complete(
            self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
        ) -> LLMResponse:
            raise LLMError("HTTP 500")

    rows = run_leaderboard([ModelSpec("broken")], lambda _s: Broken(), CAPABILITY)
    assert rows[0].stops["error"] == len(CAPABILITY)
    assert f"0/0/{len(CAPABILITY)}" in render(rows)


def test_latency_and_cost_columns() -> None:
    def case(seconds: float, cost: float, steps: int = 1) -> CaseResult:
        return CaseResult(
            "c", True, False, True, "completed", steps, 10, cost=cost, seconds=seconds
        )

    row = Row(ModelSpec("m"), SuiteResult([case(1.0, 0.5), case(3.0, 0.25), case(9.0, 0, 0)]))
    assert row.median_seconds == 2.0  # the case that never reached the model is left out
    assert row.cost == 0.75
    text = render([row])
    assert "$0.75" in text
    assert "2.0" in text
    assert render([Row(ModelSpec("m"), SuiteResult())]).count(" - ") >= 3


def test_render_is_fixed_width() -> None:
    rows = run_leaderboard(
        [ModelSpec("a"), ModelSpec("much-longer-name")], lambda _s: lazy(), CAPABILITY
    )
    body = render(rows).splitlines()[:-1]
    assert len({len(line) for line in body}) == 1


def test_repeats_limits() -> None:
    with pytest.raises(LeaderboardError, match="repeats"):
        run_leaderboard([ModelSpec("a")], lambda _s: lazy(), CAPABILITY, repeats=0)


def test_cli_repeats_records_every_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    args = ["models", "m", "--repeats", "3", "--yes", "--scars", str(tmp_path / "none")]
    assert main(args, llm_factory=lambda _n: lazy()) == 0
    err = capsys.readouterr().err
    assert "x 48 cases" in err  # 16 plain cases x 3 repeats in the estimate
    (rec,) = history.load(tmp_path / "home" / history.HISTORY_FILE)
    assert rec.total == 48


def test_usage_cost_flows_into_case_results() -> None:
    class Paid:
        def complete(
            self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
        ) -> LLMResponse:
            return LLMResponse("Done.", usage=Usage(1, 1, cost=0.01))

    rows = run_leaderboard([ModelSpec("p")], lambda _s: Paid(), [FIRST])
    assert rows[0].cost == pytest.approx(0.01)
