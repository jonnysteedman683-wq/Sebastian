from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from sebastian import history
from sebastian.cli import main
from sebastian.clock import FakeClock
from sebastian.dashboard import Snapshot, gather, line_chart, render
from sebastian.history import Record
from sebastian.trace import JsonlTracer, RunSummary


def rec(t: float, kind: str = "eval", passed: int = 8, total: int = 10, res: int = 3,
        hostile: int = 5) -> Record:  # fmt: skip
    return Record(t, kind, "m", passed, total, res, hostile)


def test_history_round_trip_and_bad_lines(tmp_path: Path) -> None:
    path = tmp_path / "h" / history.HISTORY_FILE
    assert history.load(path) == []
    history.append(path, rec(2.0))
    history.append(path, rec(1.0, "redteam", 4, 5, 4, 5))
    with path.open("a") as fh:
        bad_kind = {"t": 3, "kind": "x", "model": "m"}
        negative = {"t": 3, "kind": "eval", "model": "m", "passed": -1, "total": 1}
        for bad in ["nope", "[]", json.dumps(bad_kind), json.dumps(negative)]:
            fh.write(bad + "\n")
    loaded = history.load(path)
    assert [r.t for r in loaded] == [1.0, 2.0]  # sorted by time, bad lines skipped
    assert loaded[1].score == 0.8
    assert loaded[0].resistance == 0.8
    assert Record(0, "eval", "m", 0, 0).score == 0.0
    assert Record(0, "eval", "m", 1, 1).resistance is None


def test_line_chart() -> None:
    assert "No data yet." in line_chart("T", [], "l")
    one = line_chart("T", [(5.0, 0.5)], "l")
    assert one.count("<circle") == 1
    many = line_chart("T", [(1.0, 0.0), (2.0, 1.5)], "label <x>")
    assert "label &lt;x&gt;" in many  # escaped
    assert many.count("<circle") == 2


def test_render_escapes_everything_untrusted() -> None:
    snap = Snapshot(
        records=[rec(1.0), rec(2.0, "redteam", 1, 2, 1, 2)],
        runs=[
            RunSummary("<script>alert(1)</script>", 1.0, "completed", 3, 1234),
            RunSummary("r2", None, "running", 0, 0),
        ],
        scars=["redteam-<img onerror=x>", "plain"],
        generated=0.0,
    )
    page = render(snap)
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
    assert "<img onerror" not in page
    assert "80%" in page  # eval score tile
    assert "50%" in page  # resistance tile
    assert "1 from the red team" in page
    assert "1,234" in page
    assert "100% completed" in page


def test_render_empty_state() -> None:
    page = render(Snapshot([], [], [], 0.0))
    assert page.count("No data yet.") == 2
    assert "No runs traced yet." in page
    assert "No scars yet." in page
    assert "run evals --live" in page
    assert "<script" not in page  # no JavaScript at all


def test_gather_reads_local_files(tmp_path: Path) -> None:
    home, scars = tmp_path / "home", tmp_path / "scars"
    scars.mkdir()
    (scars / "redteam-a.json").write_text("{}")
    history.append(home / history.HISTORY_FILE, rec(1.0))
    tracer = JsonlTracer(home / "traces.jsonl", FakeClock(10.0))
    tracer.emit("r1", "run_start", task="t")
    tracer.emit("r1", "run_end", stop="completed", steps=2, usage={"input": 5, "output": 5})
    snap = gather(home, scars, 99.0)
    assert (len(snap.records), len(snap.runs), snap.scars) == (1, 1, ["redteam-a"])
    assert gather(tmp_path / "none", tmp_path / "none", 0.0).scars == []


def test_cli_dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                       capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    history.append(tmp_path / "home" / history.HISTORY_FILE, rec(1.0))
    out = tmp_path / "dash.html"
    assert main(["dashboard", "-o", str(out), "--scars", str(tmp_path / "s")]) == 0
    assert out.read_text().startswith("<!doctype html>")
    assert "1 scored round(s), 0 run(s), 0 scar(s)" in capsys.readouterr().err
