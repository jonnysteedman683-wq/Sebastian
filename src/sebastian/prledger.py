"""PR record for the trust ladder (B26b).

``sebastian evolve --pr`` writes one ``opened`` line to ``$SEBASTIAN_HOME/prs.jsonl`` for each
PR it opens. ``sebastian trust --sync`` later asks GitHub what became of each one and checks the
repo's history for reverts, then appends what it learned:

- merged: an ``evolve_pr`` round that passed (1/1) goes to ``history.jsonl``, at merge time.
- reverted (a merged PR whose merge commit a later commit says it reverts): an ``evolve_pr``
  round that failed (0/1), at revert time, so ``trust.assess`` drops a level.
- closed without merging: recorded in the ledger only; it is not a round.

Each outcome is recorded once per PR, so syncing again is harmless.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from sebastian import history

LEDGER_FILE = "prs.jsonl"
PR_KIND = "evolve_pr"
_OUTCOMES = frozenset({"merged", "reverted", "closed"})
_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{40})")
_SHA = re.compile(r"^[0-9a-f]{40}$")
# Same hardening as evolve's host git (P1-05): no hooks, no fsmonitor, no system config.
_GIT_HARDENING = ("-c", "core.fsmonitor=", "-c", "core.hooksPath=/dev/null")


class LedgerError(RuntimeError):
    """GitHub or git could not be asked about a PR."""


@dataclass(frozen=True, slots=True)
class PrState:
    """What GitHub says about one PR."""

    state: str  # OPEN, MERGED or CLOSED
    merged_at: float | None = None
    merge_sha: str | None = None


@dataclass(slots=True)
class PrEntry:
    """One PR evolve opened, and the outcomes recorded for it so far."""

    url: str
    branch: str
    item: str
    model: str
    opened: float
    outcomes: set[str] = field(default_factory=set)


def _append(path: Path, rec: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(rec), sort_keys=True) + "\n")


def record_opened(home: Path, url: str, branch: str, item: str, model: str, t: float) -> None:
    """Note that evolve opened the PR at ``url``."""
    _append(
        home / LEDGER_FILE,
        {"t": t, "event": "opened", "url": url, "branch": branch, "item": item, "model": model},
    )


def load(home: Path) -> dict[str, PrEntry]:
    """Every PR in the ledger by URL, with its recorded outcomes. Bad lines are skipped."""
    prs: dict[str, PrEntry] = {}
    try:
        with (home / LEDGER_FILE).open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict) or not isinstance(rec.get("url"), str):
                    continue
                event, url = rec.get("event"), rec["url"]
                if event == "opened" and isinstance(rec.get("t"), int | float):
                    branch, item, model = rec.get("branch"), rec.get("item"), rec.get("model")
                    if isinstance(branch, str) and isinstance(item, str) and isinstance(model, str):
                        prs.setdefault(url, PrEntry(url, branch, item, model, float(rec["t"])))
                elif event in _OUTCOMES and url in prs:
                    prs[url].outcomes.add(str(event))
    except FileNotFoundError:
        pass
    return prs


def sync(
    home: Path,
    fetch: Callable[[str], PrState],
    reverts: Mapping[str, float],
    now: float,
) -> list[str]:
    """Record new outcomes for every ledger PR; return one line per outcome recorded.

    ``reverts`` maps a reverted commit's SHA to the time of the commit that reverted it.
    """
    ledger, hist = home / LEDGER_FILE, home / history.HISTORY_FILE
    lines: list[str] = []
    for pr in load(home).values():
        if pr.outcomes & {"reverted", "closed"}:
            continue  # final
        state = fetch(pr.url)
        if state.state == "CLOSED" and "merged" not in pr.outcomes:
            _append(ledger, {"t": now, "event": "closed", "url": pr.url})
            lines.append(f"closed   {pr.url} (not a round)")
            continue
        if state.state != "MERGED":
            continue
        if "merged" not in pr.outcomes:
            t = state.merged_at if state.merged_at is not None else now
            _append(ledger, {"t": t, "event": "merged", "url": pr.url})
            history.append(hist, history.Record(t, PR_KIND, pr.model, 1, 1, run_id=pr.url))
            lines.append(f"merged   {pr.url}")
        if state.merge_sha and state.merge_sha in reverts:
            t = reverts[state.merge_sha]
            _append(ledger, {"t": t, "event": "reverted", "url": pr.url})
            history.append(hist, history.Record(t, PR_KIND, pr.model, 0, 1, run_id=pr.url))
            lines.append(f"reverted {pr.url} (counts as a failed round)")
    return lines


# --- the real GitHub and git lookups -----------------------------------------------------


def _iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def gh_fetch(url: str) -> PrState:
    """Ask the GitHub CLI for the PR's state, merge time and merge commit."""
    exe = shutil.which("gh")
    if exe is None:
        raise LedgerError("gh is not installed")
    proc = subprocess.run(
        [exe, "pr", "view", url, "--json", "state,mergedAt,mergeCommit"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise LedgerError(f"gh pr view {url} failed: {proc.stderr.strip()[:300]}")
    try:
        raw = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise LedgerError(f"gh pr view {url}: unreadable output") from exc
    return parse_state(raw)


def parse_state(raw: Any) -> PrState:
    """A ``PrState`` from ``gh pr view --json state,mergedAt,mergeCommit`` output."""
    if not isinstance(raw, dict) or raw.get("state") not in {"OPEN", "MERGED", "CLOSED"}:
        raise LedgerError("gh returned no PR state")
    commit = raw.get("mergeCommit")
    sha = commit.get("oid") if isinstance(commit, dict) else None
    return PrState(
        raw["state"],
        _iso(raw.get("mergedAt")),
        sha if isinstance(sha, str) and _SHA.match(sha) else None,
    )


def git_reverts(repo: Path, ref: str = "HEAD") -> dict[str, float]:
    """``reverted SHA -> commit time`` for every revert commit reachable from ``ref``."""
    exe = shutil.which("git")
    if exe is None:
        raise LedgerError("git is not installed")
    proc = subprocess.run(
        [exe, *_GIT_HARDENING, "-C", str(repo), "log", "--format=%ct%x1f%B%x1e", ref, "--"],
        capture_output=True,
        text=True,
        check=False,
        env=dict(os.environ, GIT_CONFIG_NOSYSTEM="1"),
    )
    if proc.returncode != 0:
        raise LedgerError(f"git log {ref} failed: {proc.stderr.strip()[:300]}")
    return parse_reverts(proc.stdout)


def parse_reverts(log: str) -> dict[str, float]:
    """Reverted SHAs from ``git log --format=%ct%x1f%B%x1e`` output (earliest revert wins)."""
    out: dict[str, float] = {}
    for entry in log.split("\x1e"):
        stamp, _, body = entry.strip().partition("\x1f")
        if not stamp.isdigit():
            continue
        for sha in _REVERTS.findall(body):
            out[sha] = min(out.get(sha, float("inf")), float(stamp))
    return out
