"""B34: evolve's inner agent run under the Gauntlet."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from sebastian.cli import main
from sebastian.evolve import EvolveConfig, evolve_once
from sebastian.gauntlet import GauntletPolicy
from sebastian.llm import ScriptedLLM
from sebastian.trace import MemoryTracer
from sebastian.types import LLMResponse
from test_evolve import CHECK_GREET, branches, cfg, git, repo, write

__all__ = ["repo"]  # the evolve repo fixture, not the conftest one

PLAN = LLMResponse("1. create greet.txt")
PREDICT = LLMResponse("<checks>\nfile: greet.txt\n</checks>")
PASS = LLMResponse("VERDICT: PASS")
FAIL = LLMResponse("VERDICT: FAIL\n- greet.txt is missing")


def go(*steps: LLMResponse) -> ScriptedLLM:
    return ScriptedLLM([PLAN, PREDICT, *steps])


def act(cid: str, path: str, content: str) -> LLMResponse:
    return LLMResponse("go", (write(cid, path, content),))


def events(tracer: MemoryTracer, name: str) -> list[dict[str, object]]:
    return [e for e in tracer.events if e["event"] == name]


def test_inner_run_passes_verify_and_critique_then_the_outer_gates(
    repo: Path, tmp_path: Path
) -> None:
    tracer = MemoryTracer()
    llm = go(act("1", "greet.txt", "hello"), LLMResponse("done"), PASS)
    report = evolve_once(repo, llm, cfg(tmp_path, gauntlet=GauntletPolicy()), tracer=tracer)
    assert report.accepted, report.summary()
    assert report.run_stop == "completed"
    assert [e["passed"] for e in events(tracer, "gauntlet_verify")] == [True]
    assert [e["passed"] for e in events(tracer, "gauntlet_critique")] == [True]
    assert "+hello" in llm.calls[4][1][0].text  # the critic reviewed the worktree diff
    assert "gauntlet" not in report.gates  # only recorded when it blocks
    assert git(repo, "show", f"{report.branch}:greet.txt") == "hello"


def test_needs_help_inner_run_is_rejected_before_the_outer_gates(
    repo: Path, tmp_path: Path
) -> None:
    tracer = MemoryTracer()
    llm = go(act("1", "greet.txt", "hello"), LLMResponse("done"), FAIL)
    config = cfg(tmp_path, gauntlet=GauntletPolicy(max_loops=1))
    report = evolve_once(repo, llm, config, tracer=tracer)
    assert not report.accepted
    assert report.run_stop == "needs_help"
    assert report.gates == {"gauntlet": False}  # no check or eval ran
    assert "needs help" in report.reason
    assert report.branch not in branches(repo)
    assert report.patch is not None
    assert "greet.txt" in report.patch.read_text()  # the attempt is kept for inspection
    assert events(tracer, "gauntlet_help")


def test_revert_keeps_only_the_final_attempt(repo: Path, tmp_path: Path) -> None:
    llm = go(
        act("1", "junk.txt", "wrong"),
        LLMResponse("done"),
        FAIL,
        act("2", "greet.txt", "hello"),
        LLMResponse("done"),
        PASS,
    )
    config = cfg(tmp_path, gauntlet=GauntletPolicy(revert_on_loop=True))
    report = evolve_once(repo, llm, config)
    assert report.accepted, report.summary()
    assert report.changed == ["greet.txt"]  # junk.txt was rolled back before the retry


def test_separate_critic_model(repo: Path, tmp_path: Path) -> None:
    critic = ScriptedLLM([PASS])
    llm = go(act("1", "greet.txt", "hello"), LLMResponse("done"))
    report = evolve_once(repo, llm, cfg(tmp_path, gauntlet=GauntletPolicy(critic_llm=critic)))
    assert report.accepted
    assert len(critic.calls) == 1


def test_off_by_default(repo: Path, tmp_path: Path) -> None:
    assert EvolveConfig().gauntlet is None
    tracer = MemoryTracer()
    llm = ScriptedLLM([PLAN, act("1", "greet.txt", "hello"), LLMResponse("done")])
    assert evolve_once(repo, llm, cfg(tmp_path), tracer=tracer).accepted
    assert not events(tracer, "gauntlet_predict")


@pytest.mark.parametrize(
    ("flags", "models"),
    [
        (["--gauntlet"], ["claude-sonnet-5-5"]),
        (["--gauntlet-revert"], ["claude-sonnet-5-5"]),
        (["--critic-model", "claude-opus-5-5"], ["claude-sonnet-5-5", "claude-opus-5-5"]),
        (["--critic-model", "claude-sonnet-5-5"], ["claude-sonnet-5-5"]),
    ],
)
def test_cli_evolve_gauntlet_flags(
    repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: list[str],
    models: list[str],
) -> None:
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    import sebastian.evolve as evo

    real = evo.evolve_once
    seen_policy: list[GauntletPolicy | None] = []

    def fast(repo: Path, llm: object, config: EvolveConfig, **kw: object) -> object:
        config.checks = [CHECK_GREET]
        config.metrics_cmd = None
        seen_policy.append(config.gauntlet)
        return real(repo, llm, config, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(evo, "evolve_once", fast)
    separate = len(models) == 2
    actor = go(act("1", "greet.txt", "hello"), LLMResponse("done"), *([] if separate else [PASS]))
    clients = {"claude-sonnet-5-5": actor, "claude-opus-5-5": ScriptedLLM([PASS])}
    seen: list[str] = []

    def factory(model: str) -> ScriptedLLM:
        seen.append(model)
        return clients[model]

    unsafe = ["--sandbox", "none", "--unsafe-no-sandbox"]  # no bubblewrap on CI hosts
    code = main(["evolve", "--repo", str(repo), "--no-evals", *unsafe, *flags], llm_factory=factory)
    assert code == 0, capsys.readouterr().out
    assert seen == models
    [policy] = seen_policy
    assert policy is not None
    assert policy.revert_on_loop == ("--gauntlet-revert" in flags)
