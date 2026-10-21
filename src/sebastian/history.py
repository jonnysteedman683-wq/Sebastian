"""Score history: one JSON line per live eval or red-team round, for ``sebastian dashboard``.

Live eval runs (``python -m sebastian.evals --live``) and ``sebastian redteam`` append a record
to ``$SEBASTIAN_HOME/history.jsonl``. Offline runs replay scripts, so their scores say nothing
about the model and are not recorded.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

HISTORY_FILE = "history.jsonl"
# "leaderboard" / "leaderboard-gauntlet" come from `sebastian models` (P2-14), kept apart from
# "eval" so a multi-model run never overwrites the configured model's live eval trend.
KINDS = frozenset(
    {"eval", "gauntlet", "redteam", "redteam_critic", "leaderboard", "leaderboard-gauntlet"}
)


@dataclass(frozen=True, slots=True)
class Record:
    """One scored round."""

    t: float
    kind: str  # one of KINDS; "gauntlet" = eval under the Gauntlet, "redteam_critic" = B35
    model: str
    passed: int
    total: int
    resisted: int = 0  # injection cases (eval) or attacks (redteam) the model resisted outright
    hostile: int = 0  # injection cases or attacks in the round
    run_id: str = ""  # shared by every row of one leaderboard run (P2-14)

    @property
    def score(self) -> float:
        """Fraction passed (0.0 when the round had no cases)."""
        return self.passed / self.total if self.total else 0.0

    @property
    def resistance(self) -> float | None:
        """Fraction of hostile cases resisted outright, or None if there were none."""
        return self.resisted / self.hostile if self.hostile else None


def append(path: str | Path, record: Record) -> None:
    """Append one record (creating the file and its directory if needed)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(record), sort_keys=True) + "\n")


def load(path: str | Path) -> list[Record]:
    """All valid records, oldest first. Missing file -> []; malformed lines are skipped."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        rec = _parse(line)
        if rec is not None:
            out.append(rec)
    return sorted(out, key=lambda r: r.t)


def _parse(line: str) -> Record | None:
    try:
        raw: Any = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    ints = ("passed", "total", "resisted", "hostile")
    if (
        not isinstance(raw.get("t"), int | float)
        or raw.get("kind") not in KINDS
        or not isinstance(raw.get("model"), str)
        or not all(type(raw.get(k, 0)) is int and raw.get(k, 0) >= 0 for k in ints)
    ):
        return None
    return Record(
        float(raw["t"]),
        raw["kind"],
        raw["model"],
        raw["passed"],
        raw["total"],
        raw.get("resisted", 0),
        raw.get("hostile", 0),
        raw["run_id"] if isinstance(raw.get("run_id"), str) else "",
    )
