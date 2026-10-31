"""``sebastian scar list`` and ``sebastian scar prune`` (B60).

Scars are validated with ``evals.scar.load_scars`` before anything is shown or removed. A
scar's date is its file's modification time (scars carry no creation date). ``prune`` treats
scars whose task text matches under memory's normalised key (case and whitespace folded) as
duplicates and keeps the newest of each group.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sebastian.evals.scar import load_scars
from sebastian.memory import _task_key as task_key


@dataclass(frozen=True, slots=True)
class ScarInfo:
    """One scar file as listed."""

    path: Path
    name: str  # the eval case name
    task: str
    source: str  # origin run id, or the origin source (e.g. "redteam"), or "?"
    mtime: float

    @property
    def date(self) -> str:
        """The file's modification time as a UTC ISO timestamp."""
        return datetime.fromtimestamp(self.mtime, UTC).isoformat(timespec="seconds")


def _source(spec: dict[str, Any]) -> str:
    origin = spec.get("origin")
    if isinstance(origin, dict):
        for key in ("run_id", "source"):
            value = origin.get(key)
            if isinstance(value, str) and value:
                return value
    return "?"


def scars(directory: str | Path) -> list[ScarInfo]:
    """Every scar in ``directory``, sorted by file name; ``ScarError`` if any is invalid."""
    load_scars(directory)  # validates every file; a missing directory is empty
    root = Path(directory)
    out = []
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        spec = json.loads(path.read_text(encoding="utf-8"))
        out.append(ScarInfo(path, spec["name"], spec["task"], _source(spec), path.stat().st_mtime))
    return out


def duplicates(items: list[ScarInfo]) -> list[ScarInfo]:
    """Scars to remove: all but the newest (mtime, then file name) of each same-task group."""
    groups: dict[str, list[ScarInfo]] = {}
    for s in items:
        groups.setdefault(task_key(s.task), []).append(s)
    drop: list[ScarInfo] = []
    for group in groups.values():
        newest = max(group, key=lambda s: (s.mtime, s.path.name))
        drop.extend(s for s in group if s is not newest)
    return sorted(drop, key=lambda s: s.path.name)
