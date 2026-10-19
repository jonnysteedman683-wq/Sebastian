"""Governor: the single authority that decides when a run must stop.

Every run is bounded by steps, tokens and wall-clock time, so termination is guaranteed. An
optional dollar limit (``max_cost``, B40) stops a run once its priced usage reaches it.
"""

from __future__ import annotations

from dataclasses import dataclass

from sebastian.clock import Clock
from sebastian.types import Usage

WARN_FRACTION = 0.8


@dataclass(frozen=True, slots=True)
class Budget:
    """Hard limits for one run: steps, tokens and wall-clock seconds."""

    max_steps: int = 25
    max_tokens: int = 200_000
    max_seconds: float = 600.0
    max_cost: float = 0.0  # USD; 0 means no dollar limit

    def __post_init__(self) -> None:
        if self.max_steps < 1 or self.max_tokens < 1 or self.max_seconds <= 0:
            raise ValueError("budget limits must be positive")
        if self.max_cost < 0:
            raise ValueError("max_cost must not be negative")


class Governor:
    """Tracks a run's usage against its ``Budget``."""

    def __init__(self, budget: Budget, clock: Clock) -> None:
        self.budget = budget
        self._clock = clock
        self._start = clock.monotonic()
        self.steps = 0
        self.usage = Usage()
        self._warned: set[str] = set()

    def record_step(self, usage: Usage) -> None:
        """Count one model call and add its token usage."""
        self.steps += 1
        self.usage = self.usage + usage

    @property
    def elapsed(self) -> float:
        """Seconds since the run started, by the injected clock."""
        return self._clock.monotonic() - self._start

    def new_warnings(self) -> list[tuple[str, float, float]]:
        """Limits that just reached ``WARN_FRACTION`` of their budget, each reported once per run.

        Returns ``(limit, used, max)`` tuples, with ``limit`` one of steps, tokens, seconds, cost.
        """
        b = self.budget
        current: list[tuple[str, float, float]] = [
            ("steps", self.steps, b.max_steps),
            ("tokens", self.usage.total, b.max_tokens),
            ("seconds", self.elapsed, b.max_seconds),
        ]
        if b.max_cost:
            current.append(("cost", self.usage.cost, b.max_cost))
        fresh = [c for c in current if c[0] not in self._warned and c[1] >= WARN_FRACTION * c[2]]
        self._warned.update(name for name, _, _ in fresh)
        return fresh

    def exhausted(self) -> str | None:
        """Return the reason the budget is spent, or ``None`` if the run may continue."""
        if self.steps >= self.budget.max_steps:
            return f"step limit {self.budget.max_steps} reached"
        if self.usage.total >= self.budget.max_tokens:
            return f"token limit {self.budget.max_tokens} reached"
        if self.elapsed >= self.budget.max_seconds:
            return f"time limit {self.budget.max_seconds}s reached"
        if self.budget.max_cost and self.usage.cost >= self.budget.max_cost:
            return f"cost limit ${self.budget.max_cost:.2f} reached"
        return None
