"""Built-in tools. File tools are always jailed; command execution is opt-in and sandboxed."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field

from sebastian.tools.base import Permission, Tool, ToolError, make_tool
from sebastian.tools.workspace import Workspace

MAX_READ_BYTES = 200_000
MAX_OUTPUT_CHARS = 20_000


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def file_tools(ws: Workspace) -> list[Tool]:
    def read_file(path: str, start_line: int = 0, max_lines: int = 0) -> str:
        """Read a UTF-8 text file inside the workspace. `path` is relative to the workspace.

        `start_line` is the 0-based first line to return; `max_lines` caps the count (0 = all).
        """
        if start_line < 0 or max_lines < 0:
            raise ToolError("start_line and max_lines must be >= 0")
        target = ws.resolve(path)
        if not target.is_file():
            raise ToolError(f"not a file: {path}")
        if target.stat().st_size > MAX_READ_BYTES:
            raise ToolError(f"file larger than {MAX_READ_BYTES} bytes: {path}")
        text = target.read_text(encoding="utf-8", errors="replace")
        if start_line == 0 and max_lines == 0:
            return text
        lines = text.splitlines(keepends=True)
        if start_line >= len(lines):
            raise ToolError(f"start_line {start_line} out of range: {path} has {len(lines)} lines")
        end = start_line + max_lines if max_lines else len(lines)
        return "".join(lines[start_line:end])

    def write_file(path: str, content: str) -> str:
        """Create or overwrite a UTF-8 text file inside the workspace."""
        target = ws.resolve(path)
        if target.is_dir():
            raise ToolError(f"path is a directory: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} chars to {ws.display(target)}"

    def list_dir(path: str = ".") -> str:
        """List entries of a directory inside the workspace. Directories end with '/'."""
        target = ws.resolve(path)
        if not target.is_dir():
            raise ToolError(f"not a directory: {path}")
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
        return "\n".join(entries) if entries else "(empty)"

    return [
        make_tool(read_file, permission=Permission.READ),
        make_tool(write_file, permission=Permission.WRITE),
        make_tool(list_dir, permission=Permission.READ),
    ]


@dataclass(frozen=True, slots=True)
class CommandPolicy:
    """Sandbox limits for ``run_command``. Nothing runs unless its executable is allowlisted."""

    allowed: frozenset[str] = field(default_factory=frozenset)
    timeout_seconds: float = 60.0
    max_memory_bytes: int = 1_073_741_824  # 1 GiB address space
    max_cpu_seconds: int = 60
    env_passthrough: frozenset[str] = frozenset({"PATH", "LANG", "LC_ALL"})

    @classmethod
    def of(cls, allowed: Iterable[str], **kwargs: object) -> CommandPolicy:
        return cls(allowed=frozenset(allowed), **kwargs)  # type: ignore[arg-type]


def _limits(policy: CommandPolicy) -> None:  # pragma: no cover - runs in the child process
    import resource  # noqa: PLC0415 - POSIX-only, imported in the child

    resource.setrlimit(resource.RLIMIT_CPU, (policy.max_cpu_seconds, policy.max_cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (policy.max_memory_bytes, policy.max_memory_bytes))


def command_tool(ws: Workspace, policy: CommandPolicy) -> Tool:
    if not policy.allowed:
        raise ValueError("command tool requires a non-empty allowlist")

    def run_command(command: str) -> str:
        """Run an allowlisted command (no shell) in the workspace. Returns exit code and output."""
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ToolError(f"unparseable command: {exc}") from exc
        if not argv:
            raise ToolError("empty command")
        exe = os.path.basename(argv[0])
        if exe != argv[0] or exe not in policy.allowed:
            raise ToolError(f"command not allowed: {argv[0]!r}; allowed: {sorted(policy.allowed)}")
        env = {k: v for k, v in os.environ.items() if k in policy.env_passthrough}
        env["HOME"] = str(ws.root)
        proc = subprocess.Popen(
            argv,
            cwd=ws.root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
            preexec_fn=(lambda: _limits(policy)) if sys.platform != "win32" else None,  # noqa: PLW1509
        )
        try:
            out, _ = proc.communicate(timeout=policy.timeout_seconds)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise ToolError(f"timed out after {policy.timeout_seconds}s") from None
        return f"exit_code={proc.returncode}\n{_truncate(out)}"

    return make_tool(run_command, permission=Permission.EXEC)
