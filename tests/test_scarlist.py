"""B60: ``sebastian scar list`` and ``sebastian scar prune``."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from sebastian.cli import main
from sebastian.evals.scar import SCAR_VERSION, write_scar
from sebastian.memory import _task_key
from sebastian.scarlist import duplicates, scars


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(h))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return h


def _scar(d: Path, name: str, task: str, mtime: float, origin: dict[str, Any] | None) -> Path:
    spec: dict[str, Any] = {
        "version": SCAR_VERSION,
        "name": name,
        "task": task,
        "files": {},
        "expect": {"stop": "completed"},
        "allow_gated": True,
    }
    if origin is not None:
        spec["origin"] = origin
    path = write_scar(spec, d)
    os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def scar_dir(tmp_path: Path) -> Path:
    d = tmp_path / "scars"
    _scar(d, "fix-ci-aaaa", "Fix  the CI", 1_000.0, {"run_id": "aaaa1111", "stop": "budget"})
    _scar(d, "fix-ci-bbbb", "fix the ci", 3_000.0, {"run_id": "bbbb2222"})
    _scar(d, "fix-ci-cccc", "FIX THE CI ", 2_000.0, {"run_id": "cccc3333"})
    _scar(d, "redteam-x", "Summarise notes", 500.0, {"source": "redteam"})
    _scar(d, "bare", "Do a thing", 0.0, None)
    (d / "README.md").write_text("not a scar\n")
    return d


def test_list(scar_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scar", "list", "--out", str(scar_dir)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "bare  ?  bare  1970-01-01T00:00:00+00:00",
        "fix-ci-aaaa  aaaa1111  fix-ci-aaaa  1970-01-01T00:16:40+00:00",
        "fix-ci-bbbb  bbbb2222  fix-ci-bbbb  1970-01-01T00:50:00+00:00",
        "fix-ci-cccc  cccc3333  fix-ci-cccc  1970-01-01T00:33:20+00:00",
        "redteam-x  redteam  redteam-x  1970-01-01T00:08:20+00:00",
    ]


def test_list_empty_and_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scar", "list", "--out", str(tmp_path / "none")]) == 0
    assert capsys.readouterr().out == "(no scars)\n"
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "x.json").write_text("{nope")
    assert main(["scar", "prune", "--out", str(bad), "--yes"]) == 2
    assert "scar: x.json: invalid JSON" in capsys.readouterr().err
    assert (bad / "x.json").exists()


def test_duplicates_keep_newest_and_use_memory_key(scar_dir: Path) -> None:
    found = scars(scar_dir)
    assert {_task_key(s.task) for s in found if s.name.startswith("fix-ci")} == {"fix the ci"}
    assert [s.name for s in duplicates(found)] == ["fix-ci-aaaa", "fix-ci-cccc"]


def test_prune_needs_confirmation(scar_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scar", "prune", "--out", str(scar_dir)]) == 1  # EOF on stdin counts as no
    captured = capsys.readouterr()
    assert "nothing removed" in captured.err
    assert len(list(scar_dir.glob("*.json"))) == 5


def test_prune_on_yes(
    scar_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("y\n"))
    assert main(["scar", "prune", "--out", str(scar_dir)]) == 0
    out = capsys.readouterr().out
    assert "removed 2 scar(s)" in out
    assert sorted(p.stem for p in scar_dir.glob("*.json")) == ["bare", "fix-ci-bbbb", "redteam-x"]
    assert (scar_dir / "README.md").exists()
    assert main(["scar", "prune", "--out", str(scar_dir), "--yes"]) == 0
    assert capsys.readouterr().out == "no duplicate scars\n"


def test_prune_yes_flag_skips_prompt(scar_dir: Path) -> None:
    assert main(["scar", "prune", "--out", str(scar_dir), "--yes"]) == 0
    assert len(list(scar_dir.glob("*.json"))) == 3


def test_origin_without_a_source(tmp_path: Path) -> None:
    _scar(tmp_path, "odd", "Odd one", 0.0, {"run_id": "", "stop": "error"})
    assert [s.source for s in scars(tmp_path)] == ["?"]
