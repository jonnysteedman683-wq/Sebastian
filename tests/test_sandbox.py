from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from sebastian.tools import CommandPolicy, ToolError, Workspace, command_tool
from sebastian.tools.builtin import SANDBOX_WORKDIR, bwrap_argv


def test_unknown_sandbox_rejected() -> None:
    with pytest.raises(ValueError, match="unknown sandbox"):
        CommandPolicy.of(["echo"], sandbox="docker")


def test_bwrap_argv_isolates() -> None:
    argv = bwrap_argv("/usr/bin/bwrap", Path("/w"), "/usr/bin/cat", ["a.txt"], {"PATH": "/usr/bin"})
    assert argv[:4] == ["/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session"]
    assert argv[-3:] == ["--", "/usr/bin/cat", "a.txt"]
    joined = " ".join(argv)
    assert f"--bind /w {SANDBOX_WORKDIR} --chdir {SANDBOX_WORKDIR}" in joined
    assert "--clearenv --setenv PATH /usr/bin" in joined
    assert "--ro-bind-try /usr /usr" in joined
    # The host home, root and /etc as a whole are never mounted.
    for host in ("/home", "/root", " /etc "):
        assert f"--ro-bind-try {host.strip()} {host.strip()}" not in joined
    assert "--share-net" not in argv


def test_missing_bwrap_is_a_tool_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    tool = command_tool(Workspace(tmp_path), CommandPolicy.of(["echo"], sandbox="bwrap"))
    with pytest.raises(ToolError, match="bubblewrap is not installed"):
        tool.invoke({"command": "echo hi"})


def test_exe_outside_system_paths_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def which(name: str, path: str | None = None) -> str | None:
        return "/usr/bin/bwrap" if name == "bwrap" else "/home/me/bin/tool"

    monkeypatch.setattr("shutil.which", which)
    tool = command_tool(Workspace(tmp_path), CommandPolicy.of(["tool"], sandbox="bwrap"))
    with pytest.raises(ToolError, match="not available inside the sandbox"):
        tool.invoke({"command": "tool x"})


def test_sandboxed_command_is_wrapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A stand-in "bwrap" that prints its argv proves the wrapper is what actually runs.
    fake = tmp_path / "fakebwrap"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    real_which = shutil.which

    def which(name: str, path: str | None = None) -> str | None:
        return str(fake) if name == "bwrap" else real_which(name, path=path)

    monkeypatch.setattr("shutil.which", which)
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = command_tool(Workspace(ws), CommandPolicy.of(["echo"], sandbox="bwrap"))
    out = tool.invoke({"command": "echo hello"})
    lines = out.splitlines()
    assert lines[0] == "exit_code=0"
    assert "--unshare-all" in lines
    assert lines[-3:] == ["--", real_which("echo"), "hello"]
    assert f"HOME\n{SANDBOX_WORKDIR}" in out


def _bwrap_works() -> bool:
    exe = shutil.which("bwrap")
    if exe is None:
        return False
    probe = [exe, "--unshare-all", "--ro-bind", "/", "/", "--", "/bin/true"]
    return subprocess.run(probe, check=False, capture_output=True).returncode == 0


@pytest.mark.skipif(not _bwrap_works(), reason="bubblewrap unavailable or namespaces blocked")
def test_real_bwrap_jails_files_and_network(tmp_path: Path) -> None:  # pragma: no cover
    (tmp_path / "in.txt").write_text("inside")
    tool = command_tool(Workspace(tmp_path), CommandPolicy.of(["cat", "ls"], sandbox="bwrap"))
    assert "inside" in tool.invoke({"command": "cat in.txt"})
    assert "exit_code=0" not in tool.invoke({"command": f"cat {Path.home()}/.bashrc"})
    assert "exit_code=0" not in tool.invoke({"command": "ls /root"})
