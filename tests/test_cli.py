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
from sebastian.clock import FakeClock
from sebastian.llm import LLM, ScriptedLLM
from sebastian.trace import JsonlTracer
from sebastian.types import LLMResponse, ToolCall, Usage


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
        [
            "run",
            "t",
            "-w",
            str(ws),
            "--allow-cmd",
            "pytest",
            "--unattended",
            "--no-memory",
            # P1-01: no bwrap in CI means auto refuses; opt out explicitly for this test.
            "--sandbox",
            "none",
            "--allow-network",
        ],
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


def _write_traces(home: Path, runs: int, junk: bool = False) -> Path:
    path = home / "traces.jsonl"
    tracer = JsonlTracer(path, FakeClock(1_760_000_000.0))
    for i in range(runs):
        rid = f"run{i}"
        tracer.emit(rid, "run_start", task=f"task {i}", recalled=0)
        tracer.emit(
            rid, "llm_step", step=1, text="", tool_calls=[], usage={"input": 5, "output": 5}
        )
        usage = {"input": 10 + i, "output": 5}
        tracer.emit(rid, "run_end", stop="completed", steps=2, detail="", usage=usage)
        if junk and i == 0:
            with path.open("a") as fh:
                fh.write('{not json\n[1, 2]\n{"event": "run_end"}\n\n')
    return path


def test_traces_missing_or_empty_file(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["traces"]) == 0
    assert capsys.readouterr().out.strip() == "(no runs)"
    home.mkdir(parents=True, exist_ok=True)
    (home / "traces.jsonl").write_text("")
    assert main(["traces"]) == 0
    assert capsys.readouterr().out.strip() == "(no runs)"


def test_traces_lists_last_n_runs(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_traces(home, 3)
    assert main(["traces", "-n", "2"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "run1  2025-10-09T08:53:20+00:00  completed  steps=2  tokens=16",
        "run2  2025-10-09T08:53:20+00:00  completed  steps=2  tokens=17",
    ]


def test_traces_n_larger_than_runs_and_unfinished(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _write_traces(home, 1)
    with path.open("a") as fh:
        fh.write(json.dumps({"t": 1_760_000_000.0, "run_id": "live", "event": "run_start"}) + "\n")
    assert main(["traces", "-n", "50"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        "run0  2025-10-09T08:53:20+00:00  completed  steps=2  tokens=15",
        "live  2025-10-09T08:53:20+00:00  running  steps=0  tokens=0",
    ]


def test_traces_skips_malformed_lines(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_traces(home, 2, junk=True)
    assert main(["traces"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in out] == ["run0", "run1"]


def test_traces_rejects_bad_limit(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["traces", "-n", "0"])


def test_traces_after_real_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    script = [LLMResponse("done", (), Usage(12, 8))]
    assert main(["run", "t", "-w", str(ws), "--no-memory"], llm_factory=factory(script, [])) == 0
    capsys.readouterr()
    assert main(["traces"]) == 0
    out = capsys.readouterr().out.split()
    assert out[2:] == ["completed", "steps=1", "tokens=20"]


@pytest.mark.parametrize("argv", [["redteam", "-n", "1"], ["run", "say hi", "-w", "{ws}"]])
def test_missing_api_key_is_one_line_exit_2(
    argv: list[str],
    home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # B77: no key used to end in an SDK TypeError traceback on the first model call
    pytest.importorskip("anthropic")
    for name in list(os.environ):
        if name.startswith(("ANTHROPIC_", "SEBASTIAN_BACKEND")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    ws = tmp_path / "ws"
    ws.mkdir()
    assert main([a.format(ws=ws) for a in argv]) == 2
    err = capsys.readouterr().err.strip()
    assert "ANTHROPIC_API_KEY" in err
    assert "Traceback" not in err
    assert len(err.splitlines()) == 1


def test_demo_usage_out_writes_the_spend_contract(tmp_path: Path) -> None:
    # Bastion G1b-2: the summon workflow hands this file to `tools.summon finish --usage`
    ws, out = tmp_path / "ws", tmp_path / "usage.json"
    ws.mkdir()
    assert main(["demo", "-w", str(ws), "--usage-out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "model": "demo",
        "input_tokens": 0,
        "output_tokens": 0,
    }


def test_run_usage_out_records_tokens_model_and_cost(tmp_path: Path, home: Path) -> None:
    ws, out = tmp_path / "ws", tmp_path / "usage.json"
    ws.mkdir()
    script = [LLMResponse("done", (), Usage(120, 30))]
    argv = ["run", "t", "-w", str(ws), "--model", "claude-haiku-5-5", "--usage-out", str(out)]
    assert main(argv, llm_factory=factory(script, [])) == 0
    usage = json.loads(out.read_text(encoding="utf-8"))
    assert usage["model"] == "claude-haiku-5-5"
    assert (usage["input_tokens"], usage["output_tokens"]) == (120, 30)
    assert "cost_usd" not in usage  # no price known: absent, never a false $0


def test_usage_out_write_failure_warns_and_keeps_the_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    bad = tmp_path / "missing-dir" / "usage.json"
    assert main(["demo", "-w", str(ws), "--usage-out", str(bad)]) == 0
    assert "usage-out" in capsys.readouterr().err
