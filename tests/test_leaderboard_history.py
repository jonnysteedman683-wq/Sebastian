"""P2-14: leaderboard history kinds, dashboard separation and per-server labels."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from sebastian import history
from sebastian.cli import main
from sebastian.dashboard import Snapshot, latest_leaderboard, render
from sebastian.evals.cases import CAPABILITY
from sebastian.evals.leaderboard import LeaderboardError, ModelSpec, parse_spec, run_leaderboard
from sebastian.history import Record
from sebastian.llm.scripted import ScriptedLLM
from sebastian.types import LLMResponse


def lazy() -> ScriptedLLM:
    return ScriptedLLM([LLMResponse("Done.")] * 10)


def test_leaderboard_run_leaves_the_eval_tile_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(home))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    path = home / history.HISTORY_FILE
    history.append(path, Record(1.0, "eval", "claude-configured", 15, 16, 5, 5))  # evals --live
    args = ["models", "a", "b", "c", "--yes", "--scars", str(tmp_path / "none")]
    assert main(args, llm_factory=lambda _n: lazy()) == 0
    capsys.readouterr()
    records = history.load(path)
    board = [r for r in records if r.kind == "leaderboard"]
    assert sorted(r.model for r in board) == ["a", "b", "c"]
    assert len({r.run_id for r in board}) == 1
    assert board[0].run_id
    html = render(Snapshot(records, [], [], 10.0))
    tile = html[html.index("Eval score") : html.index("Injection resistance")]
    assert "94%" in tile  # 15/16 from the live eval
    assert "claude-configured" in tile
    assert "Latest model leaderboard" in html
    assert [p.model for p, _ in latest_leaderboard(records)] == ["a", "b", "c"]


def test_latest_leaderboard_groups_by_run_and_pairs_gauntlet() -> None:
    records = [
        Record(1.0, "leaderboard", "old", 1, 1, run_id="r1"),
        Record(2.0, "leaderboard", "x", 1, 2, run_id="r2"),
        Record(2.0, "leaderboard", "y", 2, 2, run_id="r2"),
        Record(2.0, "leaderboard-gauntlet", "y", 1, 2, run_id="r2"),
    ]
    board = latest_leaderboard(records)
    assert [(p.model, g.model if g else None) for p, g in board] == [("y", "y"), ("x", None)]
    assert latest_leaderboard([]) == []


def test_same_model_on_two_servers() -> None:
    a = parse_spec("openai:m@http://localhost:11434/v1")
    b = parse_spec("openai:m@http://localhost:8000/v1")
    assert a.label == "openai:m@localhost:11434"
    assert b.label == "openai:m@localhost:8000"
    rows = run_leaderboard([a, b], lambda _s: lazy(), CAPABILITY)
    assert {r.spec.label for r in rows} == {a.label, b.label}
    with pytest.raises(LeaderboardError, match="only once"):
        run_leaderboard(
            [a, parse_spec("openai:m@http://localhost:11434/v1/")], lambda _s: lazy(), CAPABILITY
        )


def test_labels_without_a_server_are_unchanged() -> None:
    assert ModelSpec("m").label == "m"
    assert ModelSpec("m", "anthropic").label == "anthropic:m"


def test_old_history_files_still_load(tmp_path: Path) -> None:
    path = tmp_path / history.HISTORY_FILE
    old = {
        "t": 1.0,
        "kind": "eval",
        "model": "m",
        "passed": 1,
        "total": 2,
        "resisted": 0,
        "hostile": 0,
    }
    new = {**old, "t": 2.0, "kind": "leaderboard", "run_id": "lb-1"}
    path.write_text("\n".join(json.dumps(x) for x in (old, new, {**old, "run_id": 7})) + "\n")
    records = history.load(path)
    assert [(r.kind, r.run_id) for r in records] == [
        ("eval", ""),
        ("eval", ""),
        ("leaderboard", "lb-1"),
    ]
