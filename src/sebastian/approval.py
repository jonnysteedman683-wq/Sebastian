"""Approval gating. Risky tool calls need an explicit yes; no approver means no.

There is deliberately no "unattended bypass": a scheduled or headless run gets the
``DenyApprover`` and therefore cannot perform gated actions.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, TextIO

from sebastian.tools.base import Permission, Tool, ToolError
from sebastian.tools.builtin import MAX_READ_BYTES, parse_command
from sebastian.tools.workspace import Workspace, WorkspaceError
from sebastian.types import ToolCall

# P1-06: the most a human is asked to review in one prompt. Bigger is refused, never cut.
MAX_REVIEW_CHARS = 32_000


class ReviewError(ValueError):
    """A call that can't be shown to a human in full, so it can't be approved."""


def args_digest(call: ToolCall) -> str:
    """SHA-256 of the call's canonical JSON arguments, for the audit trail."""
    canon = json.dumps(call.arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


def _visible(text: str) -> str:
    """Escape control and format characters (ANSI, CR, bidi overrides) so text can't hide."""
    return "".join(
        c
        if c == "\t" or c.isprintable()
        else (f"\\x{ord(c):02x}" if ord(c) < 0x100 else f"\\u{ord(c):04x}")
        for c in text
    )


def _lines(prefix: str, text: str) -> list[str]:
    return [prefix + _visible(line) for line in text.split("\n")]


def describe_call(tool: Tool, call: ToolCall, root: Path | None = None) -> str:
    """Everything a human needs to judge ``call``, in full. Raises ``ReviewError``."""
    args = call.arguments
    head = f"[approval] {tool.name} ({tool.permission.name})"
    if call.name == "run_command":
        try:
            argv = parse_command(args.get("command"))
        except ToolError as exc:
            raise ReviewError(str(exc)) from exc
        body = [f"  argv[{i}] = {_visible(repr(a))}" for i, a in enumerate(argv)]
    elif call.name == "write_file" and isinstance(args.get("content"), str):
        body = _describe_write(str(args.get("path")), args["content"], root)
    elif call.name == "edit_file" and isinstance(args.get("old"), str):
        body = [
            f"  path: {_visible(str(args.get('path')))}",
            f"  replace_all: {bool(args.get('replace_all', False))}",
            *_lines("-", args["old"]),
            *_lines("+", str(args.get("new", ""))),
        ]
    else:
        body = _lines("  ", json.dumps(args, ensure_ascii=False, indent=2, default=str))
    text = "\n".join([head, *body])
    if len(text) > MAX_REVIEW_CHARS:
        raise ReviewError(
            f"too large to review ({len(text)} chars > {MAX_REVIEW_CHARS}); ask for smaller changes"
        )
    return text


def _describe_write(path: str, content: str, root: Path | None) -> list[str]:
    lines = [
        f"  path: {_visible(path)}",
        f"  size: {len(content.encode())} bytes, {content.count(chr(10))} newlines",
    ]
    old = _existing_text(path, root)
    if old is None:
        return [*lines, "  (new or unreadable file; full content)", *_lines("+", content)]
    diff = difflib.unified_diff(
        old.splitlines(), content.splitlines(), "before", "after", lineterm=""
    )
    return [*lines, *(_visible(d) for d in diff)]


def _existing_text(path: str, root: Path | None) -> str | None:
    if root is None:
        return None
    try:
        target = Workspace(root).resolve(path)
        if not target.is_file() or target.stat().st_size > MAX_READ_BYTES:
            return None
        return target.read_text(encoding="utf-8")
    except (WorkspaceError, OSError, UnicodeDecodeError):
        return None


class Approver(Protocol):
    """Decides whether one gated tool call may run."""

    def approve(self, tool: Tool, call: ToolCall) -> bool:
        """Return True to allow ``call`` of ``tool``."""


class DenyApprover:
    """Denies every gated call. Used for unattended and non-TTY runs."""

    def approve(self, tool: Tool, call: ToolCall) -> bool:
        """Always deny."""
        return False


class AllowApprover:
    """Approves everything. For tests and fully trusted sandboxes only."""

    def approve(self, tool: Tool, call: ToolCall) -> bool:
        """Always allow."""
        return True


class InteractiveApprover:
    """Asks a human on the terminal; anything but y/yes (or EOF) is a no.

    P1-06: the call is shown in full (``describe_call``): commands one argument per line,
    writes as a diff or full content, control characters escaped. A call too large to show
    in full is refused without asking. ``root`` (the workspace) enables diffs of existing files.
    """

    def __init__(
        self,
        ask: Callable[[str], str] | None = None,
        out: TextIO | None = None,
        root: Path | None = None,
    ) -> None:
        self._ask = ask
        self._out = out
        self._root = root

    def approve(self, tool: Tool, call: ToolCall) -> bool:
        """Show the call in full and ask for a yes."""
        out = self._out or sys.stderr
        try:
            text = describe_call(tool, call, self._root)
        except ReviewError as exc:
            out.write(f"\n[approval] {tool.name} refused: {exc}\n")
            return False
        out.write(f"\n{text}\n")
        ask = self._ask or input
        try:
            answer = ask("Allow? [y/N] ").strip().lower()
        except EOFError:
            return False
        return answer in {"y", "yes"}


class ApprovalGate:
    """Lets calls below ``threshold`` through and sends the rest to the approver."""

    def __init__(self, approver: Approver, threshold: Permission = Permission.WRITE) -> None:
        self.approver = approver
        self.threshold = threshold

    def gated(self, tool: Tool) -> bool:
        """True if calls of ``tool`` need the approver."""
        return tool.permission >= self.threshold

    def check(self, tool: Tool, call: ToolCall) -> bool:
        """Return True if ``call`` may run."""
        if not self.gated(tool):
            return True
        return self.approver.approve(tool, call)

    def audit(self, call: ToolCall) -> dict[str, str]:
        """Fields for an ``approval_granted`` trace event (P1-06)."""
        return {
            "tool": call.name,
            "approver": type(self.approver).__name__,
            "args_sha256": args_digest(call),
        }
