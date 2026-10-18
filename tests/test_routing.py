from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from sebastian import Sebastian
from sebastian.approval import AllowApprover, ApprovalGate
from sebastian.cli import main
from sebastian.clock import FakeClock, SeededIdSource
from sebastian.config import Config
from sebastian.llm import LLM, ScriptedLLM
from sebastian.tools import ToolRegistry, Workspace, file_tools
from sebastian.trace import MemoryTracer
from sebastian.types import LLMResponse, ToolCall


def miss(i: int) -> LLMResponse:
    return LLMResponse("try", (ToolCall(f"c{i}", "read_file", {"path": f"missing{i}"}),))


def test_planner_and_escalation_models_are_used(tmp_path: Path) -> None:
    planner = ScriptedLLM([LLMResponse("1. read\n2. answer")])
    worker = ScriptedLLM([miss(0), miss(1), miss(2), LLMResponse("done")])
    strong = ScriptedLLM([miss(3)])
    tracer = MemoryTracer()
    Sebastian(
        llm=worker,
        tools=ToolRegistry(file_tools(Workspace(tmp_path))),
        gate=ApprovalGate(AllowApprover()),
        tracer=tracer,
        clock=FakeClock(),
        ids=SeededIdSource(1),
        plan=True,
        planner_llm=planner,
        escalation_llm=strong,
    ).run("read things")
    assert len(planner.calls) == 1
    assert planner.calls[0][2] == ()
    assert len(strong.calls) == 1  # exactly the step after the nudge
    assert "<replan>" in strong.calls[0][1][-1].text
    assert len(worker.calls) == 4  # back to the worker afterwards
    assert tracer.names().count("escalated") == 1


def test_config_reads_routing_env() -> None:
    cfg = Config.from_env({"SEBASTIAN_PLAN_MODEL": "claude-opus-5-5"})
    assert cfg.plan_model == "claude-opus-5-5"
    assert cfg.escalation_model == ""


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(h))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return h


def factory(scripts: dict[str, list[LLMResponse]], seen: list[str]) -> Callable[[str], LLM]:
    def make(model: str) -> LLM:
        seen.append(model)
        return ScriptedLLM(scripts[model])

    return make


def test_cli_plan_model_implies_plan(tmp_path: Path, home: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    seen: list[str] = []
    scripts = {
        "claude-sonnet-5-5": [LLMResponse("done")],
        "claude-opus-5-5": [LLMResponse("1. answer")],
    }
    args = ["run", "t", "-w", str(ws), "--plan-model", "claude-opus-5-5", "--no-memory"]
    assert main(args, llm_factory=factory(scripts, seen)) == 0
    assert seen == ["claude-sonnet-5-5", "claude-opus-5-5"]
    events = [json.loads(x)["event"] for x in (home / "traces.jsonl").read_text().splitlines()]
    assert "plan" in events


def test_cli_same_model_routing_builds_one_client(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    seen: list[str] = []
    scripts = {"claude-sonnet-5-5": [LLMResponse("plan"), LLMResponse("done")]}
    args = [
        "run", "t", "-w", str(ws), "--no-memory",
        "--plan-model", "claude-sonnet-5-5", "--escalation-model", "claude-sonnet-5-5",
    ]  # fmt: skip
    assert main(args, llm_factory=factory(scripts, seen)) == 0
    assert seen == ["claude-sonnet-5-5"]


def test_cli_escalation_model(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    seen: list[str] = []
    scripts = {"claude-sonnet-5-5": [LLMResponse("done")], "claude-opus-5-5": []}
    args = ["run", "t", "-w", str(ws), "--no-memory", "--escalation-model", "claude-opus-5-5"]
    assert main(args, llm_factory=factory(scripts, seen)) == 0
    assert seen == ["claude-sonnet-5-5", "claude-opus-5-5"]
