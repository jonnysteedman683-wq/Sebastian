"""P2-13: real clock for live evals, and whole-run ceilings for `sebastian models`."""

from __future__ import annotations

import io
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from sebastian import history
from sebastian.cli import main
from sebastian.clock import FakeClock, SystemClock
from sebastian.evals.cases import ALL, CAPABILITY, HARD
from sebastian.evals.harness import CASE_TOKENS, run_case, run_suite
from sebastian.evals.leaderboard import (
    CONFIRM_TOKENS,
    Ceilings,
    ModelSpec,
    render,
    run_leaderboard,
    runnable,
    worst_case_tokens,
)
from sebastian.llm.base import LLM
from sebastian.llm.scripted import ScriptedLLM
from sebastian.tools.base import Tool
from sebastian.types import LLMResponse, Message, ToolCall, Usage


class SlowLLM:
    """A live-looking model whose every call takes 400 s on ``clock``."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> LLMResponse:
        self.clock.advance(400)
        return LLMResponse("look", (ToolCall("1", "read_file", {"path": "settings.json"}),))


class Spender:
    """A live-looking model that answers at once and reports 600 tokens per call."""

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> LLMResponse:
        return LLMResponse("Done.", usage=Usage(500, 100))


def test_live_case_hits_the_time_limit() -> None:
    clock = FakeClock()
    result = run_case(HARD[3], SlowLLM(clock), clock=clock)  # edit_json_config: no code run
    assert result.stop == "budget_exhausted"
    assert result.steps == 1  # 400 s > CASE_SECONDS after one call


def test_live_runs_use_the_system_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[object] = []
    from sebastian.agent import Sebastian

    def spy(**kwargs: object) -> object:
        seen.append(kwargs["clock"])
        return Sebastian(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("sebastian.evals.harness.Sebastian", spy)
    run_case(HARD[3], Spender())
    run_case(HARD[3])
    run_case(HARD[3], ScriptedLLM(HARD[3].script))
    assert isinstance(seen[0], SystemClock)
    assert isinstance(seen[1], FakeClock)
    assert isinstance(seen[2], FakeClock)


def test_scripted_runs_stay_deterministic() -> None:
    first = run_suite(ALL)
    second = run_suite(ALL)
    assert first.results == second.results
    assert not first.partial


def test_token_ceiling_stops_after_the_crossing_model() -> None:
    built: list[str] = []

    def factory(spec: ModelSpec) -> LLM:
        built.append(spec.model)
        return Spender()

    specs = [ModelSpec("a"), ModelSpec("b"), ModelSpec("c")]
    rows = run_leaderboard(specs, factory, CAPABILITY, ceilings=Ceilings(max_total_tokens=1000))
    assert [r.spec.model for r in rows] == ["a"]
    assert rows[0].partial
    assert "token limit 1,000" in rows[0].stopped
    assert len(rows[0].plain.results) == 2  # 600 + 600 crosses 1,000 on the second case
    assert set(built) == {"a"}
    assert "a (partial)" in render(rows)


def test_time_and_cost_ceilings() -> None:
    t = iter(range(0, 10_000, 100))
    rows = run_leaderboard(
        [ModelSpec("a"), ModelSpec("b")],
        lambda _s: Spender(),
        CAPABILITY,
        ceilings=Ceilings(max_seconds=250),
        clock=lambda: float(next(t)),
    )
    assert [r.spec.model for r in rows] == ["a"]
    assert "time limit 250s" in rows[0].stopped

    class Paid(Spender):
        def complete(
            self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
        ) -> LLMResponse:
            return LLMResponse("Done.", usage=Usage(1, 1, cost=0.4))

    rows = run_leaderboard(
        [ModelSpec("a")], lambda _s: Paid(), CAPABILITY, ceilings=Ceilings(max_cost_usd=1.0)
    )
    assert "cost limit $1.00" in rows[0].stopped
    assert rows[0].cost == pytest.approx(1.2)


def test_worst_case_estimate() -> None:
    plain = sum(not c.gauntlet_only for c in ALL)
    assert runnable(ALL, gauntlet=False) == plain
    assert runnable(ALL, gauntlet=True) == plain + len(ALL)
    assert worst_case_tokens(10, ALL, True) == 10 * (plain + len(ALL)) * CASE_TOKENS


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return tmp_path


def test_big_estimate_needs_yes(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[str] = []

    def factory(name: str) -> LLM:
        calls.append(name)
        return Spender()

    specs = ["a", "b", "c"]
    assert worst_case_tokens(3, ALL, False) > CONFIRM_TOKENS
    base = ["models", *specs, "--no-record", "--scars", str(home / "none")]
    assert main(base, llm_factory=factory) == 2
    err = capsys.readouterr().err
    assert "worst case" in err
    assert "--yes" in err
    assert calls == []  # exits before any client is built or called
    assert main([*base, "--yes"], llm_factory=factory) == 0
    capsys.readouterr()
    assert main([*base, "--max-total-tokens", "1000"], llm_factory=factory) == 0  # capped
    assert "capped at 1,000" in capsys.readouterr().err


def test_cli_partial_row_is_not_recorded(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["models", "a", "b", "--max-total-tokens", "1000", "--scars", str(home / "none")]
    assert main(args, llm_factory=lambda _n: Spender()) == 0
    out = capsys.readouterr()
    assert "a (partial)" in out.out
    assert "not recorded" in out.err
    assert not (home / "home" / history.HISTORY_FILE).exists()


def test_cost_ceiling_needs_prices(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["models", "a", "--max-cost-usd", "1", "--scars", str(home / "none")]
    assert main(args, llm_factory=lambda _n: Spender()) == 2
    assert "price" in capsys.readouterr().err.lower()
