"""P1-08: one shared redactor; sessions redacted on save; a private SEBASTIAN_HOME."""

from __future__ import annotations

import io
import json
import stat
import sys
from pathlib import Path

import pytest

from sebastian import Budget, Sebastian, StopReason
from sebastian.approval import AllowApprover, ApprovalGate
from sebastian.clock import FakeClock, SeededIdSource
from sebastian.config import ensure_home
from sebastian.llm import ScriptedLLM
from sebastian.session import SessionStore
from sebastian.tools import ToolRegistry, Workspace, file_tools
from sebastian.trace import REDACTED, MemoryTracer, redact, redact_text
from sebastian.types import LLMResponse, ToolCall

# Obvious fakes, assembled at runtime so no scanner mistakes this file for a leak.
B62 = "aZ3kQ9xW2mN7pL5vR8tY1uB4cD6eF0gH"  # 32 chars, mixed case + digits
POSITIVE = {
    "openai": "sk-test-FAKE" + "x" * 20,
    "anthropic": "sk-ant-api03-FAKE" + "y" * 20,
    "ghp": "ghp_" + "F" * 36,
    "gho": "gho_" + "a1" * 18,
    "ghs": "ghs_" + "b2" * 18,
    "ghu": "ghu_" + "c3" * 18,
    "github_pat": "github_pat_" + "11ABCDEFG0" + "_" + "z" * 40,
    "aws": "AKIA" + "ABCDEFGHIJKLMNOP",
    "slack": "xoxb-" + "1234567890-abcdefFAKE",
    "jwt": "eyJhbGciOiJIUzI1NiJ9" + "." + "eyJzdWIiOiJ0ZXN0In0" + "." + "c2lnbmF0dXJlRkFLRQ",
    "bearer": "Bearer " + "abc.def-ghi",
    "entropy": B62 + "Qq7Ww",
}
PEM = "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIFAKE\nLINE2\n-----END " + "RSA PRIVATE KEY-----"
NEGATIVE = {
    "uuid": "123e4567-e89b-12d3-a456-426614174000",
    "git_sha": "59c3206cf9bb75c65784bdd4bef2b0e19120c6aa",
    "sha256": "a" * 10 + "0123456789abcdef" * 3 + "bcdef0",
    "snake": "test_metrics_use_baseline_config_with_candidate_tests",
    "path": "/tmp/pytest-of-root/pytest-43/test_tampered_git_pointer_abor0/attacker",
    "sentence": "The quick brown fox jumps over the lazy dog again and again.",
    "number": "12345678901234567890123456789012345",
    "camel": "VeryLongCamelCaseIdentifierNameForTesting",
    "url": "https://github.com/jonnysteedman683-wq/Sebastian/pull/62",
    "code": "max_tokens: int = 200_000\napi_key_env = env.get('SEBASTIAN_API_KEY_ENV', '')",
    "constant": 'CASE_TOKENS = 80_000\nAPI_KEY_ENV = "OPENAI_API_KEY"',
}


@pytest.mark.parametrize("name", sorted(POSITIVE))
def test_secrets_are_redacted(name: str) -> None:
    secret = POSITIVE[name]
    out = redact_text(f"before {secret} after")
    assert secret not in out, out
    assert REDACTED in out
    assert out.startswith("before ")


def test_env_file_assignments_are_redacted() -> None:
    env = "DEBUG=1\nMY_SERVICE_API_KEY=plainvalue99\nexport GH_TOKEN='quoted-value'\nUSER=jonny\n"
    out = redact_text(env)
    assert "plainvalue99" not in out
    assert "quoted-value" not in out
    assert "MY_SERVICE_API_KEY=" + REDACTED in out
    assert "DEBUG=1" in out
    assert "USER=jonny" in out


def test_random_segment_inside_a_path_is_still_caught() -> None:
    out = redact_text(f"https://example.invalid/hook/{B62}Zz9")
    assert B62 not in out


def test_private_key_block_is_redacted() -> None:
    out = redact_text(f"key:\n{PEM}\ndone")
    assert "MIIFAKE" not in out
    assert out.endswith("done")
    unterminated = redact_text("-----BEGIN " + "OPENSSH PRIVATE KEY-----\nAAAAFAKE")
    assert "AAAAFAKE" not in unterminated


@pytest.mark.parametrize("name", sorted(NEGATIVE))
def test_ordinary_text_is_kept(name: str) -> None:
    text = NEGATIVE[name]
    assert redact_text(text) == text


def test_redact_walks_structures() -> None:
    data = {"api_key": "x", "args": {"content": f"k={POSITIVE['aws']}"}, "n": 3}
    out = redact(data)
    assert out["api_key"] == REDACTED
    assert POSITIVE["aws"] not in json.dumps(out)
    assert out["n"] == 3


# --- sessions --------------------------------------------------------------------------


def make(tmp_path: Path, script: list[LLMResponse], **store_kw: object) -> Sebastian:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return Sebastian(
        llm=ScriptedLLM(script),
        tools=ToolRegistry(file_tools(Workspace(ws))),
        gate=ApprovalGate(AllowApprover()),
        budget=Budget(max_steps=2),
        tracer=MemoryTracer(),
        clock=FakeClock(),
        ids=SeededIdSource(7),
        sessions=SessionStore(tmp_path / "sessions", **store_kw),  # type: ignore[arg-type]
        workspace=str(ws.resolve()),
    )


def leaky_script() -> list[LLMResponse]:
    read = ToolCall("1", "read_file", {"path": ".envfile"})
    return [LLMResponse(f"task uses {POSITIVE['anthropic']}", (read,)), LLMResponse("x", (read,))]


def test_sessions_are_redacted_on_save_and_resume_works(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws/.envfile").write_text(f"OPENAI={POSITIVE['openai']}\n")
    agent = make(tmp_path, leaky_script())
    first = agent.run(f"deploy with {POSITIVE['ghp']}")
    assert first.stop_reason is StopReason.BUDGET_EXHAUSTED
    raw = (tmp_path / "sessions" / f"{first.run_id}.json").read_text()
    for secret in (POSITIVE["anthropic"], POSITIVE["openai"], POSITIVE["ghp"]):
        assert secret not in raw
    assert REDACTED in raw
    # Resume continues from the redacted history.
    again = make(tmp_path, [LLMResponse("all done")])
    again.ids = SeededIdSource(8)
    second = again.resume(first.run_id)
    assert second.stop_reason is StopReason.COMPLETED


def test_keep_raw_sessions_opt_in(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws/.envfile").write_text(f"OPENAI={POSITIVE['openai']}\n")
    first = make(tmp_path, leaky_script(), raw=True).run("t")
    raw = (tmp_path / "sessions" / f"{first.run_id}.json").read_text()
    assert POSITIVE["openai"] in raw


def test_cli_keep_raw_sessions_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sebastian import cli

    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    seen: list[SessionStore] = []
    real = SessionStore

    def spy(root: Path, **kw: object) -> SessionStore:
        store = real(root, **kw)  # type: ignore[arg-type]
        seen.append(store)
        return store

    monkeypatch.setattr("sebastian.cli.SessionStore", spy)
    ws = tmp_path / "ws"
    ws.mkdir()
    base = ["run", "t", "-w", str(ws), "--no-memory"]
    for extra, raw in (([], False), (["--keep-raw-sessions"], True)):
        cli.main([*base, *extra], llm_factory=lambda m: ScriptedLLM([LLMResponse("ok")]))
        assert seen[-1].raw is raw


# --- SEBASTIAN_HOME ------------------------------------------------------------------


def test_home_is_created_private(tmp_path: Path) -> None:
    home = tmp_path / "a" / "home"
    ensure_home(home)
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_existing_home_is_tightened(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    home.chmod(0o755)
    ensure_home(home)
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_cli_creates_private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sebastian import cli

    home = tmp_path / "fresh"
    monkeypatch.setenv("SEBASTIAN_HOME", str(home))
    cli.main(["traces"])
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
