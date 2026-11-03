"""Model backends: the provider-agnostic interface, retries and the scripted test LLM."""

from sebastian.llm.base import LLM, LLMError, RetryingLLM, TransientLLMError
from sebastian.llm.scripted import ScriptedLLM

BACKENDS = ("anthropic", "openai")


def make_llm(
    model: str,
    backend: str = "anthropic",
    base_url: str = "",
    key_env: str = "",
    max_output_tokens: int | None = None,
) -> LLM:
    """A real model client with retries. ``openai`` is any OpenAI-compatible endpoint (B37).

    ``key_env`` names the variable holding the endpoint's API key (P1-19); without it only
    ``api.openai.com`` gets ``OPENAI_API_KEY`` and other hosts get no key.
    ``max_output_tokens`` caps each OpenAI-compatible reply (B53); None reads
    ``SEBASTIAN_MAX_OUTPUT_TOKENS`` (default 4096).
    """
    if backend == "anthropic":
        from sebastian.llm.anthropic import AnthropicLLM  # noqa: PLC0415 - optional SDK

        return RetryingLLM(AnthropicLLM(model))
    if backend == "openai":
        from sebastian.config import Config  # noqa: PLC0415 - avoid an import cycle
        from sebastian.llm.openai_compat import OpenAICompatLLM  # noqa: PLC0415

        cap = max_output_tokens or Config.from_env().max_output_tokens
        if base_url:
            return RetryingLLM(OpenAICompatLLM(model, base_url, key_env=key_env, max_tokens=cap))
        return RetryingLLM(OpenAICompatLLM(model, key_env=key_env, max_tokens=cap))
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
