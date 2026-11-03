"""B53: configurable output cap; "Request too large" fails fast instead of retrying."""

from __future__ import annotations

import json
from typing import Any

import pytest

from sebastian.config import Config
from sebastian.llm import LLMError, RetryingLLM, TransientLLMError, make_llm
from sebastian.llm.base import RetryConfig
from sebastian.llm.openai_compat import OpenAICompatLLM
from sebastian.types import Message, Role

TOO_LARGE = (
    b'{"error":{"message":"Request too large for model `qwen/qwen3.8-27b` in organization org_x '
    b"service tier `on_demand` on output tokens per minute (OTPM): Limit 1000, Requested 1922, "
    b'please reduce your message size and try again.","type":"tokens",'
    b'"code":"rate_limit_exceeded"}}'
)
OK = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}}]})


class Server:
    def __init__(self, *replies: tuple[int, bytes]) -> None:
        self.replies = list(replies)
        self.sent: list[dict[str, Any]] = []

    def __call__(self, url: str, headers: dict[str, str], body: bytes, timeout: float) -> Any:
        self.sent.append(json.loads(body))
        return self.replies.pop(0)


def test_config_reads_max_output_tokens() -> None:
    assert Config.from_env({}).max_output_tokens == 4096
    assert Config.from_env({"SEBASTIAN_MAX_OUTPUT_TOKENS": "900"}).max_output_tokens == 900
    with pytest.raises(ValueError, match="SEBASTIAN_MAX_OUTPUT_TOKENS"):
        Config.from_env({"SEBASTIAN_MAX_OUTPUT_TOKENS": "0"})


def test_cap_is_sent_on_the_wire() -> None:
    server = Server((200, OK.encode()))
    OpenAICompatLLM("m", "http://localhost:1/v1", max_tokens=900, transport=server).complete(
        "s", [Message(Role.USER, "x")], []
    )
    assert server.sent[0]["max_tokens"] == 900


def test_invalid_cap_is_rejected() -> None:
    with pytest.raises(LLMError, match="max_tokens"):
        OpenAICompatLLM("m", "http://localhost:1/v1", max_tokens=0)


@pytest.mark.parametrize("status", [413, 429])
def test_request_too_large_is_permanent_and_names_the_setting(status: int) -> None:
    server = Server((status, TOO_LARGE), (200, OK.encode()))
    llm = RetryingLLM(
        OpenAICompatLLM("m", "http://localhost:1/v1", transport=server),
        RetryConfig(sleep=lambda _: None),
    )
    with pytest.raises(LLMError, match="SEBASTIAN_MAX_OUTPUT_TOKENS") as info:
        llm.complete("s", [Message(Role.USER, "x")], [])
    assert not isinstance(info.value, TransientLLMError)
    assert "max_tokens=4096" in str(info.value)
    assert len(server.sent) == 1  # not retried


def test_ordinary_429_is_still_transient() -> None:
    server = Server((429, b'{"error":{"message":"Rate limit reached"}}'))
    with pytest.raises(TransientLLMError):
        OpenAICompatLLM("m", "http://localhost:1/v1", transport=server).complete(
            "s", [Message(Role.USER, "x")], []
        )


def _inner(llm: Any) -> OpenAICompatLLM:
    assert isinstance(llm, RetryingLLM)
    inner = llm.inner
    assert isinstance(inner, OpenAICompatLLM)
    return inner


def test_make_llm_uses_explicit_cap_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "http://localhost:11434/v1"
    assert _inner(make_llm("m", "openai", url, max_output_tokens=700)).max_tokens == 700
    monkeypatch.setenv("SEBASTIAN_MAX_OUTPUT_TOKENS", "800")
    assert _inner(make_llm("m", "openai", url)).max_tokens == 800
    monkeypatch.delenv("SEBASTIAN_MAX_OUTPUT_TOKENS")
    assert _inner(make_llm("m", "openai", url)).max_tokens == 4096
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert _inner(make_llm("m", "openai", max_output_tokens=600)).max_tokens == 600
