import json
from pathlib import Path

from sebastian import history
from sebastian.history import Record


def rec(
    t: float, kind: str = "eval", passed: int = 8, total: int = 10, res: int = 3, hostile: int = 5
) -> Record:
    return Record(t, kind, "m", passed, total, res, hostile)


def test_record_properties() -> None:
    r_normal = rec(1.0, passed=8, total=10, res=3, hostile=5)
    assert r_normal.score == 0.8
    assert r_normal.resistance == 0.6

    r_empty = Record(0, "eval", "m", 0, 0)
    assert r_empty.score == 0.0
    assert r_empty.resistance is None

    r_hostile_only = Record(0, "eval", "m", 1, 1, 0, 0)
    assert r_hostile_only.resistance is None


def test_append(tmp_path: Path) -> None:
    path = tmp_path / "subdir" / history.HISTORY_FILE

    # Directory should not exist yet
    assert not path.parent.exists()

    r = rec(1.0)
    history.append(path, r)

    # Check directory was created
    assert path.parent.exists()
    assert path.exists()

    lines = path.read_text().splitlines()
    assert len(lines) == 1

    data = json.loads(lines[0])
    assert data["t"] == 1.0
    assert data["kind"] == "eval"
    assert data["model"] == "m"
    assert data["passed"] == 8
    assert data["total"] == 10
    assert data["resisted"] == 3
    assert data["hostile"] == 5
    assert data["run_id"] == ""


def test_load_and_parse(tmp_path: Path) -> None:
    path = tmp_path / "h" / history.HISTORY_FILE

    # Missing file returns []
    assert history.load(path) == []

    # Valid records appended
    history.append(path, rec(2.0))
    history.append(path, rec(1.0, "redteam", 4, 5, 4, 5))

    # Bad lines ignored
    with path.open("a") as fh:
        bad_kind = {"t": 3, "kind": "x", "model": "m"}
        negative = {"t": 3, "kind": "eval", "model": "m", "passed": -1, "total": 1}
        bad_types = {"t": "3", "kind": "eval", "model": "m", "passed": 1, "total": 1}
        bad_ints = {"t": 3, "kind": "eval", "model": "m", "passed": "1", "total": 1}
        no_model = {"t": 3, "kind": "eval"}

        for bad in [
            "nope",
            "[]",
            json.dumps(bad_kind),
            json.dumps(negative),
            json.dumps(bad_types),
            json.dumps(bad_ints),
            json.dumps(no_model),
        ]:
            fh.write(bad + "\n")

    loaded = history.load(path)

    # Sorting by .t and ignoring invalid entries
    assert [r.t for r in loaded] == [1.0, 2.0]

    assert loaded[0].kind == "redteam"
    assert loaded[1].kind == "eval"

    # Check score and resistance matching parsed logic
    assert loaded[1].score == 0.8
    assert loaded[0].resistance == 0.8
