"""Tests for Governor and Budget limits."""

from __future__ import annotations

import pytest

from sebastian.budget import Budget, Governor
from sebastian.clock import FakeClock
from sebastian.types import Usage


def test_budget_validation() -> None:
    with pytest.raises(ValueError, match="budget limits must be positive"):
        Budget(max_steps=0)
    with pytest.raises(ValueError, match="budget limits must be positive"):
        Budget(max_tokens=-1)
    with pytest.raises(ValueError, match="budget limits must be positive"):
        Budget(max_seconds=0.0)
    with pytest.raises(ValueError, match="max_cost must not be negative"):
        Budget(max_cost=-0.1)

    # Valid limits
    b = Budget(max_steps=10, max_tokens=1000, max_seconds=10.0, max_cost=0.0)
    assert b.max_steps == 10


def test_governor_tracking() -> None:
    clock = FakeClock()
    budget = Budget(max_steps=10, max_tokens=100, max_seconds=10.0, max_cost=10.0)
    gov = Governor(budget, clock)

    assert gov.steps == 0
    assert gov.usage == Usage()
    assert gov.elapsed == 0.0

    gov.record_step(Usage(10, 5, 2.0))
    clock.advance(2.5)

    assert gov.steps == 1
    assert gov.usage == Usage(10, 5, 2.0)
    assert gov.elapsed == 2.5

    gov.record_step(Usage(20, 10, 3.0))
    assert gov.steps == 2
    assert gov.usage == Usage(30, 15, 5.0)


def test_governor_warnings() -> None:
    clock = FakeClock()
    budget = Budget(max_steps=10, max_tokens=100, max_seconds=10.0, max_cost=10.0)
    gov = Governor(budget, clock)

    assert gov.new_warnings() == []

    # Hit 80% steps
    for _ in range(8):
        gov.record_step(Usage(0, 0, 0.0))

    warnings = gov.new_warnings()
    assert len(warnings) == 1
    assert warnings[0] == ("steps", 8, 10)

    # Should not warn again
    assert gov.new_warnings() == []

    # Hit 80% tokens
    gov.record_step(Usage(40, 40, 0.0))
    warnings = gov.new_warnings()
    assert len(warnings) == 1
    assert warnings[0] == ("tokens", 80, 100)

    # Hit 80% seconds
    clock.advance(8.0)
    warnings = gov.new_warnings()
    assert len(warnings) == 1
    assert warnings[0] == ("seconds", 8.0, 10.0)

    # Hit 80% cost
    gov.record_step(Usage(0, 0, 8.0))
    warnings = gov.new_warnings()
    assert len(warnings) == 1
    assert warnings[0] == ("cost", 8.0, 10.0)


def test_governor_exhausted() -> None:
    clock = FakeClock()
    budget = Budget(max_steps=10, max_tokens=100, max_seconds=10.0, max_cost=10.0)

    # Check steps
    gov1 = Governor(budget, clock)
    for _ in range(9):
        gov1.record_step(Usage())
    assert gov1.exhausted() is None
    gov1.record_step(Usage())
    assert gov1.exhausted() == "step limit 10 reached"

    # Check tokens
    gov2 = Governor(budget, clock)
    gov2.record_step(Usage(50, 49, 0.0))
    assert gov2.exhausted() is None
    gov2.record_step(Usage(0, 1, 0.0))
    assert gov2.exhausted() == "token limit 100 reached"

    # Check time
    gov3 = Governor(budget, clock)
    clock.advance(9.9)
    assert gov3.exhausted() is None
    clock.advance(0.1)
    assert gov3.exhausted() == "time limit 10.0s reached"

    # Check cost
    gov4 = Governor(budget, clock)
    gov4.record_step(Usage(0, 0, 9.9))
    assert gov4.exhausted() is None
    gov4.record_step(Usage(0, 0, 0.1))
    assert gov4.exhausted() == "cost limit $10.00 reached"


def test_governor_warnings_without_cost() -> None:
    clock = FakeClock()
    budget = Budget(max_steps=10, max_tokens=100, max_seconds=10.0, max_cost=0.0)
    gov = Governor(budget, clock)

    assert gov.new_warnings() == []

    # Hit 80% steps
    for _ in range(8):
        gov.record_step(Usage(0, 0, 0.0))

    warnings = gov.new_warnings()
    assert len(warnings) == 1
    assert warnings[0] == ("steps", 8, 10)
