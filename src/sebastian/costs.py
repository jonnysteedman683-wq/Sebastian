"""``sebastian cost``: tokens and spend per model per day, from ``traces.jsonl`` (B58).

Run totals come from the same reader ``sebastian traces`` uses (``trace.summarize_runs``); the
model is the one ``run`` / ``resume`` record on ``run_start``. A run's whole usage is counted
under that model, including any planning, escalation or critic calls it made. Runs that never
finished (no ``run_end``) are left out. Read-only.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sebastian.trace import RunSummary, summarize_runs

UNKNOWN_MODEL = "?"  # runs traced before B58, or by commands that don't record a model


@dataclass(slots=True)
class CostRow:
    """One day and model."""

    day: str  # YYYY-MM-DD, UTC
    model: str
    runs: int = 0
    tokens: int = 0
    cost: float = 0.0
    priced: int = 0  # runs that reported a cost; the rest are unpriced

    @property
    def cost_text(self) -> str:
        """``?`` when no run was priced; ``$x+?`` when only some were (a lower bound)."""
        if not self.priced:
            return "?"
        text = f"${self.cost:.4f}"
        return text if self.priced == self.runs else text + "+?"


def run_models(path: str | Path) -> dict[str, str]:
    """``run_id -> model`` from ``run_start`` events; malformed lines are skipped."""
    out: dict[str, str] = {}
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(rec, dict)
                    and rec.get("event") == "run_start"
                    and isinstance(rec.get("run_id"), str)
                    and isinstance(rec.get("model"), str)
                    and rec["model"]
                ):
                    out[rec["run_id"]] = rec["model"]
    except FileNotFoundError:
        pass
    return out


def tally(
    runs: Iterable[RunSummary],
    models: dict[str, str],
    now: float,
    days: int,
    model: str | None = None,
) -> list[CostRow]:
    """Group finished runs from the last ``days`` UTC days by day and model, newest day first."""
    first = (datetime.fromtimestamp(now, UTC) - timedelta(days=days - 1)).date().isoformat()
    rows: dict[tuple[str, str], CostRow] = {}
    for r in runs:
        if r.started is None or r.stop == "running":
            continue
        day = datetime.fromtimestamp(r.started, UTC).date().isoformat()
        name = models.get(r.run_id, UNKNOWN_MODEL)
        if day < first or (model is not None and name != model):
            continue
        row = rows.setdefault((day, name), CostRow(day, name))
        row.runs += 1
        row.tokens += r.tokens
        if r.cost > 0:
            row.cost += r.cost
            row.priced += 1
    return sorted(rows.values(), key=lambda x: (x.day, x.model), reverse=True)


def render(rows: list[CostRow]) -> str:
    """A text table with a total row."""
    if not rows:
        return "(no finished runs in that window)"
    total = CostRow("total", "")
    for r in rows:
        total.runs += r.runs
        total.tokens += r.tokens
        total.cost += r.cost
        total.priced += r.priced
    width = max(5, *(len(r.model) for r in rows))
    lines = [f"{'day':<10}  {'model':<{width}}  {'runs':>5}  {'tokens':>12}  cost"]
    for r in [*rows, total]:
        lines.append(
            f"{r.day:<10}  {r.model:<{width}}  {r.runs:>5}  {r.tokens:>12,}  {r.cost_text}"
        )
    return "\n".join(lines)


def report(path: str | Path, now: float, days: int, model: str | None = None) -> str:
    """The ``sebastian cost`` table for the trace file at ``path``."""
    runs = summarize_runs(path, sys.maxsize)
    return render(tally(runs, run_models(path), now, days, model))
