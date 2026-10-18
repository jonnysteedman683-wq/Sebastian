"""Governor: the single authority that decides when a run must stop.

Every run is bounded by steps, tokens and wall-clock time, so termination is guaranteed.
"""

from __future__ import annotations

from dataclasses import dataclass

from sebastian.clock import Clock
from sebastian.types import Usage


@dataclass(frozen=True, slots=True)
class Budget:
    max_steps: int = 25
    max_tokens: int = 200_000
    max_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.max_steps < 1 or self.max_tokens < 1 or self.max_seconds <= 0:
            raise ValueError("budget limits must be positive")


class Governor:
    def __init__(self, budget: Budget, clock: Clock) -> None:
        self.budget = budget
        self._clock = clock
        self._start = clock.monotonic()
        self.steps = 0
        self.usage = Usage()

    def record_step(self, usage: Usage) -> None:
        self.steps += 1
        self.usage = self.usage + usage

    @property
    def elapsed(self) -> float:
        return self._clock.monotonic() - self._start

    def exhausted(self) -> str | None:
        """Return the reason the budget is spent, or ``None`` if the run may continue."""
        if self.steps >= self.budget.max_steps:
            return f"step limit {self.budget.max_steps} reached"
        if self.usage.total >= self.budget.max_tokens:
            return f"token limit {self.budget.max_tokens} reached"
        if self.elapsed >= self.budget.max_seconds:
            return f"time limit {self.budget.max_seconds}s reached"
        return None
