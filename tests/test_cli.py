from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from sebastian import __version__
from sebastian.cli import main
from sebastian.llm import LLM, ScriptedLLM
from sebastian.types import LLMResponse, ToolCall


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(h))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))  # not a TTY -> deny approver
    return h


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == __version__


def test_demo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    assert main(["demo", "-w", str(ws)]) == 0
    assert (ws / "hello.txt").read_text() == "hello"
    assert "[completed]" in capsys.readouterr().err


def factory(script: list[LLMResponse], seen: list[str]) -> Callable[[str], LLM]:
    def make(model: str) -> LLM:
        seen.append(model)
        return ScriptedLLM(script)

    return make


def test_run_non_tty_denies_writes_and_traces(tmp_path: Path, home: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    seen: list[str] = []
    script = [
        LLMResponse("", (ToolCall("1", "write_file", {"path": "x", "content": "y"}),)),
        LLMResponse("blocked"),
    ]
    code = main(
        ["run", "write x", "-w", str(ws), "--model", "claude-haiku-5-5"],
        llm_factory=factory(script, seen),
    )
    assert code == 0
    assert seen == ["claude-haiku-5-5"]
    assert not (ws / "x").exists()
    events = [
        json.loads(line)["event"] for line in (home / "traces.jsonl").read_text().splitlines()
    ]
    assert "approval_denied" in events
    assert (home / "memory.json").exists()


def test_run_registers_command_tool_and_no_memory(tmp_path: Path, home: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    made: list[ScriptedLLM] = []

    def make(model: str) -> LLM:
        made.append(ScriptedLLM([]))
        return made[0]

    code = main(
        ["run", "t", "-w", str(ws), "--allow-cmd", "pytest", "--unattended", "--no-memory"],
        llm_factory=make,
    )
    assert code == 1  # scripted LLM exhausted -> error stop
    assert "run_command" in made[0].calls[0][2]
    assert not (home / "memory.json").exists()


def test_run_interactive_approver_when_tty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    class TTY(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", TTY(""))
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    script = [
        LLMResponse("", (ToolCall("1", "write_file", {"path": "x", "content": "y"}),)),
        LLMResponse("done"),
    ]
    assert main(["run", "t", "-w", str(ws)], llm_factory=factory(script, [])) == 0
    assert (ws / "x").read_text() == "y"


def test_run_default_backend_is_anthropic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("anthropic")
    ws = tmp_path / "ws"
    ws.mkdir()
    built: list[str] = []

    class FakeAnthropic:
        def __init__(self, model: str) -> None:
            built.append(model)

        def complete(self, *_: object) -> LLMResponse:
            return LLMResponse("ok")

    monkeypatch.setattr("sebastian.llm.anthropic.AnthropicLLM", FakeAnthropic)
    monkeypatch.setenv("SEBASTIAN_MODEL", "claude-opus-5-5")
    assert main(["run", "t", "-w", str(ws)]) == 0
    assert built == ["claude-opus-5-5"]


def test_module_entrypoint_exists() -> None:
    assert os.path.exists(Path(__file__).parents[1] / "src" / "sebastian" / "__main__.py")
