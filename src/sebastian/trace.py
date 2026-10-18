"""Structured run tracing (JSONL) with secret redaction. One line per event, keyed by run_id."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sebastian.clock import Clock

_SECRET_KEY = re.compile(r"(api[_-]?key|token|secret|password|authorization)", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"(sk-[A-Za-z0-9_\-]{16,}|ghp_[A-Za-z0-9]{20,}|Bearer\s+\S+)")
REDACTED = "[REDACTED]"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub(REDACTED, value)
    return value


class Tracer(Protocol):
    def emit(self, run_id: str, event: str, **data: Any) -> None: ...


class NullTracer:
    def emit(self, run_id: str, event: str, **data: Any) -> None:
        return None


class MemoryTracer:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, run_id: str, event: str, **data: Any) -> None:
        self.events.append({"run_id": run_id, "event": event, **redact(data)})

    def names(self) -> list[str]:
        return [e["event"] for e in self.events]


class JsonlTracer:
    def __init__(self, path: str | Path, clock: Clock) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._seq = 0

    def emit(self, run_id: str, event: str, **data: Any) -> None:
        self._seq += 1
        record = {"t": self._clock.now(), "seq": self._seq, "run_id": run_id, "event": event}
        record.update(redact(data))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


@dataclass(slots=True)
class RunSummary:
    run_id: str
    started: float | None = None
    stop: str = "running"
    steps: int = 0
    tokens: int = 0


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
                run.tokens = sum(v for v in usage.values() if isinstance(v, int))
    return list(runs.values())[-limit:]
