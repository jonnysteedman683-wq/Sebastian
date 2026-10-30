"""B37: OpenAI-compatible backend, tested with a fake transport (no network)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from sebastian import Sebastian, StopReason
from sebastian.approval import AllowApprover, ApprovalGate
from sebastian.clock import FakeClock, SeededIdSource
from sebastian.config import Config
from sebastian.llm import BACKENDS, LLMError, RetryingLLM, TransientLLMError, make_llm
from sebastian.llm import openai_compat as oc
from sebastian.llm.base import RetryConfig
from sebastian.llm.openai_compat import OpenAICompatLLM, check_base_url, parse_response
from sebastian.tools import ToolRegistry, Workspace, file_tools
from sebastian.types import Message, Role, ToolCall, ToolResult, Usage


class FakeServer:
    """Records requests and replays canned (status, body) replies."""

    def __init__(self, *replies: tuple[int, Any]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def __call__(
        self, url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, bytes]:
        self.requests.append(
            {"url": url, "headers": headers, "json": json.loads(body), "timeout": timeout}
        )
        status, payload = self.replies.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return status, raw


def reply(content: str | None = None, *calls: dict[str, Any], usage: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = list(calls)
    out: dict[str, Any] = {"choices": [{"index": 0, "message": message}]}
    if usage is not None:
        out["usage"] = usage
    return out


def call(cid: str, name: str, args: Any) -> dict[str, Any]:
    raw = args if isinstance(args, str) else json.dumps(args)
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": raw}}


def llm(server: FakeServer, **kw: Any) -> OpenAICompatLLM:
    return OpenAICompatLLM("qwen3", "http://localhost:11434/v1", transport=server, **kw)


# --- request shape --------------------------------------------------------------------------


def test_request_wire_format(tmp_path: Path) -> None:
    server = FakeServer((200, reply("ok", usage={"prompt_tokens": 7, "completion_tokens": 3})))
    tools = list(file_tools(Workspace(tmp_path)))[:1]
    history = [
        Message(Role.USER, text="do it"),
        Message(Role.ASSISTANT, text="", tool_calls=(ToolCall("c1", "read_file", {"path": "a"}),)),
        Message(Role.USER, text="<replan/>", tool_results=(ToolResult("c1", "ERROR: no", True),)),
    ]
    resp = llm(server, api_key="sk-test").complete("SYS", history, tools)
    assert (resp.text, resp.usage) == ("ok", Usage(7, 3))
    req = server.requests[0]
    assert req["url"] == "http://localhost:11434/v1/chat/completions"
    assert req["headers"]["Authorization"] == "Bearer sk-test"
    body = req["json"]
    assert body["model"] == "qwen3"
    assert body["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ERROR: no"},  # results first
        {"role": "user", "content": "<replan/>"},
    ]
    [tool] = body["tools"]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == tools[0].name
    assert tool["function"]["parameters"] == tools[0].schema


def test_tool_free_calls_omit_tools_and_no_key_means_no_auth_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(oc.API_KEY_ENV, raising=False)
    server = FakeServer((200, reply("plan")))
    llm(server).complete("S", [Message(Role.USER, text="t")], [])
    assert "tools" not in server.requests[0]["json"]
    assert "Authorization" not in server.requests[0]["headers"]


def test_key_comes_from_env_and_never_shows_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(oc.API_KEY_ENV, "sk-test-FAKE-env")
    server = FakeServer((200, reply("x")))
    client = OpenAICompatLLM("gpt-x", "https://api.openai.com/v1", transport=server)
    client.complete("S", [Message(Role.USER, text="t")], [])
    assert server.requests[0]["headers"]["Authorization"] == "Bearer sk-test-FAKE-env"
    assert "sk-test" not in repr(client)


def test_requests_carry_a_sebastian_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    # Cloudflare-fronted hosts (seen on api.groq.com) answer urllib's default
    # "Python-urllib/3.x" with 403 error 1010 before the request reaches the API.
    server = FakeServer((200, reply("x")))
    llm(server).complete("S", [Message(Role.USER, text="t")], [])
    sent = server.requests[0]["headers"]["User-Agent"]
    assert sent == oc.USER_AGENT
    assert sent.startswith("sebastian")

    seen: list[str | None] = []

    def capture(req: urllib.request.Request, timeout: float) -> Any:
        seen.append(req.get_header("User-agent"))  # urllib stores header names capitalised
        raise urllib.error.URLError("stop")

    monkeypatch.setattr(oc, "_open", capture)
    with pytest.raises(TransientLLMError):
        oc._urllib_transport("https://h/v1", {"User-Agent": sent}, b"{}", 5)
    assert seen == [sent]  # ours, not urllib's default


# --- response parsing -----------------------------------------------------------------------


def test_tool_calls_are_parsed() -> None:
    body = json.dumps(
        reply(
            None,
            call("a", "read_file", {"path": "x"}),
            {"type": "function", "function": {"name": "list_dir", "arguments": {"path": "."}}},
        )
    ).encode()
    resp = parse_response(body)
    assert resp.text == ""
    assert resp.tool_calls == (
        ToolCall("a", "read_file", {"path": "x"}),
        ToolCall("call_1", "list_dir", {"path": "."}),  # missing id, dict arguments
    )
    assert resp.usage == Usage(0, 0, unreported=1)  # server reported no usage (P2-15)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", {}),
        (None, {}),
        ("{not json", {"invalid_json_arguments": "{not json"}),
        ("[1, 2]", {"invalid_json_arguments": "[1, 2]"}),
    ],
)
def test_bad_arguments_reach_the_tool_as_a_rejectable_argument(raw: Any, expected: Any) -> None:
    resp = parse_response(json.dumps(reply(None, call("a", "read_file", raw))).encode())
    assert resp.tool_calls[0].arguments == expected


def test_invalid_json_arguments_fail_the_tool_not_the_run(tmp_path: Path) -> None:
    server = FakeServer(
        (200, reply(None, call("a", "read_file", "{oops"))),
        (200, reply("gave up on that call")),
    )
    agent = Sebastian(
        llm=llm(server),
        tools=ToolRegistry(file_tools(Workspace(tmp_path))),
        gate=ApprovalGate(AllowApprover()),
        clock=FakeClock(),
        ids=SeededIdSource(1),
    )
    result = agent.run("read it")
    assert result.stop_reason is StopReason.COMPLETED
    tool_turn = server.requests[1]["json"]["messages"][-1]
    assert tool_turn["role"] == "tool"
    assert "invalid_json_arguments" in tool_turn["content"]


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (b"not json", "unexpected response shape"),
        (json.dumps({"choices": []}).encode(), "unexpected response shape"),
        (json.dumps({"choices": [{"message": "hi"}]}).encode(), "not an object"),
        (json.dumps(reply(None, {"id": "x", "function": {}})).encode(), "no function name"),
        (json.dumps(reply(None, "bogus")).encode(), "not an object"),  # type: ignore[arg-type]
    ],
)
def test_malformed_responses_raise(body: bytes, why: str) -> None:
    with pytest.raises(LLMError, match=why):
        parse_response(body)


def test_odd_usage_and_content_types_are_tolerated() -> None:
    resp = parse_response(
        json.dumps(
            {
                "choices": [{"message": {"content": ["parts"]}}],
                "usage": {"prompt_tokens": -1, "completion_tokens": "9"},
            }
        ).encode()
    )
    assert (resp.text, resp.usage) == ("", Usage(0, 0, unreported=1))
    assert parse_response(json.dumps({"choices": [{"message": {}}], "usage": []}).encode()).usage


# --- errors and retries ---------------------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_statuses_retry(status: int) -> None:
    server = FakeServer((status, b"busy"), (200, reply("ok")))
    client = RetryingLLM(llm(server), config=RetryConfig(sleep=lambda _s: None))
    assert client.complete("S", [Message(Role.USER, text="t")], []).text == "ok"
    assert len(server.requests) == 2


@pytest.mark.parametrize("status", [400, 401, 404])
def test_client_errors_do_not_retry(status: int) -> None:
    server = FakeServer((status, b'{"error": "nope"}'))
    with pytest.raises(LLMError, match=f"HTTP {status}") as info:
        llm(server).complete("S", [Message(Role.USER, text="t")], [])
    assert not isinstance(info.value, TransientLLMError)


def test_urllib_transport_maps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    class Resp:
        status = 200

        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    calls: list[Any] = []

    def ok(req: Any, timeout: float) -> Resp:
        calls.append((req.full_url, req.get_header("Content-type"), timeout))
        return Resp()

    monkeypatch.setattr(oc, "_open", ok)
    assert oc._urllib_transport("https://h/v1", {"Content-Type": "x"}, b"{}", 5) == (200, b"{}")
    assert calls == [("https://h/v1", "x", 5)]

    def down(req: Any, timeout: float) -> Resp:
        raise urllib.error.URLError("refused")

    monkeypatch.setattr(oc, "_open", down)
    with pytest.raises(TransientLLMError, match="cannot reach h"):
        oc._urllib_transport("https://h/v1", {}, b"{}", 5)


def test_http_error_body_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    def http_error(req: Any, timeout: float) -> Any:
        raise urllib.error.HTTPError(req.full_url, 429, "slow", {}, io.BytesIO(b"rate"))  # type: ignore[arg-type]

    monkeypatch.setattr(oc, "_open", http_error)
    assert oc._urllib_transport("https://h/v1", {}, b"{}", 5) == (429, b"rate")


# --- base URL policy ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1/",
        "http://127.0.0.1:8000/v1",
        "http://[::1]:1234/v1",
        "https://openrouter.ai/api/v1",
        "https://api.openai.com/v1",
    ],
)
def test_allowed_base_urls(url: str) -> None:
    assert not check_base_url(url).endswith("/")


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("http://example.com/v1", "only allowed for localhost"),
        ("http://10.0.0.5:8000/v1", "only allowed for localhost"),
        ("ftp://localhost/v1", "must be http"),
        ("localhost:11434", "must be http"),
        ("https://user:key@host/v1", "SEBASTIAN_API_KEY_ENV"),
    ],
)
def test_rejected_base_urls(url: str, why: str) -> None:
    with pytest.raises(LLMError, match=why):
        check_base_url(url)


def test_empty_model_rejected() -> None:
    with pytest.raises(LLMError, match="model name"):
        OpenAICompatLLM(" ", transport=FakeServer())


# --- factory and config ---------------------------------------------------------------------


def test_make_llm_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    assert BACKENDS == ("anthropic", "openai")
    remote = make_llm("gpt-x", "openai")
    assert isinstance(remote, RetryingLLM)
    assert isinstance(remote.inner, OpenAICompatLLM)
    assert remote.inner.base_url == "https://api.openai.com/v1"
    local = make_llm("llama", "openai", "http://localhost:11434/v1")
    assert isinstance(local, RetryingLLM)
    assert local.inner.base_url == "http://localhost:11434/v1"  # type: ignore[attr-defined]
    with pytest.raises(LLMError, match="unknown backend"):
        make_llm("m", "grok")


def test_make_llm_anthropic_uses_the_sdk_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    import sebastian.llm.anthropic as an

    built: list[str] = []

    class Fake:
        def __init__(self, model: str) -> None:
            built.append(model)

    monkeypatch.setattr(an, "AnthropicLLM", Fake)
    assert isinstance(make_llm("claude-sonnet-5-5"), RetryingLLM)
    assert built == ["claude-sonnet-5-5"]


def test_config_backend_env() -> None:
    cfg = Config.from_env(
        {"SEBASTIAN_BACKEND": "openai", "SEBASTIAN_BASE_URL": "http://localhost:11434/v1"}
    )
    assert (cfg.backend, cfg.base_url) == ("openai", "http://localhost:11434/v1")
    assert Config.from_env({}).backend == "anthropic"
    with pytest.raises(ValueError, match="SEBASTIAN_BACKEND"):
        Config.from_env({"SEBASTIAN_BACKEND": "grok"})


def test_cli_uses_the_configured_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io
    import sys

    from sebastian import cli

    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SEBASTIAN_BACKEND", "openai")
    monkeypatch.setenv("SEBASTIAN_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    server = FakeServer((200, reply("hello from a local model")))
    seen: list[tuple[str, str, str, str]] = []

    def fake_make(model: str, backend: str, base_url: str, key_env: str) -> OpenAICompatLLM:
        seen.append((model, backend, base_url, key_env))
        return OpenAICompatLLM(model, base_url, transport=server)

    monkeypatch.setattr(cli, "make_llm", fake_make)
    ws = tmp_path / "ws"
    ws.mkdir()
    args = ["run", "hi", "-w", str(ws), "--no-memory", "--model", "qwen3"]
    assert cli.main(args) == 0
    assert seen == [("qwen3", "openai", "http://localhost:11434/v1", "")]
    assert "hello from a local model" in capsys.readouterr().out
