"""Context management: keep the conversation sent to the model bounded.

Without this, every tool result stays in the history forever and long tasks run out of tokens
no matter how capable the model is. Compaction is deterministic and LLM-free:

1. Every tool result is capped at ``max_result_chars`` (head + tail kept) when it is produced.
2. When the history exceeds ``max_history_chars``, tool results older than the last
   ``keep_recent`` turns are replaced by a short stub, oldest first, until it fits.

The first message (the task) and all assistant text are never touched, and every tool result
keeps its ``call_id``, so tool_use/tool_result pairing stays valid for every provider.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from sebastian.types import Message, ToolResult

ELIDED_PREFIX = "[elided"


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """Limits for the history sent to the model. Sizes are in characters (~4 per token)."""

    max_result_chars: int = 12_000
    max_history_chars: int = 240_000
    keep_recent: int = 6

    def __post_init__(self) -> None:
        if self.max_result_chars < 200 or self.max_history_chars < 1_000 or self.keep_recent < 0:
            raise ValueError("context limits too small")


def cap_result(result: ToolResult, limit: int) -> ToolResult:
    """Cap one tool result, keeping its head and tail so errors at either end survive."""
    text = result.content
    if len(text) <= limit:
        return result
    marker = f"\n...[{len(text) - limit} chars truncated]...\n"
    keep = max(limit - len(marker), 0)
    head = text[: keep * 2 // 3]
    tail = text[len(text) - (keep - len(head)) :] if keep > len(head) else ""
    return replace(result, content=head + marker + tail)


def history_chars(messages: Sequence[Message]) -> int:
    """Approximate size of a history: all text, tool arguments and tool results."""
    total = 0
    for m in messages:
        total += len(m.text)
        total += sum(len(c.name) + len(str(c.arguments)) for c in m.tool_calls)
        total += sum(len(r.content) for r in m.tool_results)
    return total


def _stub(result: ToolResult) -> ToolResult:
    if result.content.startswith(ELIDED_PREFIX):
        return result
    status = "error" if result.is_error else "ok"
    note = f"{ELIDED_PREFIX}: old {status} result, {len(result.content)} chars; "
    return replace(result, content=note + "repeat the call if you still need it]")


def compact(messages: Sequence[Message], policy: ContextPolicy) -> tuple[list[Message], int]:
    """Return ``(history, n_elided)`` with old tool results stubbed until it fits the policy."""
    out = list(messages)
    if history_chars(out) <= policy.max_history_chars:
        return out, 0
    protected_from = max(len(out) - policy.keep_recent, 1)
    elided = 0
    for i in range(1, protected_from):
        msg = out[i]
        if not msg.tool_results or all(
            r.content.startswith(ELIDED_PREFIX) for r in msg.tool_results
        ):
            continue
        out[i] = replace(msg, tool_results=tuple(_stub(r) for r in msg.tool_results))
        elided += 1
        if history_chars(out) <= policy.max_history_chars:
            break
    return out, elided
