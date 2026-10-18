"""Core value types shared across Sebastian. Immutable, provider-agnostic."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

JSON = dict[str, Any]


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class StopReason(StrEnum):
    """Why a run ended. Exactly one is reported per run."""

    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: JSON


@dataclass(frozen=True, slots=True)
class ToolResult:
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
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
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
    run_id: str
    stop_reason: StopReason
    answer: str
    steps: int
    usage: Usage
    detail: str = ""
