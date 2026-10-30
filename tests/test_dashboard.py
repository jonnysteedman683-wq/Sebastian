from __future__ import annotations

import io
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
    assert page.count("No data yet.") == 3  # eval, red team, needs help
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


# --- B41: Gauntlet, critic and needs-help views ---------------------------------------------


def test_multi_line_chart_draws_each_series_with_a_legend() -> None:
    from sebastian.dashboard import multi_line_chart

    page = multi_line_chart(
        "T", [("plain", [(1.0, 0.5), (2.0, 0.6)]), ("Gauntlet", [(2.0, 0.9)]), ("x", [])], "l"
    )
    assert page.count("<polyline") == 2  # the empty series is skipped
    assert 'class="key s1">plain' in page
    assert 'class="key s2">Gauntlet' in page
    assert "Gauntlet 1970-01-01" in page  # series name in each point's tooltip
    assert "No data yet." in multi_line_chart("T", [("a", [])], "l")
    same_time = multi_line_chart("T", [("a", [(5.0, 0.1)]), ("b", [(5.0, 0.2)])], "l")
    assert same_time.count('cx="320.0"') == 2  # a single instant is centred


def test_trust_level_on_dashboard() -> None:
    from sebastian.history import Record

    # L0 (no rounds)
    page_l0 = render(Snapshot([], [], [], 9.0))
    assert "Trust level" in page_l0
    assert "L0 suggest" in page_l0

    # L1 (3 passing rounds)
    records = [
        Record(1.0, "eval", "<script>hostile</script>", 9, 10, 1, 1),
        Record(2.0, "eval", "<script>hostile</script>", 9, 10, 1, 1),
        Record(3.0, "eval", "<script>hostile</script>", 9, 10, 1, 1),
    ]
    page_l1 = render(Snapshot(records, [], [], 9.0))

    # Assert the tile is present and hostile model is escaped
    assert "Trust level" in page_l1
    assert "L1 draft-pr" in page_l1
    assert "<script>" not in page_l1
    assert "&lt;script&gt;" in page_l1

    # Assert trust level series is on the chart
    title = "Live eval score (&lt;script&gt;hostile&lt;/script&gt;)"
    eval_fig = page_l1.split(title)[1].split("</figure>")[0]
    assert 'class="key s2">trust level' in eval_fig

    # L2 (6 passing rounds)
    records.extend(
        [
            Record(4.0, "eval", "<script>hostile</script>", 9, 10, 1, 1),
            Record(5.0, "eval", "<script>hostile</script>", 9, 10, 1, 1),
            Record(6.0, "eval", "<script>hostile</script>", 9, 10, 1, 1),
        ]
    )
    page_l2 = render(Snapshot(records, [], [], 9.0))
    assert "Trust level" in page_l2
    assert "L2 pr" in page_l2


def test_gauntlet_and_critic_tiles_and_charts() -> None:
    records = [
        rec(1.0, "eval", 6, 10),
        rec(2.0, "gauntlet", 9, 10),
        rec(3.0, "redteam", 4, 5, 4, 5),
        rec(4.0, "redteam_critic", 3, 4, 3, 4),
    ]
    page = render(Snapshot(records, [], [], 9.0))
    assert "Gauntlet score" in page
    assert ">90%<" in page
    assert "Critic resistance" in page
    assert ">75%<" in page
    assert 'class="key s2">Gauntlet' in page
    assert 'class="key s2">critic' in page
    assert "Live eval score (m)" in page


def test_trend_lines_follow_the_latest_model_only() -> None:
    from sebastian.history import Record

    records = [Record(1.0, "eval", "a", 1, 2), Record(2.0, "eval", "b", 2, 2),
               Record(3.0, "eval", "a", 2, 2)]  # fmt: skip
    page = render(Snapshot(records, [], [], 9.0))
    assert "Live eval score (a)" in page
    eval_fig = page.split("Live eval score (a)")[1].split("</figure>")[0]
    assert eval_fig.count("<circle") == 4  # 2 for eval, 2 for trust level


def test_needs_help_by_day() -> None:
    from sebastian.dashboard import needs_help_by_day

    day = 86_400.0
    runs = [
        RunSummary("a", 10.0, "needs_help"),
        RunSummary("b", 20.0, "completed"),
        RunSummary("c", day + 5, "completed"),
        RunSummary("d", day + 6, "running"),  # unfinished: ignored
        RunSummary("e", None, "needs_help"),  # no start time: ignored
    ]
    assert needs_help_by_day(runs) == [(0.0, 0.5), (day, 0.0)]
    page = render(Snapshot([], runs, [], 9.0))
    assert "Runs needing help (daily share)" in page
