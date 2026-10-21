"""P2-12: preflight, per-model isolation and incremental recording for `sebastian models`."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from sebastian import history
from sebastian.cli import main
from sebastian.evals.cases import CAPABILITY
from sebastian.evals.harness import SuiteResult
from sebastian.evals.leaderboard import ModelSpec, Row, preflight, rank, render, run_leaderboard
from sebastian.llm import RetryingLLM
from sebastian.llm.base import LLM, LLMError
from sebastian.llm.openai_compat import OpenAICompatLLM
from sebastian.llm.scripted import ScriptedLLM
from sebastian.types import LLMResponse


def lazy() -> LLM:
    return ScriptedLLM([LLMResponse("Done.")] * 10)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return tmp_path


def _models(home: Path, *specs: str) -> list[str]:
    return ["models", *specs, "--scars", str(home / "none")]


def test_bad_url_in_third_spec_fails_preflight_before_any_case(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[str] = []

    def factory(name: str) -> LLM:
        calls.append(name)
        if name == "c":
            return OpenAICompatLLM(name, "http://example.com/v1")  # plain http to remote
        return lazy()

    assert main(_models(home, "a", "b", "c"), llm_factory=factory) == 2
    err = capsys.readouterr().err
    assert "preflight failed" in err
    assert "c: LLMError" in err
    assert "plain http" in err
    assert calls == ["a", "b", "c"]  # one client each, no case run
    assert not (home / "home" / history.HISTORY_FILE).exists()


def test_a_crashing_model_is_isolated(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    built: dict[str, int] = {}

    def factory(name: str) -> LLM:
        built[name] = built.get(name, 0) + 1
        if name == "m2" and built[name] > 1:  # passes preflight, then breaks mid-run
            raise RuntimeError("backend fell over")
        return lazy()

    assert main(_models(home, "m1", "m2", "m3"), llm_factory=factory) == 0
    out = capsys.readouterr()
    table = out.out.splitlines()
    assert table[-2].split()[:4] == ["3", "m2", "error:", "RuntimeError:"]
    assert "[2/3] m2: error: RuntimeError: backend fell over" in out.err
    records = history.load(home / "home" / history.HISTORY_FILE)
    assert sorted(r.model for r in records) == ["m1", "m3"]


def test_empty_openai_key_is_a_preflight_error(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "")

    def factory(name: str) -> LLM:
        return RetryingLLM(OpenAICompatLLM(name, "https://api.openai.com/v1"))

    assert main(_models(home, "openai:gpt-x"), llm_factory=factory) == 2
    assert "OPENAI_API_KEY is empty" in capsys.readouterr().err
    assert not (home / "home" / history.HISTORY_FILE).exists()


def test_ctrl_c_keeps_finished_models(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    built: dict[str, int] = {}

    def factory(name: str) -> LLM:
        built[name] = built.get(name, 0) + 1
        if name == "m2" and built[name] > 1:
            raise KeyboardInterrupt
        return lazy()

    assert main(_models(home, "m1", "m2", "m3"), llm_factory=factory) == 130
    out = capsys.readouterr()
    assert "interrupted after 1/3" in out.err
    assert out.out.splitlines()[2].split()[:2] == ["1", "m1"]
    records = history.load(home / "home" / history.HISTORY_FILE)
    assert {r.model for r in records} == {"m1"}
    assert built["m3"] == 1  # only its preflight client; nothing ran after the interrupt


def test_preflight_ping_calls_each_model_once() -> None:
    pinged: list[str] = []

    class Pong:
        def __init__(self, name: str) -> None:
            self.name = name

        def complete(self, *args: object) -> LLMResponse:
            pinged.append(self.name)
            return LLMResponse("OK")

    specs = [ModelSpec("a"), ModelSpec("b")]
    assert preflight(specs, lambda s: Pong(s.model), ping=True) == []
    assert pinged == ["a", "b"]
    assert preflight(specs, lambda s: Pong(s.model)) == []
    assert pinged == ["a", "b"]  # no ping unless asked


def test_ping_failure_is_reported() -> None:
    def broken(_s: ModelSpec) -> LLM:
        return ScriptedLLM([])

    problems = preflight([ModelSpec("x")], broken, ping=True)
    assert len(problems) == 1
    assert problems[0].startswith("x: ")


def test_run_leaderboard_isolates_and_ranks_errors_last() -> None:
    seen: list[str] = []

    def factory(spec: ModelSpec) -> LLM:
        if spec.model == "bad":
            raise LLMError("no SDK")
        return lazy()

    rows = run_leaderboard(
        [ModelSpec("bad"), ModelSpec("ok")],
        factory,
        CAPABILITY,
        on_row=lambda r: seen.append(r.spec.model),
    )
    assert [r.spec.model for r in rows] == ["ok", "bad"]
    assert rows[1].error == "LLMError: no SDK"
    assert seen == ["bad", "ok"]  # in run order, as each finished


def test_error_row_outranks_nothing() -> None:
    good = Row(ModelSpec("g"), SuiteResult())
    broken = Row(ModelSpec("b"), SuiteResult(), error="boom")
    assert rank([broken, good]) == [good, broken]
    assert "error: boom" in render([good, broken])
