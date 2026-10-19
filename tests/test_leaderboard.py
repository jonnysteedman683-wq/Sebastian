"""B39: `sebastian models` leaderboard, with scripted models (no network)."""

from __future__ import annotations

import io
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from sebastian import history
from sebastian.cli import main
from sebastian.evals.cases import ALL, CAPABILITY, INJECTION
from sebastian.evals.harness import EvalCase
from sebastian.evals.leaderboard import (
    LeaderboardError,
    ModelSpec,
    Row,
    parse_spec,
    rank,
    render,
    run_leaderboard,
)
from sebastian.llm import LLM, ScriptedLLM
from sebastian.types import LLMResponse

PLAIN = [c for c in ALL if not c.gauntlet_only]


def solver(cases: list[EvalCase]) -> Callable[[], LLM]:
    """A 'good' model: replays each case's offline script, in suite order."""
    scripts: Iterator[EvalCase] = iter(cases)
    return lambda: ScriptedLLM(next(scripts).script)


def failing() -> LLM:
    return ScriptedLLM([])  # every call errors: capability cases fail


def lazy() -> LLM:
    return ScriptedLLM([LLMResponse("Done.")] * 10)  # claims success, does nothing


@pytest.mark.parametrize(
    ("text", "spec"),
    [
        ("claude-sonnet-5-5", ModelSpec("claude-sonnet-5-5")),
        ("anthropic:claude-opus-5-5", ModelSpec("claude-opus-5-5", "anthropic")),
        ("openai:qwen3@http://localhost:11434/v1",
         ModelSpec("qwen3", "openai", "http://localhost:11434/v1")),
        ("qwen3:8b", ModelSpec("qwen3:8b")),  # a colon in the model name is not a backend
        ("openai:qwen3:8b", ModelSpec("qwen3:8b", "openai")),
    ],
)  # fmt: skip
def test_parse_spec(text: str, spec: ModelSpec) -> None:
    assert parse_spec(text) == spec


@pytest.mark.parametrize(
    ("text", "why"),
    [("openai:", "no model name"), ("  ", "no model name"), ("m@http://x", "needs the openai")],
)
def test_parse_spec_errors(text: str, why: str) -> None:
    with pytest.raises(LeaderboardError, match=why):
        parse_spec(text)


def test_labels() -> None:
    assert ModelSpec("m").label == "m"
    assert ModelSpec("m", "openai").label == "openai:m"


def test_ranking_orders_by_score_then_resistance_then_tokens() -> None:
    cases = CAPABILITY[:2] + INJECTION[:2]
    makers = {"good": solver(cases), "bad": failing, "lazy": lazy}
    specs = [ModelSpec(n) for n in ("bad", "lazy", "good")]
    rows = run_leaderboard(specs, lambda s: makers[s.model](), cases)
    assert [r.spec.model for r in rows] == ["good", "bad", "lazy"]
    good, bad, lazy_row = rows
    assert good.score == 1.0
    assert good.resistance == 0.0  # the offline scripts play a fooled model; the gate held
    assert (bad.score, bad.resistance) == (0.5, 1.0)  # erroring = never tried an injection
    assert lazy_row.score == 0.5
    assert lazy_row.gauntlet_score is None
    assert lazy_row.critic is None


def test_token_tiebreak() -> None:
    from sebastian.evals.harness import CaseResult, SuiteResult

    def row(name: str, tokens: int) -> Row:
        return Row(
            ModelSpec(name), SuiteResult([CaseResult("c", True, False, True, "x", 1, tokens)])
        )

    assert [r.spec.model for r in rank([row("pricey", 900), row("cheap", 10)])] == [
        "cheap",
        "pricey",
    ]


def test_gauntlet_columns() -> None:
    cases = CAPABILITY[:1]
    gauntlet_model = ScriptedLLM(
        [
            *CAPABILITY[0].script,  # plain run
            LLMResponse("1. write"),  # gauntlet: plan
            LLMResponse("<checks>\nfile: hello.txt\n</checks>"),  # predict
            *CAPABILITY[0].script,  # act
            LLMResponse("VERDICT: PASS"),  # critic
        ]
    )
    rows = run_leaderboard([ModelSpec("m")], lambda _s: gauntlet_model, cases, gauntlet=True)
    assert rows[0].gauntlet_score == 1.0
    assert rows[0].critic is None  # no critic-attack case in this subset
    text = render(rows)
    assert " 1  m " in text
    assert "100%" in text


@pytest.mark.parametrize(
    ("specs", "why"),
    [([], "at least one"), ([ModelSpec("a")] * 2, "only once"),
     ([ModelSpec(f"m{i}") for i in range(11)], "at most 10")],
)  # fmt: skip
def test_run_limits(specs: list[ModelSpec], why: str) -> None:
    with pytest.raises(LeaderboardError, match=why):
        run_leaderboard(specs, lambda _s: failing(), CAPABILITY[:1])


def test_render_shows_dashes_for_missing_columns() -> None:
    rows = run_leaderboard([ModelSpec("x")], lambda _s: failing(), CAPABILITY[:1])
    line = render(rows).splitlines()[2]
    assert line.split()[:3] == ["1", "x", "0%"]
    assert line.split()[3:6] == ["-", "-", "-"]


def test_cli_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    good = solver([PLAIN[0], *PLAIN])  # the first client is the preflight check's (P2-12)
    makers = {"good-model": good, "bad-model": failing}
    args = ["models", "bad-model", "good-model", "--scars", str(tmp_path / "none")]
    assert main(args, llm_factory=lambda name: makers[name]()) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[2].split()[:3] == ["1", "good-model", "100%"]
    assert out[3].split()[:2] == ["2", "bad-model"]
    records = history.load(tmp_path / "home" / history.HISTORY_FILE)
    assert sorted(r.model for r in records) == ["bad-model", "good-model"]
    assert {r.kind for r in records} == {"eval"}


def test_cli_models_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    assert main(["models", "m@http://x", "--no-record"], llm_factory=lambda _n: failing()) == 2
    assert "needs the openai" in capsys.readouterr().err
    scars = tmp_path / "scars"
    scars.mkdir()
    (scars / "bad.json").write_text("{nope")
    assert main(["models", "m", "--scars", str(scars)], llm_factory=lambda _n: failing()) == 2


def test_cli_models_real_factory_uses_make_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sebastian import cli

    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SEBASTIAN_BACKEND", "openai")
    seen: list[tuple[str, str, str]] = []

    def fake(model: str, backend: str, base_url: str) -> LLM:
        seen.append((model, backend, base_url))
        return failing()

    monkeypatch.setattr(cli, "make_llm", fake)
    args = ["models", "anthropic:claude-x", "openai:local@http://localhost:1/v1", "plain",
            "--no-record", "--scars", str(tmp_path / "none")]  # fmt: skip
    assert cli.main(args) == 0
    capsys.readouterr()
    assert set(seen) == {
        ("claude-x", "anthropic", ""),
        ("local", "openai", "http://localhost:1/v1"),
        ("plain", "openai", ""),  # no prefix: SEBASTIAN_BACKEND
    }
