"""Approval gating. Risky tool calls need an explicit yes; no approver means no.

There is deliberately no "unattended bypass": a scheduled or headless run gets the
``DenyApprover`` and therefore cannot perform gated actions.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from typing import Protocol, TextIO

from sebastian.tools.base import Permission, Tool
from sebastian.types import ToolCall


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
    """Asks a human on the terminal; anything but y/yes (or EOF) is a no."""

    def __init__(
        self,
        ask: Callable[[str], str] | None = None,
        out: TextIO | None = None,
    ) -> None:
        self._ask = ask
        self._out = out

    def approve(self, tool: Tool, call: ToolCall) -> bool:
        """Show the call and ask for a yes."""
        args = json.dumps(call.arguments, ensure_ascii=False)[:500]
        (self._out or sys.stderr).write(
            f"\n[approval] {tool.name} ({tool.permission.name}) {args}\n"
        )
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

    def check(self, tool: Tool, call: ToolCall) -> bool:
        """Return True if ``call`` may run."""
        if tool.permission < self.threshold:
            return True
        return self.approver.approve(tool, call)
