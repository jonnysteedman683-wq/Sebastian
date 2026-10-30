"""Provider-agnostic LLM interface plus a retry wrapper."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from sebastian.tools.base import Tool
from sebastian.types import LLMResponse, Message


class LLMError(Exception):
    """Base class for model-call failures."""


class TransientLLMError(LLMError):
    """Retryable failure: rate limit, overload, timeout, connection reset."""


class LLM(Protocol):
    """Anything that can answer one conversation turn, optionally with tool calls."""

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> LLMResponse:
        """Return the model's next turn for ``messages`` with ``tools`` available."""


@dataclass
class RetryConfig:
    """Configuration for retrying LLM calls."""
    attempts: int = 4
    base_delay: float = 1.0
    max_delay: float = 30.0
    sleep: Callable[[float], None] = time.sleep
    rng: random.Random | None = None

    def __post_init__(self) -> None:
        if self.attempts < 1:
            raise ValueError("attempts must be >= 1")

class RetryingLLM:
    """Retries transient errors with exponential backoff and full jitter."""

    def __init__(
        self,
        inner: LLM,
        config: RetryConfig | None = None,
    ) -> None:
        self.inner = inner
        self.config = config or RetryConfig()
        self._rng = self.config.rng or random.Random()  # noqa: S311 - jitter, not security

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> LLMResponse:
        """Call the inner model, retrying transient errors."""
        for attempt in range(self.config.attempts):
            try:
                return self.inner.complete(system, messages, tools)
            except TransientLLMError:
                if attempt == self.config.attempts - 1:
                    raise
                cap = min(self.config.max_delay, self.config.base_delay * 2**attempt)
                self.config.sleep(self._rng.uniform(0, cap))
        raise AssertionError("unreachable")  # pragma: no cover
