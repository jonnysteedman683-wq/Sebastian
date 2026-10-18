"""Time and randomness seams. Production uses real sources; tests inject deterministic ones."""

from __future__ import annotations

import random
import time
import uuid
from typing import Protocol


class Clock(Protocol):
    """Source of time; injected so tests are deterministic."""

    def monotonic(self) -> float:
        """Monotonic seconds, for durations."""

    def now(self) -> float:
        """Wall-clock epoch seconds, for timestamps."""


class SystemClock:
    """The real clock."""

    def monotonic(self) -> float:
        """Return ``time.monotonic()``."""
        return time.monotonic()

    def now(self) -> float:
        """Return ``time.time()``."""
        return time.time()


class FakeClock:
    """Manually advanced clock for deterministic tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def monotonic(self) -> float:
        """Return the fake time."""
        return self._t

    def now(self) -> float:
        """Return the fake time."""
        return self._t

    def advance(self, seconds: float) -> None:
        """Move time forward by ``seconds`` (never backwards)."""
        if seconds < 0:
            raise ValueError("clock cannot go backwards")
        self._t += seconds


class IdSource(Protocol):
    """Source of run IDs; injected so traces are reproducible."""

    def new_id(self) -> str:
        """Return a fresh ID."""


class UuidSource:
    """Random UUID4 hex IDs."""

    def new_id(self) -> str:
        """Return a new UUID4 hex string."""
        return uuid.uuid4().hex


class SeededIdSource:
    """Deterministic IDs for reproducible traces in tests."""

    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random(seed)  # noqa: S311 - not security-sensitive

    def new_id(self) -> str:
        """Return the next ID from the seeded generator."""
        return f"{self._rng.getrandbits(64):016x}"
