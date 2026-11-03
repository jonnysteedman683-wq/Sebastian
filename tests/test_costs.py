"""B58: ``sebastian cost``."""

from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sebastian.cli import main
from sebastian.costs import report, run_models
from sebastian.llm.scripted import ScriptedLLM
from sebastian.types import LLMResponse

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC).timestamp()
DAY = 86_400.0


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(h))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return h


def _run(
    rid: str, t: float, model: str | None, inp: int, out: int, cost: float | None
) -> list[dict[str, Any]]:
    start: dict[str, Any] = {"t": t, "run_id": rid, "event": "run_start", "task": "x"}
    if model is not None:
        start["model"] = model
    usage: dict[str, Any] = {"input": inp, "output": out}
    if cost is not None:
        usage["cost"] = cost
    end = {"t": t + 5, "run_id": rid, "event": "run_end", "stop": "completed", "usage": usage}
    return [start, end]


def _write(path: Path, events: list[dict[str, Any]], junk: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) for e in events]
    if junk:
        lines += ["not json", json.dumps(["a list"])]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _traces(home: Path) -> Path:
    path = home / "traces.jsonl"
    _write(
        path,
        [
            *_run("a1", NOW, "sonnet", 100, 50, 0.01),
            *_run("a2", NOW - 60, "sonnet", 10, 5, 0.002),
            *_run("b1", NOW, "qwen3", 1_000, 200, None),  # unpriced local model
            *_run("c1", NOW - DAY, "sonnet", 7, 3, None),  # partly priced day
            *_run("c2", NOW - DAY, "sonnet", 20, 10, 0.5),
            *_run("old", NOW - 30 * DAY, "sonnet", 999, 1, 9.0),  # outside 7 days
            *_run("legacy", NOW - DAY, None, 40, 2, None),  # traced before B58
            {"t": NOW, "run_id": "live", "event": "run_start", "model": "sonnet"},  # unfinished
        ],
        junk=True,
    )
    return path


def test_report_groups_by_day_and_model(home: Path) -> None:
    lines = report(_traces(home), NOW, 7).splitlines()
    assert lines[0].split() == ["day", "model", "runs", "tokens", "cost"]
    rows = [ln.split() for ln in lines[1:]]
    assert rows == [
        ["2026-10-31", "sonnet", "2", "165", "$0.0120"],
        ["2026-10-31", "qwen3", "1", "1,200", "?"],
        ["2026-10-30", "sonnet", "2", "40", "$0.5000+?"],
        ["2026-10-30", "?", "1", "42", "?"],
        ["total", "6", "1,447", "$0.5120+?"],
    ]


def test_days_and_model_filters(home: Path) -> None:
    path = _traces(home)
    today = report(path, NOW, 1).splitlines()
    assert [ln.split()[0] for ln in today[1:]] == ["2026-10-31", "2026-10-31", "total"]
    month = report(path, NOW, 31, "sonnet").splitlines()
    assert [ln.split()[1] for ln in month[1:-1]] == ["sonnet"] * 3
    assert month[-1].split()[-1] == "$9.5120+?"
    assert report(path, NOW, 7, "nope") == "(no finished runs in that window)"


def test_missing_file(home: Path) -> None:
    assert run_models(home / "none.jsonl") == {}
    assert report(home / "none.jsonl", NOW, 7) == "(no finished runs in that window)"


def test_cli_cost(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _traces(home)
    assert main(["cost", "--days", "3650", "--model", "qwen3"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1].split()[1:] == ["qwen3", "1", "1,200", "?"]


def test_run_records_its_model(tmp_path: Path, home: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()

    def factory(name: str) -> ScriptedLLM:
        return ScriptedLLM([LLMResponse("done")])

    assert main(["run", "say done", "-w", str(ws), "--model", "m-test"], llm_factory=factory) == 0
    assert set(run_models(home / "traces.jsonl").values()) == {"m-test"}
