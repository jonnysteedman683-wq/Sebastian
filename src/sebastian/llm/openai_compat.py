"""OpenAI-compatible Chat Completions backend (B37). No extra dependencies.

Speaks ``POST {base_url}/chat/completions`` as served by OpenAI and by Ollama, vLLM, LM Studio,
llama.cpp's server, OpenRouter and others. Uses only the standard library.

Wire format (verified against provider docs, Oct 2026): ``tools`` entries are
``{"type": "function", "function": {"name", "description", "parameters"}}``; an assistant turn
carries ``tool_calls`` of ``{"id", "type": "function", "function": {"name", "arguments"}}`` where
``arguments`` is a JSON *string*; each result goes back as ``{"role": "tool", "tool_call_id",
"content"}``. Token counts come from ``usage.prompt_tokens`` / ``usage.completion_tokens`` when
the server reports them.

Prompts carry workspace contents, so plain ``http://`` is accepted only for a loopback host;
anything remote must use ``https://``. The API key is sent only as a bearer header and never
logged.
"""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from sebastian.llm.base import LLMError, TransientLLMError
from sebastian.tools.base import Tool
from sebastian.types import LLMResponse, Message, Role, ToolCall, Usage

API_KEY_ENV = "OPENAI_API_KEY"
_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_MAX_ERROR_BODY = 500

# (url, headers, body, timeout) -> (status, response body). Swappable for tests.
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


def _urllib_transport(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme checked
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise TransientLLMError(f"cannot reach {urlsplit(url).netloc}: {exc}") from exc


def check_base_url(base_url: str) -> str:
    """Normalise ``base_url``; refuse plain http to anything but a loopback host."""
    parts = urlsplit(base_url.strip())
    host = parts.hostname or ""
    if parts.scheme not in {"http", "https"} or not host:
        raise LLMError(f"base URL must be http(s)://host[:port][/path], got {base_url!r}")
    if parts.username or parts.password:
        raise LLMError("put the API key in OPENAI_API_KEY, not in the base URL")
    if parts.scheme == "http" and not _is_loopback(host):
        raise LLMError("plain http is only allowed for localhost; use https for remote servers")
    return base_url.strip().rstrip("/")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class OpenAICompatLLM:
    """Any OpenAI-compatible chat endpoint, with tool calling."""

    def __init__(
        self,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        max_tokens: int = 4096,
        timeout: float = 300.0,
        transport: Transport | None = None,
    ) -> None:
        if not model.strip():
            raise LLMError("model name must not be empty")
        self.model = model
        self.base_url = check_base_url(base_url)
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")
        self._transport = transport or _urllib_transport

    def __repr__(self) -> str:  # never show the key
        return f"OpenAICompatLLM(model={self.model!r}, base_url={self.base_url!r})"

    @staticmethod
    def to_wire(system: str, messages: Sequence[Message]) -> list[dict[str, Any]]:
        """Sebastian's history as Chat Completions messages."""
        wire: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            if m.role is Role.ASSISTANT:
                turn: dict[str, Any] = {"role": "assistant", "content": m.text or None}
                if m.tool_calls:
                    turn["tool_calls"] = [
                        {
                            "id": c.id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                        }
                        for c in m.tool_calls
                    ]
                wire.append(turn)
                continue
            # Tool results must directly follow the assistant turn that asked for them.
            wire += [
                {"role": "tool", "tool_call_id": r.call_id, "content": r.content}
                for r in m.tool_results
            ]
            if m.text:
                wire.append({"role": "user", "content": m.text})
        return wire

    @staticmethod
    def tools_to_wire(tools: Sequence[Tool]) -> list[dict[str, Any]]:
        """Tool definitions in the ``{"type": "function", ...}`` form."""
        return [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.schema},
            }
            for t in tools
        ]

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> LLMResponse:
        """Send one request. Network failures and 429/5xx raise ``TransientLLMError``."""
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": self.to_wire(system, messages),
        }
        if tools:  # tool-free calls (plan, predict, critic) omit the field entirely
            payload["tools"] = self.tools_to_wire(tools)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        status, body = self._transport(
            self.base_url + "/chat/completions",
            headers,
            json.dumps(payload).encode("utf-8"),
            self.timeout,
        )
        if status in _TRANSIENT_STATUS:
            raise TransientLLMError(f"HTTP {status}: {_snippet(body)}")
        if not 200 <= status < 300:
            raise LLMError(f"HTTP {status}: {_snippet(body)}")
        return parse_response(body)


def _snippet(body: bytes) -> str:
    return body[:_MAX_ERROR_BODY].decode("utf-8", errors="replace")


def parse_response(body: bytes) -> LLMResponse:
    """Turn a Chat Completions response into an ``LLMResponse``."""
    try:
        data = json.loads(body)
        message = data["choices"][0]["message"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"unexpected response shape: {_snippet(body)}") from exc
    if not isinstance(message, dict):
        raise LLMError("unexpected response shape: message is not an object")
    text = message.get("content") or ""
    calls = []
    for i, raw in enumerate(message.get("tool_calls") or []):
        if not isinstance(raw, dict):
            raise LLMError(f"tool call {i} is not an object")
        fn_raw = raw.get("function")
        fn: dict[str, Any] = fn_raw if isinstance(fn_raw, dict) else {}
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            raise LLMError(f"tool call {i} has no function name")
        call_id = raw.get("id") or f"call_{i}"
        calls.append(ToolCall(str(call_id), name, _arguments(fn.get("arguments"))))
    usage = data.get("usage") or {}
    return LLMResponse(
        text=text if isinstance(text, str) else "",
        tool_calls=tuple(calls),
        usage=Usage(_count(usage, "prompt_tokens"), _count(usage, "completion_tokens")),
    )


def _arguments(raw: Any) -> dict[str, Any]:
    """Parse the arguments string. Bad JSON becomes an argument the tool will reject by name,
    so the model sees why its call failed instead of the run crashing."""
    if isinstance(raw, dict):
        return raw
    if raw in (None, ""):
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"invalid_json_arguments": str(raw)[:200]}
    if parsed is None:  # some servers send the JSON literal null for no arguments
        return {}
    return parsed if isinstance(parsed, dict) else {"invalid_json_arguments": str(raw)[:200]}


def _count(usage: Any, key: str) -> int:
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if type(value) is int and value >= 0 else 0
