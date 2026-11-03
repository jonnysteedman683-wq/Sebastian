"""B52: honour the provider's retry delay on HTTP 429."""

from __future__ import annotations

import email.utils
import io
import json
import urllib.error
from collections.abc import Sequence
from typing import Any

import pytest

import sebastian.llm.openai_compat as oc
from sebastian.llm.base import (
    MAX_RETRY_AFTER,
    RetryConfig,
    RetryingLLM,
    TransientLLMError,
    parse_retry_after,
)
from sebastian.llm.openai_compat import OpenAICompatLLM
from sebastian.tools.base import Tool
from sebastian.types import LLMResponse, Message, Role

GROQ_429 = (
    '{"error":{"message":"Rate limit reached for model `qwen/qwen3.8-27b` in organization '
    "org_x on tokens per minute (TPM): Limit 6000, Used 5800. Please try again in "
    '9.462857142s. Need more tokens?","type":"tokens","code":"rate_limit_exceeded"}}'
)


@pytest.mark.parametrize(
    ("header", "body", "expected"),
    [
        ("12", "", 12.0),
        (" 2.5 ", "", 2.5),
        (None, GROQ_429, 9.462857142),
        (None, "please try again in 250ms", 0.25),
        (None, "Try again in 1m2.5s.", 62.5),
        ("3", "try again in 99s", 3.0),  # the header wins over the body
        ("soon", "", None),
        ("-4", "", None),
        (str(MAX_RETRY_AFTER + 1), "", None),
        (None, "nothing useful", None),
        ("", "", None),
    ],
)
def test_parse_retry_after(header: str | None, body: str, expected: float | None) -> None:
    got = parse_retry_after(header, body)
    assert got == pytest.approx(expected) if expected is not None else got is None


def test_parse_retry_after_http_date() -> None:
    now = 1_800_000_000.0
    date = email.utils.formatdate(now + 30, usegmt=True)
    assert parse_retry_after(date, now=now) == pytest.approx(30.0)
    assert parse_retry_after(email.utils.formatdate(now - 30, usegmt=True), now=now) is None


class Throttled:
    """Fails with the given retry_after values, then answers."""

    def __init__(self, *waits: float | None) -> None:
        self.waits = list(waits)

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> LLMResponse:
        if self.waits:
            raise TransientLLMError("HTTP 429", retry_after=self.waits.pop(0))
        return LLMResponse(text="ok")


def test_retry_waits_at_least_the_server_delay() -> None:
    slept: list[float] = []
    llm = RetryingLLM(
        Throttled(9.46, None), RetryConfig(attempts=3, base_delay=1, sleep=slept.append)
    )
    assert llm.complete("s", [], []).text == "ok"
    assert slept[0] >= 9.46  # jitter alone would have been <= 1 s
    assert slept[1] <= 2  # no server delay: plain jittered backoff


def test_retry_after_is_clamped_to_max_delay() -> None:
    slept: list[float] = []
    llm = RetryingLLM(Throttled(600.0), RetryConfig(max_delay=30, sleep=slept.append))
    llm.complete("s", [], [])
    assert slept == [30]


def test_last_attempt_still_raises_with_retry_after() -> None:
    llm = RetryingLLM(Throttled(5.0, 5.0), RetryConfig(attempts=2, sleep=lambda _: None))
    with pytest.raises(TransientLLMError) as info:
        llm.complete("s", [], [])
    assert info.value.retry_after == 5.0


def _client(*replies: tuple[Any, ...]) -> OpenAICompatLLM:
    queue = list(replies)

    def server(url: str, headers: dict[str, str], body: bytes, timeout: float) -> Any:
        return queue.pop(0)

    return OpenAICompatLLM("m", "http://localhost:1/v1", transport=server)


def test_openai_429_reads_header_case_insensitively() -> None:
    with pytest.raises(TransientLLMError) as info:
        _client((429, b"{}", {"Retry-After": "4"})).complete("s", [Message(Role.USER, "x")], [])
    assert info.value.retry_after == 4.0


def test_openai_429_reads_groq_body_text() -> None:
    with pytest.raises(TransientLLMError) as info:
        _client((429, GROQ_429.encode(), {})).complete("s", [Message(Role.USER, "x")], [])
    assert info.value.retry_after == pytest.approx(9.462857142)


def test_two_tuple_transports_still_work() -> None:
    """Older transports returning (status, body) keep working; no delay is known."""
    with pytest.raises(TransientLLMError) as info:
        _client((503, b"busy")).complete("s", [Message(Role.USER, "x")], [])
    assert info.value.retry_after is None
    ok = {"choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}]}
    assert _client((200, json.dumps(ok).encode())).complete("s", [], []).text == "hi"


def test_urllib_transport_keeps_only_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    hdrs = {"Retry-After": "6", "Set-Cookie": "secret=1"}

    def http_error(req: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(req.full_url, 429, "slow", hdrs, io.BytesIO(b"x"))  # type: ignore[arg-type]

    monkeypatch.setattr(oc, "_open", http_error)
    assert oc._urllib_transport("https://h/v1", {}, b"{}", 5) == (429, b"x", {"retry-after": "6"})
