"""Model backends: the provider-agnostic interface, retries and the scripted test LLM."""

from sebastian.llm.base import LLM, LLMError, RetryingLLM, TransientLLMError
from sebastian.llm.scripted import ScriptedLLM

BACKENDS = ("anthropic", "openai")


def make_llm(model: str, backend: str = "anthropic", base_url: str = "", key_env: str = "") -> LLM:
    """A real model client with retries. ``openai`` is any OpenAI-compatible endpoint (B37).

    ``key_env`` names the variable holding the endpoint's API key (P1-19); without it only
    ``api.openai.com`` gets ``OPENAI_API_KEY`` and other hosts get no key.
    """
    if backend == "anthropic":
        from sebastian.llm.anthropic import AnthropicLLM  # noqa: PLC0415 - optional SDK

        return RetryingLLM(AnthropicLLM(model))
    if backend == "openai":
        from sebastian.llm.openai_compat import OpenAICompatConfig, OpenAICompatLLM  # noqa: PLC0415

        if base_url:
            config = OpenAICompatConfig(base_url=base_url, key_env=key_env)
        else:
            config = OpenAICompatConfig(key_env=key_env)
        return RetryingLLM(OpenAICompatLLM(model, config=config))
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
