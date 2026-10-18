"""Anthropic adapter tests with a fake client: no network, no API key needed."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sebastian.llm import LLMError, TransientLLMError
from sebastian.tools import Workspace, file_tools
from sebastian.types import Message, Role, ToolCall, ToolResult

anthropic = pytest.importorskip("anthropic")
try:  # anthropic>=1.15 ships httpx2; older SDKs use httpx
    import httpx2 as httpx
except ImportError:  # pragma: no cover
    httpx = pytest.importorskip("httpx")

from sebastian.llm.anthropic import AnthropicLLM  # noqa: E402


class FakeMessages:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def fake_client(outcome: Any) -> Any:
    return SimpleNamespace(messages=FakeMessages(outcome))


def test_anthropic_maps_request_and_response(tmp_path: Path) -> None:
    resp = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="Reading. "),
            SimpleNamespace(type="tool_use", id="tu1", name="read_file", input={"path": "a"}),
        ],
        usage=SimpleNamespace(input_tokens=12, output_tokens=3),
    )
    client = fake_client(resp)
    llm = AnthropicLLM("claude-sonnet-5-5", client=client)
    history = [
        Message(Role.USER, "task"),
        Message(Role.ASSISTANT, "x", (ToolCall("t0", "list_dir", {}),)),
        Message(Role.USER, tool_results=(ToolResult("t0", "a.txt", is_error=False),)),
    ]
    out = llm.complete("sys", history, file_tools(Workspace(tmp_path)))
    assert out.text == "Reading. "
    assert out.tool_calls == (ToolCall("tu1", "read_file", {"path": "a"}),)
    assert out.usage.total == 15

    kw = client.messages.kwargs
    assert kw["model"] == "claude-sonnet-5-5"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["messages"][1]["content"][1] == {
        "type": "tool_use",
        "id": "t0",
        "name": "list_dir",
        "input": {},
    }
    assert kw["messages"][2]["content"][0]["type"] == "tool_result"
    assert kw["messages"][2]["content"][0]["tool_use_id"] == "t0"
    assert {t["name"] for t in kw["tools"]} == {
        "read_file",
        "write_file",
        "list_dir",
        "search_files",
    }


def _status_error(code: int) -> Exception:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    res = httpx.Response(code, request=req)
    err: Exception = anthropic.APIStatusError("boom", response=res, body=None)
    return err


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (lambda: _status_error(529), TransientLLMError),
        (lambda: _status_error(429), TransientLLMError),
        (lambda: _status_error(401), LLMError),
        (
            lambda: anthropic.APIConnectionError(
                request=httpx.Request("POST", "https://api.anthropic.com")
            ),
            TransientLLMError,
        ),
    ],
)
def test_anthropic_error_mapping(exc: Any, expected: type[Exception]) -> None:
    llm = AnthropicLLM("m", client=fake_client(exc()))
    with pytest.raises(expected) as info:
        llm.complete("s", [Message(Role.USER, "x")], [])
    if expected is LLMError:
        assert not isinstance(info.value, TransientLLMError)


def test_anthropic_builds_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert AnthropicLLM("m")._client is not None
    assert AnthropicLLM("m", api_key="k")._client is not None


def test_anthropic_missing_package(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real = builtins.__import__

    def fake_import(name: str, *a: Any, **k: Any) -> Any:
        if name == "anthropic":
            raise ImportError("no")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(LLMError, match="not installed"):
        AnthropicLLM("m")
