"""Persistent episodic memory: what Sebastian tried, and how it went.

JSON on disk, validated on every load, written atomically. Corrupt files raise rather than
being silently discarded, because deleting stored state is an escalation-level decision.
Retrieval is BM25 over task + summary (pure Python, no dependencies); recording the same task
again replaces the old episode. Embeddings remain an option (work order U5).
"""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_WORD = re.compile(r"[a-z0-9_]{3,}")


class MemoryCorruptError(ValueError):
    """The memory file exists but is not valid; it is never silently discarded."""

    pass


@dataclass(frozen=True, slots=True)
class Episode:
    """One remembered run: the task, how it ended, a short summary and when."""

    task: str
    outcome: str
    summary: str
    timestamp: float

    @classmethod
    def from_json(cls, raw: Any) -> Episode:
        """Parse and validate one stored episode."""
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


_STOP = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "onto",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "not",
        "but",
        "you",
        "your",
        "its",
        "our",
        "all",
        "any",
        "can",
        "will",
        "then",
        "than",
        "when",
        "what",
        "which",
        "who",
        "how",
    }
)
BM25_K1 = 1.2
BM25_B = 0.75


def _tokens(text: str) -> list[str]:
    return [t for t in _WORD.findall(text.lower()) if t not in _STOP]


def _terms(text: str) -> set[str]:
    return set(_tokens(text))


def _task_key(task: str) -> str:
    return " ".join(task.lower().split())


class MemoryStore:
    """Episodic memory persisted as JSON, written atomically and capped in size."""

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
        """Store an episode. A previous episode for the same task (case/space-insensitive) is
        replaced, so the newest outcome wins and recall is not flooded by repeats."""
        key = _task_key(episode.task)
        self._episodes = [e for e in self._episodes if _task_key(e.task) != key]
        self._episodes.append(episode)
        self._episodes = self._episodes[-self.max_episodes :]
        self._save()

    def recall(self, query: str, k: int = 3) -> list[Episode]:
        """Top-``k`` episodes by BM25 relevance to ``query``; ties go to the most recent."""
        q = _terms(query)
        if not q or not self._episodes:
            return []
        docs = [_tokens(e.task + " " + e.summary) for e in self._episodes]
        n = len(docs)
        avg = sum(len(d) for d in docs) / n or 1.0
        df = {t: sum(1 for d in docs if t in d) for t in q}
        scored: list[tuple[float, int, Episode]] = []
        for i, (doc, ep) in enumerate(zip(docs, self._episodes, strict=True)):
            score = 0.0
            for t in q:
                tf = doc.count(t)
                if not tf:
                    continue
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                norm = tf + BM25_K1 * (1 - BM25_B + BM25_B * len(doc) / avg)
                score += idf * tf * (BM25_K1 + 1) / norm
            if score > 0:
                scored.append((score, i, ep))
        scored.sort(key=lambda s: (-s[0], -s[1]))
        return [e for _, _, e in scored[:k]]

    def episodes(self) -> list[Episode]:
        """All stored episodes, newest first (recording a task again makes it the newest)."""
        return self._episodes[::-1]

    def forget(self, task: str) -> bool:
        """Delete the episode for ``task`` (same case/space-insensitive key as ``record``).

        Returns whether one was deleted; the file is only rewritten on a hit."""
        key = _task_key(task)
        kept = [e for e in self._episodes if _task_key(e.task) != key]
        if len(kept) == len(self._episodes):
            return False
        self._episodes = kept
        self._save()
        return True

    def __len__(self) -> int:
        return len(self._episodes)
