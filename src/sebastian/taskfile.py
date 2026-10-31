"""Task files for ``sebastian run --task-file`` (B57).

A task file is the task text, optionally preceded by front matter between ``---`` lines::

    ---
    allow_cmd: [pytest, ruff]
    max_steps: 40
    gauntlet: true
    ---
    Fix the failing parser test and keep ruff clean.

Front matter is ``key: value`` lines (blank lines and ``#`` comments allowed). Only the keys in
``KEYS`` exist, each mirroring a run flag or budget setting; there are deliberately no approval
or sandbox keys, so a file can't widen anything the command line can't. Unknown or repeated
keys and bad values are errors. Standard library only (no YAML).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 256_000
KEYS = ("allow_cmd", "max_steps", "max_tokens", "max_cost", "model", "gauntlet")
_TRUE = {"true", "yes", "on"}
_FALSE = {"false", "no", "off"}


class TaskFileError(ValueError):
    """The task file can't be read or its front matter is invalid."""


@dataclass(frozen=True, slots=True)
class TaskFile:
    """A parsed task file. ``None`` means the file did not set that value."""

    task: str
    allow_cmd: tuple[str, ...] | None = None
    max_steps: int | None = None
    max_tokens: int | None = None
    max_cost: float | None = None
    model: str | None = None
    gauntlet: bool | None = None


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _positive(key: str, value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise TaskFileError(f"{key} must be an integer, got {value!r}") from None
    if n < 1:
        raise TaskFileError(f"{key} must be >= 1")
    return n


def _cost(value: str) -> float:
    try:
        n = float(value)
    except ValueError:
        raise TaskFileError(f"max_cost must be a number, got {value!r}") from None
    if not (math.isfinite(n) and n >= 0):
        raise TaskFileError("max_cost must be zero or more")
    return n


def _commands(value: str) -> tuple[str, ...]:
    inner = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    names = tuple(_unquote(part.strip()) for part in inner.split(",") if part.strip())
    if not names or any(not n or any(c.isspace() for c in n) for n in names):
        raise TaskFileError(f"allow_cmd must list executable names, got {value!r}")
    return names


def _bool(value: str) -> bool:
    low = value.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise TaskFileError(f"gauntlet must be true or false, got {value!r}")


def parse(text: str) -> TaskFile:
    """Parse task-file ``text``."""
    lines = text.splitlines()
    fields: dict[str, str] = {}
    body = lines
    if lines and lines[0].strip() == "---":
        try:
            end = next(i for i, ln in enumerate(lines[1:], 1) if ln.strip() == "---")
        except StopIteration:
            raise TaskFileError("front matter starts with --- but has no closing ---") from None
        for n, raw in enumerate(lines[1:end], 2):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition(":")
            key, value = key.strip(), value.strip()
            if not sep or not key:
                raise TaskFileError(f"line {n}: expected 'key: value', got {line!r}")
            if key not in KEYS:
                raise TaskFileError(f"line {n}: unknown key {key!r} (allowed: {', '.join(KEYS)})")
            if key in fields:
                raise TaskFileError(f"line {n}: {key} is set twice")
            if not value:
                raise TaskFileError(f"line {n}: {key} has no value")
            fields[key] = value
        body = lines[end + 1 :]
    task = "\n".join(body).strip()
    if not task:
        raise TaskFileError("the task file has no task text")
    model = _unquote(fields["model"]).strip() if "model" in fields else None
    if model == "":
        raise TaskFileError("model is empty")
    return TaskFile(
        task=task,
        allow_cmd=_commands(fields["allow_cmd"]) if "allow_cmd" in fields else None,
        max_steps=_positive("max_steps", fields["max_steps"]) if "max_steps" in fields else None,
        max_tokens=(
            _positive("max_tokens", fields["max_tokens"]) if "max_tokens" in fields else None
        ),
        max_cost=_cost(fields["max_cost"]) if "max_cost" in fields else None,
        model=model,
        gauntlet=_bool(fields["gauntlet"]) if "gauntlet" in fields else None,
    )


def load(path: str | Path) -> TaskFile:
    """Read and parse the task file at ``path`` (UTF-8, at most ``MAX_BYTES``)."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise TaskFileError(f"cannot read {p}: {exc.strerror or exc}") from None
    if len(data) > MAX_BYTES:
        raise TaskFileError(f"{p} is larger than {MAX_BYTES:,} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise TaskFileError(f"{p} is not UTF-8 text") from None
    try:
        return parse(text)
    except TaskFileError as exc:
        raise TaskFileError(f"{p}: {exc}") from None
