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
    def approve(self, tool: Tool, call: ToolCall) -> bool: ...


class DenyApprover:
    def approve(self, tool: Tool, call: ToolCall) -> bool:
        return False


class AllowApprover:
    """Approves everything. For tests and fully trusted sandboxes only."""

    def approve(self, tool: Tool, call: ToolCall) -> bool:
        return True


class InteractiveApprover:
    def __init__(
        self,
        ask: Callable[[str], str] | None = None,
        out: TextIO | None = None,
    ) -> None:
        self._ask = ask
        self._out = out

    def approve(self, tool: Tool, call: ToolCall) -> bool:
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
    def __init__(self, approver: Approver, threshold: Permission = Permission.WRITE) -> None:
        self.approver = approver
        self.threshold = threshold

    def check(self, tool: Tool, call: ToolCall) -> bool:
        if tool.permission < self.threshold:
            return True
        return self.approver.approve(tool, call)
