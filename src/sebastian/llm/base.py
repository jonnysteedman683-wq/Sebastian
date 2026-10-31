"""Provider-agnostic LLM interface plus a retry wrapper."""

from __future__ import annotations

import email.utils
import random
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from sebastian.tools.base import Tool
from sebastian.types import LLMResponse, Message


class LLMError(Exception):
    """Base class for model-call failures."""


class TransientLLMError(LLMError):
    """Retryable failure: rate limit, overload, timeout, connection reset.

    ``retry_after`` is the server's requested wait in seconds (B52), when it gave one.
    """

    def __init__(self, message: str = "", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


MAX_RETRY_AFTER = 3600.0  # ignore absurd server delays; RetryConfig.max_delay clamps further
_TRY_AGAIN = re.compile(
    r"try again in\s+(?:(\d+(?:\.\d+)?)m)?(\d+(?:\.\d+)?)(ms|s)\b", re.IGNORECASE
)


def parse_retry_after(
    header: str | None = None, body: str = "", now: float | None = None
) -> float | None:
    """The server's requested retry delay in seconds, or None.

    Reads the ``Retry-After`` header (delta-seconds or an HTTP date), else a provider's
    ``try again in 9.46s`` / ``1m2.5s`` / ``250ms`` text in the error body. Negative,
    unparseable or absurd (> 1 hour) values give None.
    """
    value: float | None = None
    if header and header.strip():
        h = header.strip()
        try:
            value = float(h)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(h)
            except (TypeError, ValueError):
                when = None
            if when is not None:
                value = when.timestamp() - (time.time() if now is None else now)
    if value is None and body:
        m = _TRY_AGAIN.search(body)
        if m:
            mins, num, unit = m.groups()
            value = float(num) / (1000 if unit.lower() == "ms" else 1) + float(mins or 0) * 60
    if value is None or value < 0 or value > MAX_RETRY_AFTER:
        return None
    return value


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
    """Retries transient errors with exponential backoff and full jitter.

    When the error carries the server's ``retry_after``, each wait is at least that long
    (clamped to ``max_delay``).
    """

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
            except TransientLLMError as exc:
                if attempt == self.config.attempts - 1:
                    raise
                cap = min(self.config.max_delay, self.config.base_delay * 2**attempt)
                delay = self._rng.uniform(0, cap)
                if exc.retry_after is not None:  # B52: never retry sooner than the server asked
                    delay = max(delay, min(exc.retry_after, self.config.max_delay))
                self.config.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover
