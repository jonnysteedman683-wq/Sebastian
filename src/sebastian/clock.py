"""Time and randomness seams. Production uses real sources; tests inject deterministic ones."""

from __future__ import annotations

import random
import time
import uuid
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def now(self) -> float: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> float:
        return time.time()


class FakeClock:
    """Manually advanced clock for deterministic tests."""

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def monotonic(self) -> float:
        return self._t

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("clock cannot go backwards")
        self._t += seconds


class IdSource(Protocol):
    def new_id(self) -> str: ...


class UuidSource:
    def new_id(self) -> str:
        return uuid.uuid4().hex


class SeededIdSource:
    """Deterministic IDs for reproducible traces in tests."""

    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random(seed)  # noqa: S311 - not security-sensitive

    def new_id(self) -> str:
        return f"{self._rng.getrandbits(64):016x}"
