"""B57: ``sebastian run --task-file``."""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import pytest

from sebastian.cli import _apply_task_file, main
from sebastian.config import Config
from sebastian.llm import LLM, ScriptedLLM
from sebastian.taskfile import MAX_BYTES, TaskFile, TaskFileError, load, parse
from sebastian.types import LLMResponse, ToolCall

FULL = """---
# budgets for this task
allow_cmd: [pytest, "ruff"]
max_steps: 40
max_tokens: 90000
max_cost: 0.5
model: 'claude-haiku-5-5'

gauntlet: yes
---

Fix the failing parser test.
Keep ruff clean.
"""


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(h))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return h


def test_parse_full_front_matter() -> None:
    assert parse(FULL) == TaskFile(
        task="Fix the failing parser test.\nKeep ruff clean.",
        allow_cmd=("pytest", "ruff"),
        max_steps=40,
        max_tokens=90_000,
        max_cost=0.5,
        model="claude-haiku-5-5",
        gauntlet=True,
    )


def test_parse_plain_text_and_variants() -> None:
    assert parse("  Just do it.\n") == TaskFile("Just do it.")
    tf = parse("---\nallow_cmd: pytest\ngauntlet: off\n---\nGo")
    assert tf.allow_cmd == ("pytest",)
    assert tf.gauntlet is False


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("---\nmodel: m\nGo", "no closing ---"),
        ("---\nsandbox: none\n---\nGo", "line 2: unknown key 'sandbox'"),
        ("---\napprove: all\n---\nGo", "unknown key 'approve'"),
        ("---\nmax_steps: 3\nmax_steps: 4\n---\nGo", "line 3: max_steps is set twice"),
        ("---\njust words\n---\nGo", "line 2: expected 'key: value'"),
        ("---\n: x\n---\nGo", "expected 'key: value'"),
        ("---\nmodel:\n---\nGo", "model has no value"),
        ("---\nmodel: ''\n---\nGo", "model is empty"),
        ("---\nmax_steps: 0\n---\nGo", "max_steps must be >= 1"),
        ("---\nmax_tokens: lots\n---\nGo", "max_tokens must be an integer"),
        ("---\nmax_cost: -1\n---\nGo", "max_cost must be zero or more"),
        ("---\nmax_cost: inf\n---\nGo", "max_cost must be zero or more"),
        ("---\nmax_cost: cheap\n---\nGo", "max_cost must be a number"),
        ("---\nallow_cmd: [ ]\n---\nGo", "allow_cmd must list executable names"),
        ("---\nallow_cmd: rm -rf\n---\nGo", "allow_cmd must list executable names"),
        ("---\ngauntlet: maybe\n---\nGo", "gauntlet must be true or false"),
        ("---\nmodel: m\n---\n   \n", "no task text"),
        ("", "no task text"),
    ],
)
def test_parse_errors(text: str, error: str) -> None:
    with pytest.raises(TaskFileError, match=error.replace("[", r"\[")):
        parse(text)


def test_load_errors(tmp_path: Path) -> None:
    with pytest.raises(TaskFileError, match="cannot read"):
        load(tmp_path / "missing.md")
    big = tmp_path / "big.md"
    big.write_bytes(b"x" * (MAX_BYTES + 1))
    with pytest.raises(TaskFileError, match="larger than"):
        load(big)
    latin = tmp_path / "latin.md"
    latin.write_bytes("café".encode("latin-1"))
    with pytest.raises(TaskFileError, match="not UTF-8"):
        load(latin)
    bad = tmp_path / "bad.md"
    bad.write_text("---\nfoo: 1\n---\nGo")
    with pytest.raises(TaskFileError, match=r"bad\.md: line 2: unknown key 'foo'"):
        load(bad)


def _args(task_file: Path, **flags: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "task": None,
        "task_file": task_file,
        "allow_cmd": [],
        "model": None,
        "max_cost": None,
        "gauntlet": False,
    }
    return argparse.Namespace(**{**base, **flags})


def test_file_fills_args_and_budgets(tmp_path: Path) -> None:
    f = tmp_path / "TASK.md"
    f.write_text(FULL)
    args = _args(f)
    cfg = _apply_task_file(Config(), args)
    assert args.task == "Fix the failing parser test.\nKeep ruff clean."
    assert args.allow_cmd == ["pytest", "ruff"]
    assert args.model == "claude-haiku-5-5"
    assert args.max_cost == 0.5
    assert args.gauntlet is True
    assert (cfg.max_steps, cfg.max_tokens) == (40, 90_000)


def test_flags_win_over_the_file(tmp_path: Path) -> None:
    f = tmp_path / "TASK.md"
    f.write_text(FULL)
    args = _args(f, allow_cmd=["make"], model="claude-opus-5-5", max_cost=0.0)
    _apply_task_file(Config(), args)
    assert args.allow_cmd == ["make"]
    assert args.model == "claude-opus-5-5"
    assert args.max_cost == 0.0
    f.write_text("---\ngauntlet: false\n---\nGo")
    args = _args(f, gauntlet=True)
    cfg = _apply_task_file(Config(max_steps=9), args)
    assert args.gauntlet is True  # the file can't switch off a flag
    assert cfg.max_steps == 9  # unset in the file: the environment's value stays


def test_cli_needs_exactly_one_task_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "TASK.md"
    f.write_text("Go")
    assert main(["run", "-w", str(tmp_path)]) == 2
    assert "run needs a task or --task-file (not both)" in capsys.readouterr().err
    assert main(["run", "Go", "--task-file", str(f), "-w", str(tmp_path)]) == 2
    f.write_text("---\nnope: 1\n---\nGo")
    assert main(["run", "--task-file", str(f), "-w", str(tmp_path)]) == 2
    assert "unknown key 'nope'" in capsys.readouterr().err


def test_cli_runs_the_file_task_with_its_budget(tmp_path: Path, home: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    f = tmp_path / "TASK.md"
    f.write_text("---\nmodel: m-file\nmax_steps: 1\n---\nList the files.")
    seen: list[str] = []

    def factory(name: str) -> LLM:
        seen.append(name)
        return ScriptedLLM(
            [
                LLMResponse("", (ToolCall("1", "list_dir", {"path": "."}),)),
                LLMResponse("done"),
            ]
        )

    assert main(["run", "--task-file", str(f), "-w", str(ws)], llm_factory=factory) == 1
    assert seen == ["m-file"]
    events = [json.loads(ln) for ln in (home / "traces.jsonl").read_text().splitlines()]
    assert events[0]["task"] == "List the files."
    assert events[-1]["stop"] == "budget_exhausted"
