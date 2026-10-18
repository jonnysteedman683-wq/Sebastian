from __future__ import annotations

import random
from typing import Any

import pytest

from sebastian.llm import LLMError, RetryingLLM, ScriptedLLM, TransientLLMError
from sebastian.types import LLMResponse, Message, Role

# --- retry ----------------------------------------------------------------


class Flaky:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def complete(self, system: str, messages: Any, tools: Any) -> LLMResponse:
        self.calls += 1
        if self.calls <= self.failures:
            raise TransientLLMError("overloaded")
        return LLMResponse("ok")


def test_retry_recovers_with_bounded_backoff() -> None:
    sleeps: list[float] = []
    llm = RetryingLLM(
        Flaky(2), attempts=4, base_delay=1, max_delay=3, sleep=sleeps.append, rng=random.Random(0)
    )
    assert llm.complete("s", [], []).text == "ok"
    assert len(sleeps) == 2
    assert 0 <= sleeps[0] <= 1
    assert 0 <= sleeps[1] <= 2


def test_retry_gives_up() -> None:
    inner = Flaky(10)
    llm = RetryingLLM(inner, attempts=3, sleep=lambda _: None)
    with pytest.raises(TransientLLMError):
        llm.complete("s", [], [])
    assert inner.calls == 3


def test_retry_does_not_retry_permanent_errors() -> None:
    class Bad:
        calls = 0

        def complete(self, *_: Any) -> LLMResponse:
            Bad.calls += 1
            raise LLMError("auth")

    with pytest.raises(LLMError, match="auth"):
        RetryingLLM(Bad(), sleep=lambda _: None).complete("s", [], [])
    assert Bad.calls == 1


def test_retry_validates_attempts() -> None:
    with pytest.raises(ValueError, match="attempts"):
        RetryingLLM(Flaky(0), attempts=0)


def test_scripted_llm_callable_and_exhaustion() -> None:
    llm = ScriptedLLM([lambda msgs: LLMResponse(f"saw {len(msgs)}")])
    assert llm.complete("s", [Message(Role.USER, "hi")], []).text == "saw 1"
    with pytest.raises(LLMError, match="exhausted"):
        llm.complete("s", [], [])
