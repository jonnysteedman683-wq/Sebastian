"""Provider-agnostic LLM interface plus a retry wrapper."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from typing import Protocol

from sebastian.tools.base import Tool
from sebastian.types import LLMResponse, Message


class LLMError(Exception):
    """Base class for model-call failures."""


class TransientLLMError(LLMError):
    """Retryable failure: rate limit, overload, timeout, connection reset."""


class LLM(Protocol):
    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> LLMResponse: ...


class RetryingLLM:
    """Retries transient errors with exponential backoff and full jitter."""

    def __init__(
        self,
        inner: LLM,
        attempts: int = 4,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        if attempts < 1:
            raise ValueError("attempts must be >= 1")
        self.inner = inner
        self.attempts = attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 - jitter, not security

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> LLMResponse:
        for attempt in range(self.attempts):
            try:
                return self.inner.complete(system, messages, tools)
            except TransientLLMError:
                if attempt == self.attempts - 1:
                    raise
                cap = min(self.max_delay, self.base_delay * 2**attempt)
                self._sleep(self._rng.uniform(0, cap))
        raise AssertionError("unreachable")  # pragma: no cover
