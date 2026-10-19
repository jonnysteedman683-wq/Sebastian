"""Model backends: the provider-agnostic interface, retries and the scripted test LLM."""

from sebastian.llm.base import LLM, LLMError, RetryingLLM, TransientLLMError
from sebastian.llm.scripted import ScriptedLLM

BACKENDS = ("anthropic", "openai")


def make_llm(model: str, backend: str = "anthropic", base_url: str = "") -> LLM:
    """A real model client with retries. ``openai`` is any OpenAI-compatible endpoint (B37)."""
    if backend == "anthropic":
        from sebastian.llm.anthropic import AnthropicLLM  # noqa: PLC0415 - optional SDK

        return RetryingLLM(AnthropicLLM(model))
    if backend == "openai":
        from sebastian.llm.openai_compat import OpenAICompatLLM  # noqa: PLC0415

        if base_url:
            return RetryingLLM(OpenAICompatLLM(model, base_url))
        return RetryingLLM(OpenAICompatLLM(model))
    raise LLMError(f"unknown backend {backend!r}; use one of {', '.join(BACKENDS)}")


__all__ = [
    "BACKENDS",
    "LLM",
    "LLMError",
    "RetryingLLM",
    "ScriptedLLM",
    "TransientLLMError",
    "make_llm",
]
