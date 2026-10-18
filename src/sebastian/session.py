"""Saved conversations, so a run that stopped early can be resumed.

After every step the agent writes the run's system prompt and full message history to
``$SEBASTIAN_HOME/sessions/<run_id>.json`` (atomically). ``sebastian resume <run_id>`` reloads
it and continues the same conversation under a fresh budget, as a new run that records which
run it resumed. Completed runs are not resumable.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sebastian.types import Message, Role, ToolCall, ToolResult

SESSION_VERSION = 1
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
RUNNING = "running"


class SessionError(ValueError):
    """A session is missing, invalid or cannot be resumed."""


@dataclass(slots=True)
class Session:
    """One run's resumable state."""

    run_id: str
    task: str
    system: str
    messages: list[Message] = field(default_factory=list)
    status: str = RUNNING  # "running" or the run's stop reason
    detail: str = ""
    workspace: str = ""
    resumed_from: str = ""


def message_to_json(m: Message) -> dict[str, Any]:
    """Plain-JSON form of a message."""
    return {
        "role": m.role.value,
        "text": m.text,
        "tool_calls": [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in m.tool_calls
        ],
        "tool_results": [
            {"call_id": r.call_id, "content": r.content, "is_error": r.is_error}
            for r in m.tool_results
        ],
    }


def message_from_json(raw: Any) -> Message:
    """Parse and validate one stored message."""
    try:
        role = Role(raw["role"])
        text = raw["text"]
        calls = tuple(
            ToolCall(str(c["id"]), str(c["name"]), dict(c["arguments"])) for c in raw["tool_calls"]
        )
        results = tuple(
            ToolResult(str(r["call_id"]), str(r["content"]), bool(r["is_error"]))
            for r in raw["tool_results"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SessionError(f"invalid stored message: {exc}") from exc
    if not isinstance(text, str):
        raise SessionError("invalid stored message: text must be a string")
    return Message(role, text, calls, results)


class SessionStore:
    """Directory of saved sessions."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, run_id: str) -> Path:
        if not _RUN_ID.match(run_id):
            raise SessionError(f"invalid run id {run_id!r}")
        return self.root / f"{run_id}.json"

    def save(self, s: Session) -> None:
        """Write ``s`` atomically."""
        path = self._path(s.run_id)
        payload = {
            "version": SESSION_VERSION,
            "run_id": s.run_id,
            "task": s.task,
            "system": s.system,
            "status": s.status,
            "detail": s.detail,
            "workspace": s.workspace,
            "resumed_from": s.resumed_from,
            "messages": [message_to_json(m) for m in s.messages],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".session-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def load(self, run_id: str) -> Session:
        """Read and validate a saved session."""
        path = self._path(run_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SessionError(f"no saved session for run {run_id}") from None
        except json.JSONDecodeError as exc:
            raise SessionError(f"session for run {run_id} is corrupt") from exc
        if not isinstance(raw, dict) or raw.get("version") != SESSION_VERSION:
            raise SessionError(f"session for run {run_id} has an unsupported format")
        keys = ("run_id", "task", "system", "status", "detail", "workspace", "resumed_from")
        if not all(isinstance(raw.get(k), str) for k in keys):
            raise SessionError(f"session for run {run_id} is corrupt")
        messages = raw.get("messages")
        if not isinstance(messages, list) or not messages:
            raise SessionError(f"session for run {run_id} has no messages")
        return Session(
            run_id=raw["run_id"],
            task=raw["task"],
            system=raw["system"],
            messages=[message_from_json(m) for m in messages],
            status=raw["status"],
            detail=raw["detail"],
            workspace=raw["workspace"],
            resumed_from=raw["resumed_from"],
        )
