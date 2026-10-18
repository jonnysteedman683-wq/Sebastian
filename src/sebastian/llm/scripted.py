"""Deterministic scripted LLM for tests and offline demos. No network, no randomness."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from sebastian.llm.base import LLMError
from sebastian.tools.base import Tool
from sebastian.types import LLMResponse, Message

Step = LLMResponse | Callable[[Sequence[Message]], LLMResponse]


class ScriptedLLM:
    """Replays a fixed list of responses. Callables receive the history so far."""

    def __init__(self, script: Sequence[Step]) -> None:
        self._script = list(script)
        self.calls: list[tuple[str, tuple[Message, ...], tuple[str, ...]]] = []

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> LLMResponse:
        """Return the next scripted response; record the call for assertions."""
        self.calls.append((system, tuple(messages), tuple(t.name for t in tools)))
        if not self._script:
            raise LLMError("scripted LLM exhausted")
        step = self._script.pop(0)
        return step(messages) if callable(step) else step
