"""Tests for types in src/sebastian/types.py."""

from sebastian.types import Usage

def test_usage_total():
    assert Usage(input_tokens=10, output_tokens=20).total == 30
    assert Usage(input_tokens=0, output_tokens=0).total == 0
    assert Usage(input_tokens=5, output_tokens=0).total == 5
    assert Usage(input_tokens=0, output_tokens=8).total == 8

def test_usage_add():
    u1 = Usage(input_tokens=10, output_tokens=20, cost=0.05, unreported=1)
    u2 = Usage(input_tokens=5, output_tokens=15, cost=0.02, unreported=0)

    result = u1 + u2

    assert result.input_tokens == 15
    assert result.output_tokens == 35
    assert result.cost == 0.07
    assert result.unreported == 1

def test_usage_add_zeros():
    u1 = Usage()
    u2 = Usage(input_tokens=5, output_tokens=15, cost=0.02, unreported=0)

    result = u1 + u2

    assert result.input_tokens == 5
    assert result.output_tokens == 15
    assert result.cost == 0.02
    assert result.unreported == 0

def test_usage_multiple_add():
    u1 = Usage(input_tokens=10, output_tokens=20)
    u2 = Usage(input_tokens=5, output_tokens=15)
    u3 = Usage(input_tokens=2, output_tokens=3)

    result = u1 + u2 + u3

    assert result.input_tokens == 17
    assert result.output_tokens == 38
