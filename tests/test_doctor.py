"""B47: ``sebastian doctor`` checks the setup without network or side effects."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sebastian import doctor
from sebastian.cli import main
from sebastian.config import Config
from sebastian.doctor import FAIL, PASS, WARN, Check, run_checks

SECRET = "sk-ant-doctor-secret-value-123"  # noqa: S105 - fake, asserted never printed


def _ok_sandbox() -> dict[str, object]:
    return {"which": lambda _n: "/usr/bin/bwrap", "usable": lambda _p: True}


def _checks(env: dict[str, str], **kw: object) -> dict[str, Check]:
    opts = _ok_sandbox() | kw
    return {c.name: c for c in run_checks(env, **opts)}  # type: ignore[arg-type]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir(mode=0o700)
    return h


def _env(home: Path, **extra: str) -> dict[str, str]:
    return {"SEBASTIAN_HOME": str(home), "ANTHROPIC_API_KEY": SECRET, **extra}


def test_all_pass(home: Path) -> None:
    checks = _checks(_env(home))
    assert [c.name for c in checks.values()] == [
        "python", "config", "home", "api key", "prices", "sandbox", "memory", "history",
    ]  # fmt: skip
    assert {c.status for c in checks.values()} == {PASS}


def test_python_version() -> None:
    assert doctor.check_python((3, 11, 0)).status == PASS
    old = doctor.check_python((3, 10, 9))
    assert old.status == FAIL
    assert "3.11" in old.detail


def test_bad_config_fails_and_skips_dependent_checks(home: Path) -> None:
    checks = _checks(_env(home, SEBASTIAN_MAX_STEPS="lots"))
    assert checks["config"].status == FAIL
    assert "SEBASTIAN_MAX_STEPS" in checks["config"].detail
    assert "api key" not in checks
    assert "sandbox" in checks


def test_home_states(tmp_path: Path) -> None:
    missing = doctor.check_home(tmp_path / "a" / "b")
    assert missing.status == WARN
    assert not (tmp_path / "a").exists()  # read-only: nothing created
    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o755)
    assert doctor.check_home(loose).status == WARN
    afile = tmp_path / "file"
    afile.write_text("x")
    assert doctor.check_home(afile).status == FAIL


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_home_not_writable(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        assert doctor.check_home(locked).status == FAIL
    finally:
        locked.chmod(0o700)


def test_anthropic_key_missing_and_never_printed(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _checks({"SEBASTIAN_HOME": str(home)})["api key"].status == FAIL
    assert doctor.main(_env(home)) in (0, 1)
    assert SECRET not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("extra", "status", "needle"),
    [
        ({}, FAIL, "OPENAI_API_KEY is not set"),
        ({"OPENAI_API_KEY": "sk-x"}, PASS, "api.openai.com"),
        ({"SEBASTIAN_BASE_URL": "http://localhost:11434/v1"}, PASS, "local server"),
        ({"SEBASTIAN_BASE_URL": "https://api.groq.com/openai/v1"}, WARN, "api.groq.com"),
        (
            {"SEBASTIAN_BASE_URL": "https://api.groq.com/openai/v1",
             "SEBASTIAN_API_KEY_ENV": "GROQ_API_KEY"},
            FAIL,
            "GROQ_API_KEY is not set",
        ),
        (
            {"SEBASTIAN_BASE_URL": "https://api.groq.com/openai/v1",
             "SEBASTIAN_API_KEY_ENV": "GROQ_API_KEY", "GROQ_API_KEY": "gsk-x"},
            PASS,
            "GROQ_API_KEY is set",
        ),
        ({"SEBASTIAN_API_KEY_ENV": "ANTHROPIC_API_KEY"}, FAIL, "refusing"),
        ({"SEBASTIAN_BASE_URL": "http://example.com/v1"}, FAIL, "plain http"),
    ],
)  # fmt: skip
def test_openai_key_rules(home: Path, extra: dict[str, str], status: str, needle: str) -> None:
    env = {"SEBASTIAN_HOME": str(home), "SEBASTIAN_BACKEND": "openai", **extra}
    check = doctor.check_key(Config.from_env(env), env)
    assert check.status == status
    assert needle in check.detail
    assert "sk-x" not in check.detail
    assert "gsk-x" not in check.detail


def test_prices(home: Path) -> None:
    (home / "prices.json").write_text("{not json")
    assert _checks(_env(home))["prices"].status == FAIL
    (home / "prices.json").write_text(json.dumps({"claude-sonnet-5-5": {"input": 3, "output": 15}}))
    assert _checks(_env(home))["prices"].status == PASS
    unpriced = _checks(_env(home, SEBASTIAN_MAX_COST="1", SEBASTIAN_MODEL="other-model"))
    assert unpriced["prices"].status == FAIL


def test_sandbox_states() -> None:
    assert doctor.check_sandbox(lambda _n: None, lambda _p: True).status == WARN
    blocked = doctor.check_sandbox(lambda _n: "/usr/bin/bwrap", lambda _p: False)
    assert blocked.status == WARN
    assert "blocked" in blocked.detail


def test_memory_corrupt_fails(home: Path) -> None:
    (home / "memory.json").write_text("{not json")
    assert _checks(_env(home))["memory"].status == FAIL
    assert (home / "memory.json").read_text() == "{not json"


def test_history_malformed_lines_warn(home: Path) -> None:
    good = {"t": 1.0, "kind": "eval", "model": "m", "passed": 1, "total": 2}
    (home / "history.jsonl").write_text(json.dumps(good) + "\nnot json\n")
    h = _checks(_env(home))["history"]
    assert h.status == WARN
    assert "1 record(s), 1 malformed" in h.detail


def test_cli_exit_codes(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEBASTIAN_HOME", str(home))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SEBASTIAN_BACKEND", raising=False)
    assert main(["doctor"]) == 1
    assert "[fail] api key" in capsys.readouterr().out
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setattr(doctor, "check_sandbox", lambda *a: Check("sandbox", WARN, "x"))
    assert main(["doctor"]) == 0  # warnings alone don't fail
    out = capsys.readouterr().out
    assert "[warn] sandbox" in out
    assert SECRET not in out


def test_cli_doctor_reports_bad_config_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SEBASTIAN_BACKEND", "nope")
    assert main(["doctor"]) == 1
    assert "[fail] config" in capsys.readouterr().out
