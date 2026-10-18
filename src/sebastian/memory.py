"""Persistent episodic memory: what Sebastian tried, and how it went.

JSON on disk, validated on every load, written atomically. Corrupt files raise rather than
being silently discarded, because deleting stored state is an escalation-level decision.
Retrieval is keyword overlap for now; embeddings are planned (work order U5).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_WORD = re.compile(r"[a-z0-9_]{3,}")


class MemoryCorruptError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Episode:
    task: str
    outcome: str
    summary: str
    timestamp: float

    @classmethod
    def from_json(cls, raw: Any) -> Episode:
        if not isinstance(raw, dict):
            raise MemoryCorruptError("episode is not an object")
        try:
            task, outcome, summary, ts = (
                raw["task"],
                raw["outcome"],
                raw["summary"],
                raw["timestamp"],
            )
        except KeyError as exc:
            raise MemoryCorruptError(f"episode missing field {exc}") from exc
        if not all(type(v) is str for v in (task, outcome, summary)):
            raise MemoryCorruptError("episode text fields must be strings")
        if type(ts) not in (int, float):
            raise MemoryCorruptError("episode timestamp must be a number")
        return cls(task, outcome, summary, float(ts))


def _terms(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


class MemoryStore:
    def __init__(self, path: str | Path, max_episodes: int = 500) -> None:
        self.path = Path(path)
        self.max_episodes = max_episodes
        self._episodes: list[Episode] = self._load()

    def _load(self) -> list[Episode]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MemoryCorruptError(f"{self.path}: invalid JSON") from exc
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            raise MemoryCorruptError(f"{self.path}: unsupported memory schema")
        episodes = data.get("episodes")
        if not isinstance(episodes, list):
            raise MemoryCorruptError(f"{self.path}: episodes must be a list")
        return [Episode.from_json(e) for e in episodes]

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": SCHEMA_VERSION, "episodes": [asdict(e) for e in self._episodes]}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".mem-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def record(self, episode: Episode) -> None:
        self._episodes.append(episode)
        self._episodes = self._episodes[-self.max_episodes :]
        self._save()

    def recall(self, query: str, k: int = 3) -> list[Episode]:
        q = _terms(query)
        if not q:
            return []
        scored = [
            (len(q & _terms(e.task + " " + e.summary)), i, e) for i, e in enumerate(self._episodes)
        ]
        scored = [s for s in scored if s[0] > 0]
        scored.sort(key=lambda s: (-s[0], -s[1]))  # best overlap, then most recent
        return [e for _, _, e in scored[:k]]

    def __len__(self) -> int:
        return len(self._episodes)
