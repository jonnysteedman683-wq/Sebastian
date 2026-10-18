"""Anthropic Messages API backend.

Requires the ``anthropic`` extra: ``pip install 'sebastian[anthropic]'``.

Verified against anthropic SDK 1.15: ``messages.create`` with ``tools``; ``tool_use`` content
blocks; ``tool_result`` blocks carrying ``tool_use_id``, ``content`` and ``is_error``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sebastian.llm.base import LLMError, TransientLLMError
from sebastian.tools.base import Tool
from sebastian.types import LLMResponse, Message, Role, ToolCall, Usage

_TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class AnthropicLLM:
    def __init__(
        self,
        model: str,
        max_tokens: int = 4096,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        if client is None:
            try:
                import anthropic  # noqa: PLC0415 - optional dependency
            except ImportError as exc:
                raise LLMError(
                    "anthropic package not installed; pip install 'sebastian[anthropic]'"
                ) from exc
            client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self._client: Any = client  # SDK or test double; typed loosely by design
        self.model = model
        self.max_tokens = max_tokens

    @staticmethod
    def _to_wire(messages: Sequence[Message]) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        for m in messages:
            blocks: list[dict[str, Any]] = []
            if m.text:
                blocks.append({"type": "text", "text": m.text})
            if m.role is Role.ASSISTANT:
                blocks += [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                    for c in m.tool_calls
                ]
            else:
                blocks += [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.call_id,
                        "content": r.content,
                        "is_error": r.is_error,
                    }
                    for r in m.tool_results
                ]
            wire.append({"role": m.role.value, "content": blocks})
        return wire

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> LLMResponse:
        import anthropic  # noqa: PLC0415

        try:
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                # Cache the static system prompt across steps (work order U8).
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=self._to_wire(messages),
                tools=[
                    {"name": t.name, "description": t.description, "input_schema": t.schema}
                    for t in tools
                ],
            )
        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as exc:
            raise TransientLLMError(str(exc)) from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code in _TRANSIENT_STATUS:
                raise TransientLLMError(str(exc)) from exc
            raise LLMError(str(exc)) from exc

        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = tuple(
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in resp.content
            if b.type == "tool_use"
        )
        usage = Usage(resp.usage.input_tokens, resp.usage.output_tokens)
        return LLMResponse(text=text, tool_calls=calls, usage=usage)
