"""Structured run tracing (JSONL) with secret redaction. One line per event, keyed by run_id."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sebastian.clock import Clock

_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|authorization)", re.IGNORECASE)
REDACTED = "[REDACTED]"

# P1-08: the one redactor for traces, sessions and anything else written to disk.
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_SECRET_VALUE = re.compile(
    "|".join(
        (
            r"sk-[A-Za-z0-9_\-]{16,}",  # OpenAI / Anthropic (sk-ant-...)
            r"gh[pousr]_[A-Za-z0-9]{20,}",  # GitHub tokens
            r"github_pat_[A-Za-z0-9_]{20,}",  # GitHub fine-grained PATs
            r"\bAKIA[0-9A-Z]{16}\b",  # AWS access key ids
            r"xox[abpr]-[A-Za-z0-9\-]{10,}",  # Slack
            r"eyJ[A-Za-z0-9_\-]{5,}\.eyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}",  # JWTs
            r"Bearer\s+\S+",
        )
    )
)
# .env / shell style: SOME_API_KEY=value, export GITHUB_TOKEN=value (the value is replaced).
# No spaces around "=": that is how .env and shell assign, and it skips Python constants.
_ENV_ASSIGN = re.compile(
    r"(?m)^(\s*(?:export\s+)?[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD)[A-Z0-9_]*=)"
    r"[\"']?[^\s\"']{6,}[\"']?"
)
_CANDIDATE = re.compile(r"[A-Za-z0-9+/=_\-]{32,}")
_HEXISH = re.compile(r"[0-9a-fA-F\-]+")  # git SHAs, digests, UUIDs: identifiers, not secrets
ENTROPY_BITS = 4.0  # per character; random base62 of 32+ chars is about 4.7 to 5.2


def _entropy(text: str) -> float:
    n = len(text)
    return -sum(c / n * math.log2(c / n) for c in Counter(text).values())


def _looks_random(tok: str) -> bool:
    return (
        len(tok) >= 32
        and not _HEXISH.fullmatch(tok)
        and any(c.isdigit() for c in tok)
        and any(c.isupper() for c in tok)
        and any(c.islower() for c in tok)
        and _entropy(tok) >= ENTROPY_BITS
    )


def _high_entropy(m: re.Match[str]) -> str:
    tok = m.group(0)
    # A token with "/" may be a URL or path: judge each segment on its own, so
    # github.com/owner/Repo stays readable while a long random segment is still caught.
    if "/" in tok:
        parts = tok.split("/")
        if any(_looks_random(p) for p in parts):
            return "/".join(REDACTED if _looks_random(p) else p for p in parts)
        return tok
    return REDACTED if _looks_random(tok) else tok


def redact_text(text: str) -> str:
    """``text`` with secret-looking substrings replaced by ``[REDACTED]``."""
    text = _PRIVATE_KEY.sub(REDACTED, text)
    text = _SECRET_VALUE.sub(REDACTED, text)
    text = _ENV_ASSIGN.sub(lambda m: m.group(1) + REDACTED, text)
    return _CANDIDATE.sub(_high_entropy, text)


def redact(value: Any) -> Any:
    """Copy of ``value`` with secret-looking keys and values replaced."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class Tracer(Protocol):
    """Receives structured run events."""

    def emit(self, run_id: str, event: str, **data: Any) -> None:
        """Record ``event`` for ``run_id``."""


class NullTracer:
    """Discards everything."""

    def emit(self, run_id: str, event: str, **data: Any) -> None:
        """Do nothing."""


class MemoryTracer:
    """Keeps redacted events in memory, for tests and evals."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, run_id: str, event: str, **data: Any) -> None:
        """Store a redacted event."""
        self.events.append({"run_id": run_id, "event": event, **redact(data)})

    def names(self) -> list[str]:
        """Event names in order."""
        return [e["event"] for e in self.events]


class JsonlTracer:
    """Appends one redacted JSON object per event to a file."""

    def __init__(self, path: str | Path, clock: Clock) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._seq = 0

    def emit(self, run_id: str, event: str, **data: Any) -> None:
        """Append a redacted, timestamped, sequenced event."""
        self._seq += 1
        record = {"t": self._clock.now(), "seq": self._seq, "run_id": run_id, "event": event}
        record.update(redact(data))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


@dataclass(slots=True)
class RunSummary:
    """One run as shown by ``sebastian traces``."""

    run_id: str
    started: float | None = None
    stop: str = "running"
    steps: int = 0
    tokens: int = 0
    cost: float = 0.0  # USD, when the run's models were priced (B40)


def summarize_runs(path: str | Path, limit: int) -> list[RunSummary]:
    """Summarise the last ``limit`` runs in a JSONL trace file, oldest first.

    Missing files give no runs; malformed or foreign lines are skipped.
    """
    runs: dict[str, RunSummary] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict) or not isinstance(rec.get("run_id"), str):
            continue
        run = runs.setdefault(rec["run_id"], RunSummary(rec["run_id"]))
        event = rec.get("event")
        if event == "run_start" and isinstance(rec.get("t"), int | float):
            run.started = float(rec["t"])
        elif event == "run_end":
            run.stop = str(rec.get("stop", "unknown"))
            if isinstance(rec.get("steps"), int):
                run.steps = rec["steps"]
            usage = rec.get("usage")
            if isinstance(usage, dict):
                run.tokens = sum(
                    v for k, v in usage.items() if k in ("input", "output") and type(v) is int
                )
                cost = usage.get("cost")
                if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0:
                    run.cost = float(cost)
    return list(runs.values())[-limit:]
