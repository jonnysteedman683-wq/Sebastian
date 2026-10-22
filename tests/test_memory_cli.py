"""B46: ``sebastian memory list`` and ``sebastian memory forget``."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

from sebastian.cli import main
from sebastian.memory import Episode, MemoryCorruptError, MemoryStore


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(h))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return h


def _store(home: Path) -> MemoryStore:
    s = MemoryStore(home / "memory.json")
    s.record(Episode("Fix the parser bug", "completed", "patched tokenizer", 1_000.0))
    s.record(Episode("Write release notes", "budget", "drafted changelog", 2_000.0))
    s.record(Episode("Rename config loader", "completed", "renamed across files", 3_000.0))
    return s


def _lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return capsys.readouterr().out.strip().splitlines()


def test_list_empty_store(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["memory", "list"]) == 0
    assert _lines(capsys) == ["(no episodes)"]


def test_list_newest_first_with_limit(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _store(home)
    assert main(["memory", "list"]) == 0
    lines = _lines(capsys)
    assert [ln.split("  ", 2)[2] for ln in lines] == [
        "Rename config loader",
        "Write release notes",
        "Fix the parser bug",
    ]
    assert lines[0].startswith("1970-01-01T00:50:00+00:00  completed  ")
    assert main(["memory", "list", "-n", "1"]) == 0
    assert len(_lines(capsys)) == 1


def test_rerecorded_task_becomes_newest(home: Path) -> None:
    s = _store(home)
    s.record(Episode("fix  the PARSER bug", "completed", "again", 4_000.0))
    assert s.episodes()[0].task == "fix  the PARSER bug"
    assert len(s) == 3


def test_list_query_ranks_by_recall(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _store(home)
    assert main(["memory", "list", "--query", "changelog release"]) == 0
    assert _lines(capsys)[0].endswith("Write release notes")
    assert main(["memory", "list", "-q", "nothing matches zzzz"]) == 0
    assert _lines(capsys) == ["(no episodes)"]


def test_forget_with_yes_hit(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _store(home)
    assert main(["memory", "forget", "  write RELEASE notes ", "--yes"]) == 0
    assert "forgot" in capsys.readouterr().out
    assert [e.task for e in MemoryStore(home / "memory.json").episodes()] == [
        "Rename config loader",
        "Fix the parser bug",
    ]


def test_forget_miss(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _store(home)
    before = (home / "memory.json").read_bytes()
    assert main(["memory", "forget", "no such task", "--yes"]) == 1
    assert "no episode" in capsys.readouterr().err
    assert (home / "memory.json").read_bytes() == before


def test_forget_store_api(home: Path) -> None:
    s = _store(home)
    assert s.forget("FIX THE PARSER BUG") is True
    assert s.forget("Fix the parser bug") is False
    assert len(MemoryStore(home / "memory.json")) == 2


@pytest.mark.parametrize("answer", ["y\n", "yes\n"])
def test_forget_confirmed(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], answer: str
) -> None:
    _store(home)
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    assert main(["memory", "forget", "Fix the parser bug"]) == 0
    assert "forget 'Fix the parser bug'? [y/N]" in capsys.readouterr().out
    assert len(MemoryStore(home / "memory.json")) == 2


@pytest.mark.parametrize("answer", ["n\n", "\n", ""])
def test_forget_declined_or_eof_keeps_episode(
    home: Path, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    _store(home)
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    assert main(["memory", "forget", "Fix the parser bug"]) == 1
    assert len(MemoryStore(home / "memory.json")) == 3


def test_forget_needs_task(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["memory", "forget", "--yes"]) == 2
    assert "needs a task" in capsys.readouterr().err


def test_corrupt_memory_still_raises(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home.mkdir(parents=True)
    (home / "memory.json").write_text("{not json")
    with pytest.raises(MemoryCorruptError):
        MemoryStore(home / "memory.json")
    assert main(["memory", "list"]) == 2
    assert main(["memory", "forget", "x", "--yes"]) == 2
    assert "invalid JSON" in capsys.readouterr().err
    assert (home / "memory.json").read_text() == "{not json"
