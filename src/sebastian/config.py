"""Configuration from environment variables. Secrets are never stored on this object."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Verified current Anthropic model IDs (Oct 2026): claude-opus-5-5, claude-sonnet-5-5,
# claude-haiku-5-5. Re-verify on every maintenance pass (work order §6.3).
DEFAULT_MODEL = "claude-sonnet-5-5"


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{key} must be >= 1")
    return value


@dataclass(frozen=True, slots=True)
class Config:
    """Runtime settings read from ``SEBASTIAN_*`` environment variables. Holds no secrets."""

    model: str = DEFAULT_MODEL
    max_steps: int = 25
    max_tokens: int = 200_000
    max_seconds: int = 600
    home: Path = Path.home() / ".sebastian"
    plan_model: str = ""
    escalation_model: str = ""
    critic_model: str = ""
    backend: str = "anthropic"  # B37: "anthropic" or "openai" (any OpenAI-compatible server)
    base_url: str = ""  # OpenAI-compatible endpoint, e.g. http://localhost:11434/v1 for Ollama
    api_key_env: str = ""  # P1-19: variable holding the key for a non-OpenAI base URL
    max_cost: float = 0.0  # B40: USD per run; 0 = no dollar limit (needs prices.json)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        """Build a config from ``env`` (default ``os.environ``), validating numbers."""
        env = os.environ if env is None else env
        return cls(
            model=env.get("SEBASTIAN_MODEL") or DEFAULT_MODEL,
            max_steps=_int(env, "SEBASTIAN_MAX_STEPS", 25),
            max_tokens=_int(env, "SEBASTIAN_MAX_TOKENS", 200_000),
            max_seconds=_int(env, "SEBASTIAN_MAX_SECONDS", 600),
            home=Path(env.get("SEBASTIAN_HOME") or Path.home() / ".sebastian"),
            plan_model=env.get("SEBASTIAN_PLAN_MODEL", ""),
            escalation_model=env.get("SEBASTIAN_ESCALATION_MODEL", ""),
            critic_model=env.get("SEBASTIAN_CRITIC_MODEL", ""),
            backend=_backend(env.get("SEBASTIAN_BACKEND") or "anthropic"),
            base_url=env.get("SEBASTIAN_BASE_URL", ""),
            api_key_env=env.get("SEBASTIAN_API_KEY_ENV", "").strip(),
            max_cost=_cost(env.get("SEBASTIAN_MAX_COST", "")),
        )


def _cost(raw: str) -> float:
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"SEBASTIAN_MAX_COST must be a number, got {raw!r}") from exc
    if not 0 <= value < float("inf"):
        raise ValueError("SEBASTIAN_MAX_COST must be zero or more")
    return value


def _backend(value: str) -> str:
    if value not in {"anthropic", "openai"}:
        raise ValueError(f"SEBASTIAN_BACKEND must be anthropic or openai, got {value!r}")
    return value


def ensure_home(path: Path) -> Path:
    """Create ``path`` (and parents) private to the user: 0700, tightened if looser (P1-08)."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        path.chmod(0o700)
    return path
