"""B40: prices, dollar accounting and --max-cost."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from sebastian import Budget, Sebastian, StopReason
from sebastian.budget import Governor
from sebastian.cli import main
from sebastian.clock import FakeClock, SeededIdSource
from sebastian.config import Config
from sebastian.dashboard import Snapshot, render
from sebastian.llm import LLM, ScriptedLLM
from sebastian.pricing import (
    Price,
    PricedLLM,
    PricingError,
    load_prices,
    parse_prices,
    priced,
    require_prices,
)
from sebastian.tools import ToolRegistry
from sebastian.trace import MemoryTracer, RunSummary, summarize_runs
from sebastian.types import LLMResponse, Message, Role, Usage

PRICE = Price(input=3.0, output=15.0)


def resp(i: int, o: int, text: str = "x", *calls: Any) -> LLMResponse:
    return LLMResponse(text, calls, Usage(i, o))


def test_price_cost() -> None:
    assert PRICE.cost(Usage(1_000_000, 0)) == 3.0
    assert PRICE.cost(Usage(100_000, 20_000)) == pytest.approx(0.3 + 0.3)
    assert Price(0, 0).cost(Usage(5, 5)) == 0


def test_usage_adds_cost() -> None:
    assert (Usage(1, 2, 0.5) + Usage(3, 4, 0.25)) == Usage(4, 6, 0.75)


def test_priced_llm_fills_in_cost_and_unknown_models_pass_through() -> None:
    inner = ScriptedLLM([resp(1_000_000, 100_000)])
    wrapped = priced(inner, "m", {"m": PRICE})
    assert isinstance(wrapped, PricedLLM)
    out = wrapped.complete("s", [Message(Role.USER, text="t")], [])
    assert out.usage.cost == pytest.approx(4.5)
    other = ScriptedLLM([])
    assert priced(other, "unknown", {"m": PRICE}) is other


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ([], "JSON object"),
        ({"": {"input": 1, "output": 1}}, "non-empty"),
        ({"m": {"input": 1}}, "exactly 'input' and 'output'"),
        ({"m": {"input": 1, "output": 1, "x": 2}}, "exactly"),
        ({"m": {"input": "1", "output": 1}}, "must be a number"),
        ({"m": {"input": True, "output": 1}}, "must be a number"),
        ({"m": {"input": -1, "output": 1}}, "between 0"),
        ({"m": {"input": 1e9, "output": 1}}, "between 0"),
        ({"m": {"input": float("nan"), "output": 1}}, "between 0"),
    ],
)
def test_parse_prices_rejects(raw: Any, why: str) -> None:
    with pytest.raises(PricingError, match=why):
        parse_prices(raw)


def test_load_prices(tmp_path: Path) -> None:
    assert load_prices(tmp_path) == {}
    (tmp_path / "prices.json").write_text('{"m": {"input": 3, "output": 15}, "local": '
                                          '{"input": 0, "output": 0}}')  # fmt: skip
    assert load_prices(tmp_path) == {"m": PRICE, "local": Price(0, 0)}
    (tmp_path / "prices.json").write_text("{nope")
    with pytest.raises(PricingError, match="invalid JSON"):
        load_prices(tmp_path)
    (tmp_path / "prices.json").write_text('{"m": 1}')
    with pytest.raises(PricingError, match=r"prices\.json: m"):
        load_prices(tmp_path)


def test_require_prices() -> None:
    require_prices(["m"], {"m": PRICE})
    with pytest.raises(PricingError, match="needs a price for a, b"):
        require_prices(["b", "m", "a", "a"], {"m": PRICE})


def test_budget_validates_and_governor_stops_on_cost() -> None:
    with pytest.raises(ValueError, match="max_cost"):
        Budget(max_cost=-1)
    gov = Governor(Budget(max_cost=1.0), FakeClock())
    gov.record_step(Usage(10, 10, 0.79))
    assert gov.new_warnings() == []
    assert gov.exhausted() is None
    gov.record_step(Usage(10, 10, 0.02))
    assert [w[0] for w in gov.new_warnings()] == ["cost"]
    gov.record_step(Usage(1, 1, 0.5))
    assert gov.exhausted() == "cost limit $1.00 reached"
    assert Governor(Budget(), FakeClock()).exhausted() is None  # no dollar limit by default


def test_run_stops_at_the_dollar_limit_and_traces_cost() -> None:
    # The planning call alone costs $1.50 (500k input tokens at $3/M): over a $1 limit.
    model = ScriptedLLM([resp(500_000, 0, "plan"), resp(10, 0, "done")])
    tracer = MemoryTracer()
    agent = Sebastian(
        llm=PricedLLM(model, PRICE),
        tools=ToolRegistry([]),
        budget=Budget(max_tokens=10_000_000, max_cost=1.0),
        tracer=tracer,
        clock=FakeClock(),
        ids=SeededIdSource(1),
        plan=True,
    )
    result = agent.run("t")
    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert result.detail == "cost limit $1.00 reached"
    assert result.usage.cost == pytest.approx(1.5)
    assert len(model.calls) == 1  # stopped before spending more
    end = next(e for e in tracer.events if e["event"] == "run_end")
    assert end["usage"]["cost"] == pytest.approx(1.5)
    plan = next(e for e in tracer.events if e["event"] == "plan")
    assert plan["usage"] == {"input": 500_000, "output": 0, "cost": 1.5}


def test_unpriced_runs_keep_the_old_trace_shape() -> None:
    tracer = MemoryTracer()
    Sebastian(
        llm=ScriptedLLM([resp(3, 4, "done")]), tools=ToolRegistry([]), tracer=tracer,
        clock=FakeClock(), ids=SeededIdSource(1),
    ).run("t")  # fmt: skip
    end = next(e for e in tracer.events if e["event"] == "run_end")
    assert end["usage"] == {"input": 3, "output": 4}


def test_summary_reads_cost(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    lines = [
        {"run_id": "a", "event": "run_end", "stop": "completed", "steps": 1,
         "usage": {"input": 5, "output": 5, "cost": 0.25}},
        {"run_id": "b", "event": "run_end", "stop": "completed", "steps": 1,
         "usage": {"input": 1, "output": 1, "cost": "lots"}},
        {"run_id": "c", "event": "run_end", "stop": "completed", "steps": 1,
         "usage": {"input": 1, "output": 1, "cost": True}},
    ]  # fmt: skip
    path.write_text("\n".join(json.dumps(x) for x in lines))
    runs = summarize_runs(path, 10)
    assert [(r.tokens, r.cost) for r in runs] == [(10, 0.25), (2, 0.0), (2, 0.0)]


def test_dashboard_spend_tile() -> None:
    priced_run = render(Snapshot([], [RunSummary("r", 1.0, "completed", 1, 10, 1.234)], [], 2.0))
    assert "$1.23" in priced_run
    unpriced = render(Snapshot([], [RunSummary("r", 1.0, "completed", 1, 10)], [], 2.0))
    assert "add prices.json" in unpriced


def test_config_max_cost() -> None:
    assert Config.from_env({"SEBASTIAN_MAX_COST": "2.5"}).max_cost == 2.5
    assert Config.from_env({}).max_cost == 0.0
    for bad in ("x", "-1", "inf"):
        with pytest.raises(ValueError, match="SEBASTIAN_MAX_COST"):
            Config.from_env({"SEBASTIAN_MAX_COST": bad})


# --- CLI -------------------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("SEBASTIAN_HOME", str(h))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return h


def scripted(_name: str) -> LLM:
    return ScriptedLLM([resp(200_000, 10_000, "done")])


def test_cli_prices_runs_and_shows_cost_in_traces(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (home / "prices.json").write_text('{"claude-sonnet-5-5": {"input": 3, "output": 15}}')
    ws = tmp_path / "ws"
    ws.mkdir()
    assert main(["run", "t", "-w", str(ws), "--no-memory", "--max-cost", "5"],
                llm_factory=scripted) == 0  # fmt: skip
    capsys.readouterr()
    assert main(["traces"]) == 0
    assert "cost=$0.7500" in capsys.readouterr().out


def test_cli_max_cost_needs_prices(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    args = ["run", "t", "-w", str(ws), "--no-memory", "--max-cost", "1"]
    assert main(args, llm_factory=scripted) == 2
    assert "needs a price for claude-sonnet-5-5" in capsys.readouterr().err
    assert main(["run", "t", "-w", str(ws), "--no-memory"], llm_factory=scripted) == 0


def test_cli_bad_price_file_and_bad_flag(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (home / "prices.json").write_text("[]")
    assert main(["traces"]) == 2
    assert "JSON object" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["run", "t", "--max-cost", "-1"])
    with pytest.raises(SystemExit):
        main(["run", "t", "--max-cost", "lots"])


def test_cli_models_are_priced(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sebastian import cli
    from sebastian.evals.harness import CaseResult, SuiteResult

    (home / "prices.json").write_text('{"m": {"input": 3, "output": 15}}')
    built: list[LLM] = []

    def fake_run(specs: Any, factory: Any, cases: Any, gauntlet: bool = False) -> list[Any]:
        from sebastian.evals.leaderboard import Row

        built.append(factory(specs[0]))
        return [Row(specs[0], SuiteResult([CaseResult("c", True, False, True, "x", 1, 1)]))]

    monkeypatch.setattr("sebastian.evals.leaderboard.run_leaderboard", fake_run)
    args = ["models", "m", "--no-record", "--scars", str(tmp_path / "none")]
    assert cli.main(args, llm_factory=lambda _n: ScriptedLLM([])) == 0
    assert isinstance(built[0], PricedLLM)
