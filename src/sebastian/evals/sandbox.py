"""Run eval checkers on code a model wrote, without host secrets, network or unbounded output.

Some eval checkers (B38) judge a case by running the workspace's Python, which is code the
model under test just wrote (P1-18). ``run_python_check`` is the one way they do it:

- The environment is exactly ``PATH``, ``LANG``, ``LC_ALL``, ``HOME=<workspace>`` and
  ``PYTHONDONTWRITEBYTECODE=1``. API keys and every other host variable stay out.
- Under bubblewrap (when usable) the child has no network and sees only the workspace plus
  read-only system and interpreter paths.
- The child gets its own process group, which is killed when the check ends or times out, so
  a background process it started cannot outlive the check.
- Only the last ``MAX_OUTPUT_BYTES`` of stdout are kept; stderr is discarded.

``checker_sandbox`` decides which mode a case gets. Live runs fail closed: with no usable
bubblewrap, cases that run model code are refused unless the user passed
``--unsafe-unsandboxed-checkers``. Scripted (offline) runs may run unsandboxed, still with the
scrubbed environment.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from sebastian.tools.base import ToolError
from sebastian.tools.builtin import (
    SANDBOX_WORKDIR,
    CommandPolicy,
    _limits,
    bwrap_argv,
    python_roots,
    resolve_command_sandbox,
)

CHECK_TIMEOUT = 20.0
MAX_OUTPUT_BYTES = 64 * 1024
ENV_PASSTHROUGH = ("PATH", "LANG", "LC_ALL")
BWRAP, NONE, UNAVAILABLE = "bwrap", "none", "unavailable"
UNAVAILABLE_ERROR = "checker sandbox unavailable"
UNSAFE_WARNING = (
    "warning: --unsafe-unsandboxed-checkers: model-written code runs on the host "
    "(scrubbed env, but network and the host filesystem are reachable)"
)
# Same limits as run_command's defaults (1 GiB address space, 60 s CPU).
_LIMITS = CommandPolicy()
_PYC_IN_SANDBOX = "/tmp/sebastian-eval-pyc"  # noqa: S108 - bwrap's private tmpfs, never written


class CheckerSandboxError(RuntimeError):
    """A checker needs a sandbox this run does not have (live run, no bubblewrap)."""


@dataclass(frozen=True, slots=True)
class CheckRun:
    """What a checker learns from one run: exit code and the tail of stdout."""

    returncode: int | None
    stdout: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """True when the check exited 0 before the timeout."""
        return not self.timed_out and self.returncode == 0


def checker_env(home: str) -> dict[str, str]:
    """The checker's whole environment: an allowlist of host variables plus fixed ones."""
    env = {k: os.environ[k] for k in ENV_PASSTHROUGH if k in os.environ}
    env["HOME"] = home
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def bwrap_ready() -> bool:
    """True when bubblewrap can sandbox commands and checkers here."""
    try:
        sandbox, _ = resolve_command_sandbox("auto")
    except ToolError:  # newer resolvers fail closed instead of falling back
        return False
    return sandbox == BWRAP


def checker_sandbox(*, live: bool, unsafe: bool = False) -> str:
    """``bwrap`` when usable; otherwise ``none`` offline or with ``unsafe``, else refuse."""
    if bwrap_ready():
        return BWRAP
    return NONE if not live or unsafe else UNAVAILABLE


def run_python_check(
    workspace: Path, args: Sequence[str], *, sandbox: str, timeout: float | None = None
) -> CheckRun:
    """Run ``python -E -s <args>`` in ``workspace`` under ``sandbox`` (``bwrap`` or ``none``).

    ``timeout`` defaults to ``CHECK_TIMEOUT`` seconds.
    """
    if sandbox == UNAVAILABLE:
        raise CheckerSandboxError(UNAVAILABLE_ERROR)
    if sandbox not in {BWRAP, NONE}:
        raise ValueError(f"unknown checker sandbox {sandbox!r}")
    with tempfile.TemporaryDirectory(prefix="sebastian-eval-pyc-") as cache:
        # A bytecode prefix nothing has written to: never read a stale .pyc from __pycache__.
        prefix = _PYC_IN_SANDBOX if sandbox == BWRAP else cache
        py = [sys.executable, "-E", "-s", "-X", f"pycache_prefix={prefix}", *args]
        if sandbox == BWRAP:
            env = checker_env(SANDBOX_WORKDIR)
            argv = bwrap_argv(
                _bwrap(), workspace, py[0], py[1:], env, extra_ro=tuple(python_roots())
            )
        else:
            env = checker_env(str(workspace))
            argv = py
        return _run_bounded(argv, workspace, env, CHECK_TIMEOUT if timeout is None else timeout)


def _bwrap() -> str:
    exe = shutil.which("bwrap")
    if exe is None:  # pragma: no cover - checker_sandbox only picks bwrap when it exists
        raise CheckerSandboxError(UNAVAILABLE_ERROR)
    return exe


def _run_bounded(argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> CheckRun:
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        preexec_fn=(lambda: _limits(_LIMITS)) if sys.platform != "win32" else None,  # noqa: PLW1509
    )
    assert proc.stdout is not None  # noqa: S101 - stdout=PIPE above
    tail = bytearray()
    reader = threading.Thread(target=_drain, args=(proc.stdout, tail), daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    _kill_group(proc.pid)  # also reaps background children the check left running
    proc.wait()
    reader.join(timeout=5)
    proc.stdout.close()
    return CheckRun(
        None if timed_out else proc.returncode,
        bytes(tail).decode("utf-8", errors="replace"),
        timed_out,
    )


def _drain(stream: IO[bytes], tail: bytearray) -> None:
    while chunk := stream.read(8192):
        tail += chunk
        if len(tail) > MAX_OUTPUT_BYTES:
            del tail[:-MAX_OUTPUT_BYTES]


def _kill_group(pid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGKILL)
