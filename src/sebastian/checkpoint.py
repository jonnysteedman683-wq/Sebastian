"""Per-run file checkpoints, so a run's writes can be undone.

Before a file tool changes a file for the first time in a run, its original bytes (or the fact
that it did not exist) are saved under ``<home>/checkpoints/<run_id>/``. After every write the
new content hash is recorded. ``undo`` restores each file to its original state, but refuses
any file changed since the run's last write unless forced, so it never clobbers later work.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sebastian.tools.workspace import Workspace, WorkspaceError

SCHEMA_VERSION = 1
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class CheckpointError(ValueError):
    """A checkpoint is missing, invalid, or cannot be applied safely."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".ckpt-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@dataclass(slots=True)
class _Entry:
    before: str | None  # blob hash of the original bytes; None = file did not exist
    after: str | None = None  # hash after the run's last write


@dataclass(slots=True)
class UndoReport:
    """What ``undo`` did, file by file (paths relative to the workspace)."""

    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # changed since the run; not touched


class Checkpoints:
    """Checkpoint store for runs under ``root`` (normally ``$SEBASTIAN_HOME/checkpoints``)."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._run: str | None = None
        self._workspace: Path | None = None
        self._entries: dict[str, _Entry] = {}

    # --- recording (called by the agent and the file tools) -----------------------------

    def begin(self, run_id: str) -> None:
        """Start recording for ``run_id``. Nothing is written until a file is changed."""
        if not _RUN_ID.match(run_id):
            raise CheckpointError(f"invalid run id {run_id!r}")
        self._run, self._workspace, self._entries = run_id, None, {}

    def before_write(self, ws: Workspace, target: Path) -> None:
        """Save ``target``'s original state the first time this run touches it."""
        if self._run is None:
            return
        rel = ws.display(target)
        if rel in self._entries:
            return
        self._workspace = ws.root
        before: str | None = None
        if target.is_file():
            data = target.read_bytes()
            before = _sha(data)
            blob = self._dir(self._run) / "blobs" / before
            if not blob.exists():
                _atomic_write(blob, data)
        self._entries[rel] = _Entry(before)
        self._save()

    def after_write(self, ws: Workspace, target: Path) -> None:
        """Record the content hash a write left behind."""
        if self._run is None:
            return
        entry = self._entries.get(ws.display(target))
        if entry is not None:
            entry.after = _sha(target.read_bytes()) if target.is_file() else None
            self._save()

    def rewind(self) -> UndoReport:
        """Restore every file the current run changed to its state before the run (B33).

        Used between Gauntlet attempts, so it always restores (the run owns these files) and
        then records the restored state as the run's latest, so a later ``undo`` stays exact.
        Files changed some other way (for example by ``run_command``) are not tracked here.
        """
        report = UndoReport()
        if self._run is None or self._workspace is None:
            return report
        # P1-09: check every path before touching any, so a swapped-in symlink means no writes.
        ws = Workspace(self._workspace)
        targets: dict[str, Path] = {}
        for rel in self._entries:
            try:
                targets[rel] = ws.resolve_strict(rel)
            except WorkspaceError as exc:
                raise CheckpointError(f"refusing to rewind {rel}: {exc}") from exc
        for rel, entry in sorted(self._entries.items()):
            target = targets[rel]
            if entry.before is None:
                target.unlink(missing_ok=True)
                report.deleted.append(rel)
            else:
                data = (self._dir(self._run) / "blobs" / entry.before).read_bytes()
                if _sha(data) != entry.before:
                    raise CheckpointError(f"checkpoint blob for {rel} is corrupt")
                _atomic_write(target, data)
                report.restored.append(rel)
            entry.after = entry.before
        self._save()
        return report

    # --- inspection and undo -----------------------------------------------------------

    def files(self, run_id: str) -> list[str]:
        """Paths the run changed, relative to its workspace."""
        return sorted(self._load(run_id)[1])

    def workspace_of(self, run_id: str) -> Path:
        """The workspace root the run wrote to."""
        return self._load(run_id)[0]

    def originals(self, run_id: str) -> dict[str, bytes | None]:
        """Each changed file's bytes before the run (None = the run created it)."""
        out: dict[str, bytes | None] = {}
        for rel, entry in self._load(run_id)[1].items():
            if entry.before is None:
                out[rel] = None
                continue
            data = (self._dir(run_id) / "blobs" / entry.before).read_bytes()
            if _sha(data) != entry.before:
                raise CheckpointError(f"checkpoint blob for {rel} is corrupt")
            out[rel] = data
        return out

    def undo(self, run_id: str, ws: Workspace, force: bool = False) -> UndoReport:
        """Restore every file ``run_id`` changed in ``ws`` to its state before the run."""
        root, entries = self._load(run_id)
        if root != ws.root:
            raise CheckpointError(f"run {run_id} wrote to {root}, not {ws.root}")
        report = UndoReport()
        for rel, entry in sorted(entries.items()):
            target = ws.resolve(rel)
            current = _sha(target.read_bytes()) if target.is_file() else None
            if current != entry.after and not force:
                report.skipped.append(rel)
                continue
            if entry.before is None:
                target.unlink(missing_ok=True)
                report.deleted.append(rel)
            else:
                blob = self._dir(run_id) / "blobs" / entry.before
                data = blob.read_bytes()
                if _sha(data) != entry.before:
                    raise CheckpointError(f"checkpoint blob for {rel} is corrupt")
                _atomic_write(target, data)
                report.restored.append(rel)
        return report

    # --- storage ----------------------------------------------------------------------

    def _dir(self, run_id: str) -> Path:
        if not _RUN_ID.match(run_id):
            raise CheckpointError(f"invalid run id {run_id!r}")
        return self.root / run_id

    def _save(self) -> None:
        if self._run is None or self._workspace is None:  # pragma: no cover - guarded by callers
            return
        payload: dict[str, Any] = {
            "version": SCHEMA_VERSION,
            "run_id": self._run,
            "workspace": str(self._workspace),
            "files": {k: {"before": e.before, "after": e.after} for k, e in self._entries.items()},
        }
        data = json.dumps(payload, indent=1, sort_keys=True).encode()
        _atomic_write(self._dir(self._run) / "manifest.json", data)

    def _load(self, run_id: str) -> tuple[Path, dict[str, _Entry]]:
        path = self._dir(run_id) / "manifest.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise CheckpointError(f"no checkpoint for run {run_id}") from None
        except json.JSONDecodeError as exc:
            raise CheckpointError(f"checkpoint for run {run_id} is corrupt") from exc
        if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
            raise CheckpointError(f"checkpoint for run {run_id} has an unsupported format")
        files = raw.get("files")
        if not isinstance(files, dict) or not isinstance(raw.get("workspace"), str):
            raise CheckpointError(f"checkpoint for run {run_id} is corrupt")
        entries: dict[str, _Entry] = {}
        for rel, e in files.items():
            if not isinstance(e, dict) or not all(
                v is None or (isinstance(v, str) and _HASH.match(v))
                for v in (e.get("before"), e.get("after"))
            ):
                raise CheckpointError(f"checkpoint entry for {rel!r} is corrupt")
            entries[rel] = _Entry(e.get("before"), e.get("after"))
        return Path(raw["workspace"]), entries
