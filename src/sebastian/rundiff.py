"""``sebastian diff``: what a run changed, as a unified diff (B56).

Each file the run's checkpoint recorded is diffed from its original bytes (before the run) to
the file as it is now in the run's workspace. A file the run created diffs from ``/dev/null``;
one that no longer exists diffs to ``/dev/null``. Read-only: nothing is written anywhere.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from sebastian.checkpoint import CheckpointError, Checkpoints
from sebastian.tools.workspace import Workspace, WorkspaceError

MIN_PREFIX = 6  # same as ``sebastian show``
NULL = "/dev/null"


@dataclass(slots=True)
class RunDiff:
    """The diff text for one run, plus any files that could not be read safely."""

    run_id: str
    text: str = ""
    files: int = 0  # files with a difference
    skipped: list[str] = field(default_factory=list)  # "path: reason"


def resolve_run(root: str | Path, run_id: str) -> str:
    """The checkpointed run whose id is ``run_id`` or starts with it (6+ characters)."""
    base = Path(root)
    runs = sorted(p.parent.name for p in base.glob("*/manifest.json"))
    if run_id in runs:
        return run_id
    if len(run_id) < MIN_PREFIX:
        raise CheckpointError(
            f"no checkpoint for run {run_id!r}; give the full id or at least {MIN_PREFIX} chars"
        )
    matches = [r for r in runs if r.startswith(run_id)]
    if not matches:
        raise CheckpointError(f"no checkpoint for a run starting with {run_id!r}")
    if len(matches) > 1:
        raise CheckpointError(f"{run_id!r} matches {len(matches)} runs; give more of the id")
    return matches[0]


def _decode(data: bytes | None) -> list[str] | None:
    """Lines for difflib, ``[]`` for a missing file, ``None`` for binary content."""
    if data is None:
        return []
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None


def _current(ws: Workspace, rel: str) -> bytes | None:
    """The file's bytes now; ``None`` if it no longer exists. Symlinks are refused (P1-09)."""
    target = ws.resolve_strict(rel)
    return target.read_bytes() if target.is_file() else None


def _one(rel: str, before: bytes | None, after: bytes | None) -> str:
    old, new = _decode(before), _decode(after)
    a = NULL if before is None else f"a/{rel}"
    b = NULL if after is None else f"b/{rel}"
    if old is None or new is None:
        return f"Binary files {a} and {b} differ\n"
    lines = list(difflib.unified_diff(old, new, fromfile=a, tofile=b))
    if not lines:  # an empty file created or deleted: headers only, no hunks
        return f"--- {a}\n+++ {b}\n"
    out = []
    for line in lines:
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def run_diff(store: Checkpoints, run_id: str) -> RunDiff:
    """Diff every file ``run_id`` changed against its current state in the run's workspace."""
    rid = resolve_run(store.root, run_id)
    originals = store.originals(rid)
    try:
        ws = Workspace(store.workspace_of(rid))
    except (WorkspaceError, OSError) as exc:
        raise CheckpointError(f"run {rid}'s workspace is not available: {exc}") from exc
    result = RunDiff(rid)
    parts = []
    for rel in sorted(originals):
        before = originals[rel]
        try:
            after = _current(ws, rel)
        except (WorkspaceError, OSError) as exc:
            result.skipped.append(f"{rel}: {exc}")
            continue
        if before == after:
            continue
        parts.append(_one(rel, before, after))
        result.files += 1
    result.text = "".join(parts)
    return result
