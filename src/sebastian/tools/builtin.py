"""Built-in tools. File tools are always jailed; command execution is opt-in and sandboxed."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import site
import subprocess
import sys
import sysconfig
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sebastian.checkpoint import Checkpoints
from sebastian.tools.base import Permission, Tool, ToolError, make_tool
from sebastian.tools.workspace import Workspace, WorkspaceError

MAX_READ_BYTES = 200_000
MAX_OUTPUT_CHARS = 20_000
MAX_SEARCH_MATCHES = 200


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def file_tools(ws: Workspace, checkpoints: Checkpoints | None = None) -> list[Tool]:
    """READ and WRITE file tools jailed to ``ws``. Writes are checkpointed when given a store."""

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
        if checkpoints is not None:
            checkpoints.before_write(ws, target)
        target.write_text(content, encoding="utf-8")
        if checkpoints is not None:
            checkpoints.after_write(ws, target)
        return f"wrote {len(content)} chars to {ws.display(target)}"

    def edit_file(path: str, old: str, new: str, replace_all: bool = False) -> str:
        """Replace the exact text `old` with `new` in an existing workspace file.

        `old` must occur exactly once unless `replace_all` is true; include surrounding lines
        to make it unique. Prefer this to write_file for small changes to existing files.
        """
        return _edit(ws, path, old, new, replace_all, checkpoints)

    def list_dir(path: str = ".") -> str:
        """List entries of a directory inside the workspace. Directories end with '/'."""
        target = ws.resolve(path)
        if not target.is_dir():
            raise ToolError(f"not a directory: {path}")
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
        return "\n".join(entries) if entries else "(empty)"

    def search_files(query: str, path: str = ".") -> str:
        """Find lines containing `query` (case-sensitive substring) in workspace text files.

        Returns `path:line: text` (line numbers are 1-based), at most 200 matches. Skips binary
        files, files over the read limit, `.git/` and anything a symlink points outside the jail.
        """
        if not query:
            raise ToolError("empty query")
        base = ws.resolve(path)
        if not base.is_dir():
            raise ToolError(f"not a directory: {path}")
        matches = _search_tree(ws, base, query)
        return _truncate("\n".join(matches)) if matches else "(no matches)"

    return [
        make_tool(read_file, permission=Permission.READ),
        make_tool(write_file, permission=Permission.WRITE),
        make_tool(edit_file, permission=Permission.WRITE),
        make_tool(list_dir, permission=Permission.READ),
        make_tool(search_files, permission=Permission.READ),
    ]


def _edit(
    ws: Workspace,
    path: str,
    old: str,
    new: str,
    replace_all: bool,
    checkpoints: Checkpoints | None,
) -> str:
    if not old:
        raise ToolError("old must not be empty")
    target = ws.resolve(path)
    if not target.is_file():
        raise ToolError(f"not a file: {path}")
    if target.stat().st_size > MAX_READ_BYTES:
        raise ToolError(f"file larger than {MAX_READ_BYTES} bytes: {path}")
    text = target.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        raise ToolError(f"old text not found in {path}; read the file and copy it exactly")
    if count > 1 and not replace_all:
        raise ToolError(f"old text occurs {count} times in {path}; add context or set replace_all")
    if checkpoints is not None:
        checkpoints.before_write(ws, target)
    target.write_text(text.replace(old, new), encoding="utf-8")
    if checkpoints is not None:
        checkpoints.after_write(ws, target)
    return f"replaced {count} occurrence(s) in {ws.display(target)}"


def _search_tree(ws: Workspace, base: Path, query: str) -> list[str]:
    matches: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):  # never follows directory symlinks
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            shown = ws.display(Path(dirpath) / name)
            try:
                target = ws.resolve(shown)  # a file symlink may point outside the jail
            except WorkspaceError:
                continue
            if not target.is_file() or target.stat().st_size > MAX_READ_BYTES:
                continue
            data = target.read_bytes()
            if b"\x00" in data:
                continue
            for lineno, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
                if query in line:
                    if len(matches) == MAX_SEARCH_MATCHES:
                        return [*matches, f"...[capped at {MAX_SEARCH_MATCHES} matches]"]
                    matches.append(f"{shown}:{lineno}: {line}")
    return matches


@dataclass(frozen=True, slots=True)
class CommandPolicy:
    """Sandbox limits for ``run_command``. Nothing runs unless its executable is allowlisted."""

    allowed: frozenset[str] = field(default_factory=frozenset)
    timeout_seconds: float = 60.0
    max_memory_bytes: int = 1_073_741_824  # 1 GiB address space
    max_cpu_seconds: int = 60
    env_passthrough: frozenset[str] = frozenset({"PATH", "LANG", "LC_ALL"})
    # "bwrap": Linux namespaces, no network, workspace-only. "unshare": no sandbox, but no
    # network either (user + net namespace). "none": host process with network.
    sandbox: str = "none"
    env: tuple[tuple[str, str], ...] = ()  # fixed extra variables, e.g. (("PYTHONPATH", "src"),)
    extra_ro: tuple[str, ...] = ()  # extra read-only paths in the sandbox (e.g. a venv)

    def __post_init__(self) -> None:
        if self.sandbox not in SANDBOXES:
            raise ValueError(f"unknown sandbox {self.sandbox!r}; choose from {sorted(SANDBOXES)}")

    @classmethod
    def of(cls, allowed: Iterable[str], **kwargs: object) -> CommandPolicy:
        """Build a policy allowing the executables in ``allowed``."""
        return cls(allowed=frozenset(allowed), **kwargs)  # type: ignore[arg-type]


SANDBOXES = frozenset({"none", "unshare", "bwrap"})
SANDBOX_WORKDIR = "/workspace"
# Read-only system paths visible inside the bwrap sandbox. Nothing else from the host is
# mounted: no /home, no /root, no /etc beyond what the dynamic loader needs.
_RO_SYSTEM = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib32",
    "/lib64",
    "/etc/alternatives",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
    "/etc/ld.so.conf.d",
)


def bwrap_argv(
    bwrap: str,
    root: Path,
    exe: str,
    args: list[str],
    env: dict[str, str],
    extra_ro: Sequence[str] = (),
) -> list[str]:
    """Wrap a command for bubblewrap: every namespace unshared (so no network), the workspace
    bind-mounted read-write at ``/workspace``, system paths (plus ``extra_ro``) read-only, a
    private /tmp, and a cleared environment. ``exe`` must be visible inside the sandbox."""
    argv = [bwrap, "--unshare-all", "--die-with-parent", "--new-session"]
    for path in (*_RO_SYSTEM, *extra_ro):
        argv += ["--ro-bind-try", path, path]
    # A fresh, private tmpfs inside the sandbox; nothing is shared with the host /tmp.
    argv += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]  # noqa: S108
    argv += ["--bind", str(root), SANDBOX_WORKDIR, "--chdir", SANDBOX_WORKDIR, "--clearenv"]
    for key, value in sorted(env.items()):
        argv += ["--setenv", key, value]
    return [*argv, "--", exe, *args]


def _limits(policy: CommandPolicy) -> None:  # pragma: no cover - runs in the child process
    import resource  # noqa: PLC0415 - POSIX-only, imported in the child

    resource.setrlimit(resource.RLIMIT_CPU, (policy.max_cpu_seconds, policy.max_cpu_seconds))
    resource.setrlimit(resource.RLIMIT_AS, (policy.max_memory_bytes, policy.max_memory_bytes))


def parse_command(command: object) -> list[str]:
    """Split a ``run_command`` string into argv (shlex, no shell).

    The single parser for running, approving and keying commands (P1-06), so what a human
    approves is exactly what runs. Raises ``ToolError`` if it is not a non-empty string.
    """
    if not isinstance(command, str):
        raise ToolError("command must be a string")
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ToolError(f"unparseable command: {exc}") from exc
    if not argv:
        raise ToolError("empty command")
    return argv


def command_tool(ws: Workspace, policy: CommandPolicy) -> Tool:
    """The EXEC-level ``run_command`` tool, limited by ``policy``."""
    if not policy.allowed:
        raise ValueError("command tool requires a non-empty allowlist")

    def run_command(command: str) -> str:
        """Run an allowlisted command (no shell) in the workspace. Returns exit code and output."""
        argv = parse_command(command)
        exe = os.path.basename(argv[0])
        if exe != argv[0] or exe not in policy.allowed:
            raise ToolError(f"command not allowed: {argv[0]!r}; allowed: {sorted(policy.allowed)}")
        env = {k: v for k, v in os.environ.items() if k in policy.env_passthrough}
        env["HOME"] = str(ws.root)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(policy.env)
        if policy.sandbox == "bwrap":
            argv = _sandboxed(ws, argv, env, policy.extra_ro)
        elif policy.sandbox == "unshare":
            argv = _netless(argv)
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


def _sandboxed(
    ws: Workspace, argv: list[str], env: dict[str, str], extra_ro: Sequence[str] = ()
) -> list[str]:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise ToolError("sandbox 'bwrap' requested but bubblewrap is not installed")
    exe = shutil.which(argv[0], path=env.get("PATH", ""))
    visible = tuple(p.rstrip("/") + "/" for p in (*_RO_SYSTEM, *extra_ro))
    if exe is None or not exe.startswith(visible):
        raise ToolError(f"{argv[0]!r} is not available inside the sandbox")
    inner = dict(env, HOME=SANDBOX_WORKDIR)
    return bwrap_argv(bwrap, ws.root, exe, argv[1:], inner, extra_ro=extra_ro)


UNSHARE_NET = ("--user", "--map-root-user", "--net")


def _netless(argv: list[str]) -> list[str]:
    unshare = shutil.which("unshare")
    if unshare is None:
        raise ToolError("network isolation needs util-linux 'unshare', which is not installed")
    return [unshare, *UNSHARE_NET, "--", *argv]


def unshare_usable(unshare: str) -> bool:
    """True if ``unshare`` can create an unprivileged user + network namespace here."""
    probe = [unshare, *UNSHARE_NET, "--", "/bin/true"]
    try:
        return subprocess.run(probe, check=False, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def bwrap_usable(bwrap: str) -> bool:
    """True if ``bwrap`` can create an unprivileged sandbox here (namespaces may be blocked)."""
    probe = [bwrap, "--unshare-all", "--ro-bind", "/", "/", "--", "/bin/true"]
    try:
        return subprocess.run(probe, check=False, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def python_roots() -> list[str]:
    """Read-only paths the interpreter and installed tools (ruff, mypy, pytest) live under."""
    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix}
    roots.update(site.getsitepackages())
    roots.add(site.getusersitepackages())
    roots.add(sysconfig.get_path("scripts"))  # console scripts: pytest, ruff, mypy
    roots.add(os.path.join(site.getuserbase(), "bin"))  # pip install --user scripts
    roots.add(str(Path(os.path.realpath(sys.executable)).parent))
    return sorted(r for r in roots if r and r != "/")


def resolve_command_sandbox(mode: str, *, allow_network: bool = False) -> tuple[str, str]:
    """Turn ``auto | bwrap | none`` into the sandbox ``run_command`` will use. Fails closed.

    Returns ``(sandbox, note)``; ``note`` is a warning to print on every run.

    - ``auto`` and ``bwrap`` use bubblewrap and raise ``ToolError`` when it is missing or
      blocked: commands never run unsandboxed unless the user asked for ``none``.
    - ``none`` runs on the host but still without network (``unshare``) when it can; if it
      cannot, it raises unless ``allow_network`` accepts a networked host process.

    ``allow_network`` never loosens bubblewrap, which always unshares the network.
    """
    if mode not in {"auto", "bwrap", "none"}:
        raise ValueError(f"unknown sandbox mode {mode!r}")
    if mode == "none":
        return _resolve_unsandboxed(allow_network)
    exe = shutil.which("bwrap")
    if exe is not None and bwrap_usable(exe):
        return "bwrap", ""
    if mode == "bwrap":
        raise ToolError("sandbox 'bwrap' required but bubblewrap is missing or blocked here")
    raise ToolError(
        "bubblewrap is missing or blocked here, so run_command will not run (--sandbox auto "
        "fails closed); install bubblewrap, or pass --sandbox none to run commands on the host"
    )


def _resolve_unsandboxed(allow_network: bool) -> tuple[str, str]:
    if allow_network:
        return "none", (
            "warning: run_command is NOT sandboxed and has network access "
            "(--sandbox none --allow-network)"
        )
    exe = shutil.which("unshare")
    if exe is not None and unshare_usable(exe):
        return "unshare", (
            "warning: run_command is NOT sandboxed (--sandbox none); "
            "network is cut off with unshare"
        )
    raise ToolError(
        "--sandbox none: unshare cannot isolate the network here, so commands would have "
        "network access; pass --allow-network to accept that"
    )
