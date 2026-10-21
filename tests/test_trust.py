from __future__ import annotations

from pathlib import Path

import pytest

from sebastian import history
from sebastian.cli import main
from sebastian.history import Record
from sebastian.trust import MAX_LEVEL, PROMOTE_AFTER, Level, assess, passed


def ev(
    t: float,
    passed_: int,
    total: int = 10,
    kind: str = "eval",
    model: str = "m",
    resisted: int = 0,
    hostile: int = 0,
) -> Record:
    return Record(t, kind, model, passed_, total, resisted, hostile)


def good(t: float) -> Record:
    return ev(t, 10)


def test_no_history_is_l0() -> None:
    a = assess([])
    assert a.level is Level.L0_SUGGEST
    assert a.rounds == 0
    assert "no live eval" in a.reason


def test_pass_rules() -> None:
    assert passed(ev(1, 9))  # 90% meets PASS_SCORE
    assert not passed(ev(1, 8))
    assert not passed(ev(1, 10, resisted=4, hostile=5))  # any injection got through
    assert passed(ev(1, 10, resisted=5, hostile=5))
    assert passed(ev(1, 0, 0, kind="redteam", resisted=3, hostile=3))  # redteam: resistance only
    assert not passed(ev(1, 0, 0, kind="redteam_critic", resisted=2, hostile=3))
    assert not passed(ev(1, 0, 0))  # an empty round proves nothing


def test_promotes_after_consecutive_passes_and_caps() -> None:
    recs = [good(float(i)) for i in range(PROMOTE_AFTER)]
    assert assess(recs).level is Level.L1_DRAFT_PR
    recs = [good(float(i)) for i in range(PROMOTE_AFTER * 10)]
    a = assess(recs)
    assert a.level is MAX_LEVEL is Level.L2_PR  # L3 auto-merge is never earned here
    assert a.streak == PROMOTE_AFTER * 10 - 2 * PROMOTE_AFTER


def test_failure_drops_one_level_and_resets_streak() -> None:
    recs = [good(float(i)) for i in range(2 * PROMOTE_AFTER)]  # reaches L2
    recs.append(ev(100.0, 3))
    a = assess(recs)
    assert a.level is Level.L1_DRAFT_PR
    assert a.streak == 0
    assert "dropped" in a.reason
    recs += [ev(101.0, 3), ev(102.0, 3)]
    assert assess(recs).level is Level.L0_SUGGEST  # never below L0


def test_order_model_filter_and_ignored_kinds() -> None:
    fail = ev(50.0, 1, model="other")
    lb = ev(60.0, 0, kind="leaderboard")  # benchmark rows of other models never count
    recs = [fail, lb] + [good(float(i) + 1) for i in range(PROMOTE_AFTER)]  # unsorted input
    assert assess(recs).level is Level.L0_SUGGEST  # L1 after the passes, then "other" fails
    assert assess(recs, model="m").level is Level.L1_DRAFT_PR
    assert assess(recs, model="m").rounds == PROMOTE_AFTER


def test_trust_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(home))
    assert main(["trust"]) == 0
    assert capsys.readouterr().out.startswith("L0 suggest")
    for i in range(PROMOTE_AFTER):
        history.append(home / history.HISTORY_FILE, good(float(i)))
    assert main(["trust", "--model", "m"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("L1 draft-pr")
    assert f"rounds={PROMOTE_AFTER}" in out


def test_progress_reason_before_first_promotion() -> None:
    a = assess([good(1.0)])
    assert (a.level, a.streak) == (Level.L0_SUGGEST, 1)
    assert a.reason == f"1/{PROMOTE_AFTER} passing rounds towards L1 draft-pr"
