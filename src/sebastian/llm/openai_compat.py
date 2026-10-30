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

Which key goes where (P1-19): ``OPENAI_API_KEY`` is sent only to ``api.openai.com``. Any other
host gets the key from an environment variable the user names for it (``key_env``: the
leaderboard's ``#ENV_NAME`` suffix or ``SEBASTIAN_API_KEY_ENV``), or no key at all. Names that
hold other credentials (``ANTHROPIC_API_KEY``, ``GH_*``, ``GITHUB_*``) are refused. Redirects
are never followed, so a key can't be forwarded to a host it wasn't meant for.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import ssl
from collections.abc import Callable, Sequence
from typing import Any
from urllib.parse import urlsplit

from sebastian.llm.base import LLMError, TransientLLMError
from sebastian.tools.base import Tool
from sebastian.types import LLMResponse, Message, Role, ToolCall, Usage

USER_AGENT = "sebastian (+https://github.com/jonnysteedman683-wq/Sebastian)"
API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_HOST = "api.openai.com"
KEY_ENV_SETTING = "SEBASTIAN_API_KEY_ENV"
_FORBIDDEN_KEY_ENVS = frozenset({"ANTHROPIC_API_KEY"})
_FORBIDDEN_KEY_ENV_PREFIXES = ("GH_", "GITHUB_")
_TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_MAX_ERROR_BODY = 500

# (url, headers, body, timeout) -> (status, response body). Swappable for tests.
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


def _http_transport(
    url: str, headers: dict[str, str], body: bytes, timeout: float
) -> tuple[int, bytes]:
    parts = urlsplit(url)
    try:
        if parts.scheme == "https":
            context = ssl.create_default_context()
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                parts.netloc, timeout=timeout, context=context
            )
        else:
            conn = http.client.HTTPConnection(parts.netloc, timeout=timeout)

        path = parts.path
        if parts.query:
            path += "?" + parts.query

        try:
            conn.request("POST", path or "/", body=body, headers=headers)
            resp = conn.getresponse()
            status = int(resp.status)

            if status in (301, 302, 303, 307, 308):
                newurl = resp.getheader("Location") or ""
                raise LLMError(
                    f"HTTP {status} redirect to {urlsplit(newurl).netloc or newurl!r} refused; "
                    "set the final URL as the base URL"
                )

            return status, resp.read()
        finally:
            conn.close()
    except (http.client.HTTPException, TimeoutError, ConnectionError, OSError) as exc:
        raise TransientLLMError(f"cannot reach {urlsplit(url).netloc}: {exc}") from exc


def check_base_url(base_url: str) -> str:
    """Normalise ``base_url``; refuse plain http to anything but a loopback host."""
    parts = urlsplit(base_url.strip())
    host = parts.hostname or ""
    if parts.scheme not in {"http", "https"} or not host:
        raise LLMError(f"base URL must be http(s)://host[:port][/path], got {base_url!r}")
    if parts.username or parts.password:
        raise LLMError(
            "don't put credentials in the base URL; name the key's environment variable with "
            f"{KEY_ENV_SETTING} (or #ENV_NAME in a leaderboard spec)"
        )
    if parts.scheme == "http" and not _is_loopback(host):
        raise LLMError("plain http is only allowed for localhost; use https for remote servers")
    return base_url.strip().rstrip("/")


def check_key_env(name: str) -> str:
    """Validate the name of an environment variable that holds an API key."""
    name = name.strip()
    if not name.isidentifier() or not name.isascii():
        raise LLMError(f"API key variable must be an environment variable name, got {name!r}")
    upper = name.upper()
    if upper in _FORBIDDEN_KEY_ENVS or upper.startswith(_FORBIDDEN_KEY_ENV_PREFIXES):
        raise LLMError(f"refusing to send {name} to an OpenAI-compatible server")
    return name


def resolve_api_key(base_url: str, key_env: str = "") -> str:
    """The key to send to ``base_url``: the named variable, else OPENAI_API_KEY for OpenAI only."""
    if key_env:
        return os.environ.get(check_key_env(key_env), "")
    if (urlsplit(base_url).hostname or "").lower() == OPENAI_HOST:
        return os.environ.get(API_KEY_ENV, "")
    return ""


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
        key_env: str = "",
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
        self._api_key = api_key if api_key is not None else resolve_api_key(self.base_url, key_env)
        self._transport = transport or _http_transport

    def preflight(self) -> None:
        """Raise ``LLMError`` when this client cannot work, before any tokens are spent (P2-12)."""
        if (urlsplit(self.base_url).hostname or "").lower() == "api.openai.com" and not (
            self._api_key
        ):
            raise LLMError(f"{API_KEY_ENV} is empty but {self.base_url} needs a key")

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
        # An explicit User-Agent: Cloudflare-fronted hosts (e.g. Groq) reject urllib's default
        # "Python-urllib/3.x" with error 1010 before the request reaches the API.
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
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
            raise LLMError(f"HTTP {status}: {_snippet(body)}{self._key_hint(status)}")
        return parse_response(body)

    def _key_hint(self, status: int) -> str:
        if status not in {401, 403} or self._api_key:
            return ""
        host = urlsplit(self.base_url).hostname
        return (
            f" (no API key was sent to {host}; name its variable with {KEY_ENV_SETTING} "
            "or #ENV_NAME in a leaderboard spec)"
        )


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
        usage=Usage(
            _count(usage, "prompt_tokens"),
            _count(usage, "completion_tokens"),
            unreported=0 if _reported(usage) else 1,
        ),
    )


def _reported(usage: Any) -> bool:
    """True when the server gave valid prompt and completion token counts (P2-15)."""
    if not isinstance(usage, dict):
        return False
    return all(
        type(usage.get(k)) is int and usage[k] >= 0 for k in ("prompt_tokens", "completion_tokens")
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
