"""Filesystem jail. Every path a tool touches is resolved here and must stay inside the root."""

from __future__ import annotations

from pathlib import Path


class WorkspaceError(PermissionError):
    """Raised when a path escapes the workspace or is otherwise disallowed."""


class Workspace:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise WorkspaceError(f"workspace root is not a directory: {self.root}")

    def resolve(self, relative: str) -> Path:
        """Resolve ``relative`` inside the jail, following symlinks, and reject any escape."""
        if not relative or "\x00" in relative:
            raise WorkspaceError("empty or invalid path")
        candidate = Path(relative)
        if candidate.is_absolute():
            raise WorkspaceError(f"absolute paths are not allowed: {relative}")
        resolved = (self.root / candidate).resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise WorkspaceError(f"path escapes workspace: {relative}")
        return resolved

    def display(self, path: Path) -> str:
        rel = path.relative_to(self.root)
        return "." if str(rel) == "." else rel.as_posix()
