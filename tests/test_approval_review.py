"""P1-06: approvals are shown in full, grants are audited, denials are normalised."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from sebastian import Budget, Sebastian, StopReason
from sebastian.approval import (
    MAX_REVIEW_CHARS,
    AllowApprover,
    ApprovalGate,
    DenyApprover,
    InteractiveApprover,
    describe_call,
)
from sebastian.clock import FakeClock, SeededIdSource
from sebastian.llm import ScriptedLLM
from sebastian.tools import (
    CommandPolicy,
    Tool,
    ToolError,
    ToolRegistry,
    Workspace,
    command_tool,
    file_tools,
)
from sebastian.tools.builtin import parse_command
from sebastian.trace import MemoryTracer
from sebastian.types import LLMResponse, ToolCall


def tools(tmp_path: Path) -> dict[str, Tool]:
    ts = [
        *file_tools(Workspace(tmp_path)),
        command_tool(Workspace(tmp_path), CommandPolicy.of(["echo"])),
    ]
    return {t.name: t for t in ts}


def review(tmp_path: Path, call: ToolCall, answer: str = "y") -> tuple[bool, str, list[str]]:
    out, asked = io.StringIO(), []

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return answer

    tool = tools(tmp_path)[call.name]
    ok = InteractiveApprover(ask=ask, out=out, root=tmp_path).approve(tool, call)
    return ok, out.getvalue(), asked


# --- parse_command ------------------------------------------------------------------------


def test_parse_command() -> None:
    assert parse_command('pytest -k "a b"  -q') == ["pytest", "-k", "a b", "-q"]
    for bad in ("", "   ", '"unclosed', None, 3):
        with pytest.raises(ToolError):
            parse_command(bad)


# --- shown in full ------------------------------------------------------------------------


def test_command_shown_one_argument_per_line(tmp_path: Path) -> None:
    ok, out, asked = review(tmp_path, ToolCall("1", "run_command", {"command": 'echo -n "a b" c'}))
    assert ok
    assert asked
    assert "argv[0] = 'echo'" in out
    assert "argv[2] = 'a b'" in out  # quoting made visible: one argument, not two
    assert "argv[3] = 'c'" in out


def test_long_command_is_not_truncated(tmp_path: Path) -> None:
    arg = "x" * 3000
    _, out, _ = review(tmp_path, ToolCall("1", "run_command", {"command": f"echo {arg} TAIL"}))
    assert arg in out
    assert "'TAIL'" in out


def test_write_new_file_shows_path_size_and_content(tmp_path: Path) -> None:
    content = "line1\n" * 200 + "END\n"
    _, out, _ = review(tmp_path, ToolCall("1", "write_file", {"path": "a.txt", "content": content}))
    assert "path: a.txt" in out
    assert f"size: {len(content.encode())} bytes" in out
    assert out.count("+line1") == 200
    assert "+END" in out


def test_write_existing_file_shows_a_diff(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("keep\nold\n")
    _, out, _ = review(
        tmp_path, ToolCall("1", "write_file", {"path": "a.txt", "content": "keep\nnew\n"})
    )
    assert "-old" in out
    assert "+new" in out
    assert " keep" in out


def test_edit_shows_old_and_new_in_full(tmp_path: Path) -> None:
    old, new = "a" * 900, "b" * 900
    call = ToolCall("1", "edit_file", {"path": "x.py", "old": old, "new": new})
    _, out, _ = review(tmp_path, call)
    assert f"-{old}" in out
    assert f"+{new}" in out


def test_too_large_to_review_is_refused_not_truncated(tmp_path: Path) -> None:
    big = "y" * (MAX_REVIEW_CHARS + 1)
    ok, out, asked = review(tmp_path, ToolCall("1", "write_file", {"path": "b", "content": big}))
    assert not ok
    assert not asked  # never asked about something the human couldn't see
    assert "too large to review" in out


def test_unparseable_command_is_refused(tmp_path: Path) -> None:
    ok, out, asked = review(tmp_path, ToolCall("1", "run_command", {"command": '"oops'}))
    assert not ok
    assert not asked
    assert "refused" in out


def test_control_characters_are_escaped(tmp_path: Path) -> None:
    esc, cr, rlo = chr(0x1B), chr(0x0D), chr(0x202E)  # ANSI escape, carriage return, bidi
    sneaky = f"safe{esc}[2K{cr}rm -rf ~{rlo}gnp.\n"
    _, out, _ = review(tmp_path, ToolCall("1", "write_file", {"path": "c", "content": sneaky}))
    for raw in (esc, cr, rlo):
        assert raw not in out
    for shown in ("\\x1b", "\\x0d", "\\u202e"):
        assert shown in out


def test_describe_falls_back_to_full_json(tmp_path: Path) -> None:
    text = describe_call(tools(tmp_path)["write_file"], ToolCall("1", "write_file", {"path": "a"}))
    assert '"path": "a"' in text


# --- audit and denials in the agent -------------------------------------------------------


def agent(
    tmp_path: Path, script: list[LLMResponse], approver: object, **kw: object
) -> tuple[Sebastian, MemoryTracer]:
    tracer = MemoryTracer()
    reg = ToolRegistry(
        [
            *file_tools(Workspace(tmp_path)),
            command_tool(Workspace(tmp_path), CommandPolicy.of(["echo", "rm"])),
        ]
    )
    a = Sebastian(
        llm=ScriptedLLM(script),
        tools=reg,
        gate=ApprovalGate(approver),  # type: ignore[arg-type]
        budget=Budget(max_steps=30),
        tracer=tracer,
        clock=FakeClock(),
        ids=SeededIdSource(1),
        **kw,  # type: ignore[arg-type]
    )
    return a, tracer


def go(*calls: ToolCall) -> list[LLMResponse]:
    return [LLMResponse("", (c,)) for c in calls] + [LLMResponse("done")]


def test_grants_are_audited(tmp_path: Path) -> None:
    w = ToolCall("1", "write_file", {"path": "a.txt", "content": "hi"})
    r = ToolCall("2", "read_file", {"path": "a.txt"})
    a, tracer = agent(tmp_path, go(w, r), AllowApprover())
    assert a.run("t").stop_reason is StopReason.COMPLETED
    grants = [e for e in tracer.events if e["event"] == "approval_granted"]
    assert len(grants) == 1  # the READ call is not gated, so not a grant
    canon = json.dumps(w.arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert grants[0]["tool"] == "write_file"
    assert grants[0]["approver"] == "AllowApprover"
    assert grants[0]["args_sha256"] == hashlib.sha256(canon.encode()).hexdigest()


class CountingDeny:
    def __init__(self) -> None:
        self.calls = 0

    def approve(self, tool: object, call: object) -> bool:
        self.calls += 1
        return False


def test_denial_key_ignores_whitespace_in_commands(tmp_path: Path) -> None:
    deny = CountingDeny()
    c1 = ToolCall("1", "run_command", {"command": "rm -rf  x"})
    c2 = ToolCall("2", "run_command", {"command": " rm  -rf x "})
    a, tracer = agent(tmp_path, go(c1, c2), deny)
    a.run("t")
    assert deny.calls == 1  # the reformatted retry never reached the approver again
    assert tracer.names().count("approval_denied") == 1


def test_denial_key_uses_the_normalised_path(tmp_path: Path) -> None:
    deny = CountingDeny()
    c1 = ToolCall("1", "write_file", {"path": "a/../b.txt", "content": "one"})
    c2 = ToolCall("2", "write_file", {"path": "./b.txt", "content": "two"})
    a, _ = agent(tmp_path, go(c1, c2), deny)
    a.run("t")
    assert deny.calls == 1


def test_failures_are_still_keyed_on_full_arguments(tmp_path: Path) -> None:
    """A tool *error* (not a denial) can be retried with different content."""
    c1 = ToolCall("1", "edit_file", {"path": "f.txt", "old": "nope", "new": "x"})
    c2 = ToolCall("2", "edit_file", {"path": "f.txt", "old": "here", "new": "x"})
    (tmp_path / "f.txt").write_text("here")
    a, _ = agent(tmp_path, go(c1, c2), AllowApprover())
    a.run("t")
    assert (tmp_path / "f.txt").read_text() == "x"


def test_run_stops_after_max_denials(tmp_path: Path) -> None:
    calls = [ToolCall(str(i), "write_file", {"path": f"f{i}", "content": ""}) for i in range(10)]
    a, tracer = agent(tmp_path, go(*calls), DenyApprover())
    result = a.run("t")
    assert result.stop_reason is StopReason.ERROR
    assert "5 approval denials" in result.detail
    assert tracer.names().count("approval_denied") == 5


def test_max_denials_configurable_and_zero_disables(tmp_path: Path) -> None:
    calls = [ToolCall(str(i), "write_file", {"path": f"f{i}", "content": ""}) for i in range(7)]
    a, _ = agent(tmp_path, go(*calls), DenyApprover(), max_denials=0)
    assert a.run("t").stop_reason is StopReason.COMPLETED
    a, _ = agent(tmp_path, go(*calls), DenyApprover(), max_denials=2)
    assert "2 approval denials" in a.run("t").detail


def test_repeat_of_a_denied_call_counts_toward_the_limit(tmp_path: Path) -> None:
    same = [ToolCall(str(i), "run_command", {"command": "rm x"}) for i in range(6)]
    a, _ = agent(tmp_path, go(*same), DenyApprover(), max_denials=3)
    assert a.run("t").stop_reason is StopReason.ERROR


# --- shared parser ------------------------------------------------------------------------


def test_scoped_approver_parses_like_run_command(tmp_path: Path) -> None:
    import re

    from sebastian.evolve import ScopedApprover

    ap = ScopedApprover(tmp_path, re.compile(r"^x$"), frozenset({"pytest"}))
    run = command_tool(Workspace(tmp_path), CommandPolicy.of(["pytest"]))
    assert ap.approve(run, ToolCall("1", "run_command", {"command": '"pytest" -q'}))
    assert not ap.approve(run, ToolCall("2", "run_command", {"command": '"pytest'}))


def test_no_diff_without_a_readable_in_jail_file(tmp_path: Path) -> None:
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe\x00")
    tool = tools(tmp_path)["write_file"]
    for path, root in (("bin.dat", tmp_path), ("../escape.txt", tmp_path), ("bin.dat", None)):
        text = describe_call(
            tool, ToolCall("1", "write_file", {"path": path, "content": "x"}), root
        )
        assert "(new or unreadable file; full content)" in text, path


def test_unparseable_denied_command_still_keys_its_denial(tmp_path: Path) -> None:
    deny = CountingDeny()
    bad = ToolCall("1", "run_command", {"command": '"unclosed'})
    a, _ = agent(tmp_path, go(bad, ToolCall("2", "run_command", {"command": '"unclosed'})), deny)
    a.run("t")
    assert deny.calls == 1


def test_existing_text_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sebastian.approval import _existing_text

    (tmp_path / "test.txt").write_text("hello")

    def mock_read_text(*args: object, **kwargs: object) -> str:
        raise PermissionError("no access")

    monkeypatch.setattr(Path, "read_text", mock_read_text)
    assert _existing_text("test.txt", tmp_path) is None


def test_interactive_approver_eoferror(tmp_path: Path) -> None:
    def ask(prompt: str) -> str:
        raise EOFError()

    tool = tools(tmp_path)["write_file"]
    call = ToolCall("1", "write_file", {"path": "a.txt", "content": "hello"})
    approver = InteractiveApprover(ask=ask, root=tmp_path)
    assert not approver.approve(tool, call)
