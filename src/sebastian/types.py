"""Core value types shared across Sebastian. Immutable, provider-agnostic."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

JSON = dict[str, Any]


class Role(StrEnum):
    """Who sent a message."""

    USER = "user"
    ASSISTANT = "assistant"


class StopReason(StrEnum):
    """Why a run ended. Exactly one is reported per run."""

    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ERROR = "error"
    NEEDS_HELP = "needs_help"  # the Gauntlet could not prove the task done (B11d)


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool call requested by the model."""

    id: str
    name: str
    arguments: JSON


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The result of one tool call, matched to it by ``call_id``."""

    call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class Message:
    """One conversation turn. Assistant turns may carry tool calls; user turns may carry results."""

    role: Role
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()


@dataclass(frozen=True, slots=True)
class Usage:
    """Token usage for one or more model calls."""

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        """Input plus output tokens."""
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens
        )


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """A model reply. ``tool_calls`` empty means the model considers the task finished."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True, slots=True)
class RunResult:
    """The outcome of one run."""

    run_id: str
    stop_reason: StopReason
    answer: str
    steps: int
    usage: Usage
    detail: str = ""
