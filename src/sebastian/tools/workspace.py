"""Filesystem jail. Every path a tool touches is resolved here and must stay inside the root."""

from __future__ import annotations

from pathlib import Path


class WorkspaceError(PermissionError):
    """Raised when a path escapes the workspace or is otherwise disallowed."""


class Workspace:
    """The only directory tools may touch; every path is resolved and jailed to it."""

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
        # P1-05: no tool reads or writes git metadata. A rewritten .git file, config or hook
        # would run code the next time the host (outside any sandbox) runs git here.
        inside = resolved.relative_to(self.root).parts
        if any(p.lower() == ".git" for p in (*candidate.parts, *inside)):
            raise WorkspaceError(f"paths inside .git are not allowed: {relative}")
        return resolved

    def display(self, path: Path) -> str:
        """``path`` relative to the workspace, for messages."""
        rel = path.relative_to(self.root)
        return "." if str(rel) == "." else rel.as_posix()
