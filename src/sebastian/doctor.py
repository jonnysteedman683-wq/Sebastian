"""``sebastian doctor`` (B47): check the setup before a run spends tokens on it.

Every check is local and read-only: no network, no files created, and key values are never
printed (only whether the variable is set). Each check yields a pass, warn or fail line;
any fail makes the command exit 1.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from sebastian import history
from sebastian.config import Config
from sebastian.llm.base import LLMError
from sebastian.llm.openai_compat import (
    API_KEY_ENV,
    OPENAI_HOST,
    check_base_url,
    check_key_env,
)
from sebastian.memory import MemoryCorruptError, MemoryStore
from sebastian.pricing import PricingError, load_prices, require_prices
from sebastian.tools.builtin import bwrap_usable

PASS, WARN, FAIL = "pass", "warn", "fail"
MIN_PYTHON = (3, 11)
ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"


@dataclass(frozen=True, slots=True)
class Check:
    """One doctor line: which check, how it went and a short human-readable detail."""

    name: str
    status: str
    detail: str

    def line(self) -> str:
        """The printed form, e.g. ``[pass] python: 3.12.3``."""
        return f"[{self.status}] {self.name}: {self.detail}"


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_python(version: tuple[int, int, int] | None = None) -> Check:
    """The interpreter meets ``requires-python``."""
    v = version or (sys.version_info[0], sys.version_info[1], sys.version_info[2])
    text = ".".join(map(str, v))
    if v[:2] >= MIN_PYTHON:
        return Check("python", PASS, text)
    need = ".".join(map(str, MIN_PYTHON))
    return Check("python", FAIL, f"{text}; Sebastian needs {need} or newer")


def check_home(home: Path) -> Check:
    """``SEBASTIAN_HOME`` is writable (or can be created) and private."""
    if home.exists():
        if not home.is_dir():
            return Check("home", FAIL, f"{home} exists but is not a directory")
        try:
            with tempfile.TemporaryFile(dir=home):
                pass
        except OSError as exc:
            return Check("home", FAIL, f"{home} is not writable ({exc.strerror or exc})")
        if stat.S_IMODE(home.stat().st_mode) & 0o077:
            return Check("home", WARN, f"{home} is readable by others; the next run sets it 0700")
        return Check("home", PASS, str(home))
    parent = home.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if os.access(parent, os.W_OK | os.X_OK):
        return Check("home", WARN, f"{home} does not exist yet; the first run creates it")
    return Check("home", FAIL, f"{home} does not exist and {parent} is not writable")


def check_key(cfg: Config, env: Mapping[str, str]) -> Check:
    """The configured backend's key variable is set. Its value is never read into output."""
    if cfg.backend == "anthropic":
        return _key_set(ANTHROPIC_KEY_ENV, env, "anthropic")
    try:
        base_url = check_base_url(cfg.base_url) if cfg.base_url else f"https://{OPENAI_HOST}/v1"
    except LLMError as exc:
        return Check("api key", FAIL, f"SEBASTIAN_BASE_URL: {exc}")
    host = (urlsplit(base_url).hostname or "").lower()
    if cfg.api_key_env:
        return _named_key(cfg.api_key_env, env, host)
    if host == OPENAI_HOST:
        return _key_set(API_KEY_ENV, env, f"sent to {host}")
    if _is_loopback(host):
        return Check("api key", PASS, f"no key needed for local server {host}")
    return Check(
        "api key",
        WARN,
        f"no key will be sent to {host}; if it needs one, set SEBASTIAN_API_KEY_ENV to the "
        "variable that holds it",
    )


def _named_key(raw: str, env: Mapping[str, str], host: str) -> Check:
    try:
        name = check_key_env(raw)
    except LLMError as exc:
        return Check("api key", FAIL, str(exc))
    return _key_set(name, env, f"sent to {host}; SEBASTIAN_API_KEY_ENV names it")


def _key_set(name: str, env: Mapping[str, str], where: str) -> Check:
    if env.get(name):
        return Check("api key", PASS, f"{name} is set ({where})")
    return Check("api key", FAIL, f"{name} is not set ({where})")


def check_prices(cfg: Config) -> Check:
    """``prices.json`` parses, and covers the model when a dollar limit is set."""
    try:
        prices = load_prices(cfg.home)
    except PricingError as exc:
        return Check("prices", FAIL, str(exc))
    if cfg.max_cost:
        try:
            require_prices([cfg.model], prices)
        except PricingError as exc:
            return Check("prices", FAIL, str(exc))
    if not prices:
        return Check("prices", PASS, "no prices.json (costs show as unknown)")
    return Check("prices", PASS, f"{len(prices)} model(s) priced")


def check_sandbox(
    which: Callable[[str], str | None] = shutil.which,
    usable: Callable[[str], bool] = bwrap_usable,
) -> Check:
    """Bubblewrap works here, so ``--sandbox auto`` can run commands and live evals."""
    exe = which("bwrap")
    if exe is None:
        return Check(
            "sandbox", WARN, "bubblewrap not installed: --allow-cmd and live evals won't run"
        )
    if not usable(exe):
        return Check(
            "sandbox", WARN, f"{exe} is blocked here (no user namespaces?): commands won't run"
        )
    return Check("sandbox", PASS, f"bubblewrap usable ({exe})")


def check_memory(home: Path) -> Check:
    """The memory file loads (a corrupt one is never silently discarded)."""
    try:
        store = MemoryStore(home / "memory.json")
    except MemoryCorruptError as exc:
        return Check("memory", FAIL, str(exc))
    except OSError as exc:
        return Check("memory", FAIL, f"cannot read memory: {exc.strerror or exc}")
    return Check("memory", PASS, f"{len(store)} episode(s)")


def check_history(home: Path) -> Check:
    """Every line of ``history.jsonl`` is a valid record; malformed ones are skipped by readers."""
    path = home / history.HISTORY_FILE
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except FileNotFoundError:
        return Check("history", PASS, "no history yet")
    except (OSError, UnicodeDecodeError) as exc:
        return Check("history", FAIL, f"cannot read {path}: {exc}")
    good = len(history.load(path))
    bad = len(lines) - good
    if bad:
        return Check("history", WARN, f"{good} record(s), {bad} malformed line(s) skipped")
    return Check("history", PASS, f"{good} record(s)")


def run_checks(
    env: Mapping[str, str] | None = None,
    *,
    which: Callable[[str], str | None] = shutil.which,
    usable: Callable[[str], bool] = bwrap_usable,
) -> Iterator[Check]:
    """All checks in order. A config that doesn't parse fails and skips the checks needing it."""
    env = os.environ if env is None else env
    yield check_python()
    try:
        cfg = Config.from_env(env)
    except ValueError as exc:
        yield Check("config", FAIL, str(exc))
        yield check_sandbox(which, usable)
        return
    yield Check("config", PASS, f"backend={cfg.backend} model={cfg.model}")
    yield check_home(cfg.home)
    yield check_key(cfg, env)
    yield check_prices(cfg)
    yield check_sandbox(which, usable)
    yield check_memory(cfg.home)
    yield check_history(cfg.home)


def main(env: Mapping[str, str] | None = None) -> int:
    """Print every check; exit 1 if any failed."""
    failed = False
    for c in run_checks(env):
        print(c.line())
        failed |= c.status == FAIL
    return 1 if failed else 0


__all__ = ["Check", "main", "run_checks"]
