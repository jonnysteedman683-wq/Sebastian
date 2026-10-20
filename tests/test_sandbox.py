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


def test_extra_ro_makes_tools_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    tool = venv_bin / "mytool"
    tool.write_text("#!/bin/sh\necho tool\n")
    tool.chmod(0o755)
    fake = tmp_path / "fakebwrap"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    real_which = shutil.which

    def which(name: str, path: str | None = None) -> str | None:
        return str(fake) if name == "bwrap" else real_which(name, path=path)

    monkeypatch.setattr("shutil.which", which)
    monkeypatch.setenv("PATH", f"{venv_bin}:/usr/bin:/bin")
    ws = tmp_path / "ws"
    ws.mkdir()
    hidden = command_tool(Workspace(ws), CommandPolicy.of(["mytool"], sandbox="bwrap"))
    with pytest.raises(ToolError, match="not available inside the sandbox"):
        hidden.invoke({"command": "mytool"})
    venv = str(tmp_path / "venv")
    seen = command_tool(
        Workspace(ws), CommandPolicy.of(["mytool"], sandbox="bwrap", extra_ro=(venv,))
    )
    out = seen.invoke({"command": "mytool x"}).splitlines()
    assert f"--ro-bind-try\n{venv}\n{venv}" in "\n".join(out)
    assert out[-3:] == ["--", str(tool), "x"]


# --- sandbox by default (B19) ------------------------------------------------------------

from sebastian.tools.builtin import python_roots, resolve_command_sandbox  # noqa: E402


def _usable(monkeypatch: pytest.MonkeyPatch, ok: bool) -> None:
    import sebastian.tools.builtin as b

    monkeypatch.setattr("shutil.which", lambda n, *a, **k: "/usr/bin/bwrap" if ok else None)
    monkeypatch.setattr(b, "bwrap_usable", lambda exe: ok)


def _unshare(monkeypatch: pytest.MonkeyPatch, ok: bool) -> None:
    import sebastian.tools.builtin as b

    real = shutil.which

    def which(name: str, *a: object, **k: object) -> str | None:
        if name == "unshare":
            return "/usr/bin/unshare" if ok else None
        found: str | None = real(name, *a, **k)  # type: ignore[call-overload]
        return found

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(b, "unshare_usable", lambda exe: ok)


def test_resolve_command_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="unknown sandbox"):
        resolve_command_sandbox("docker")
    _usable(monkeypatch, True)
    assert resolve_command_sandbox("auto") == ("bwrap", "")
    assert resolve_command_sandbox("bwrap") == ("bwrap", "")
    # --allow-network never loosens bwrap: it always unshares the network.
    assert resolve_command_sandbox("auto", allow_network=True) == ("bwrap", "")
    _usable(monkeypatch, False)
    with pytest.raises(ToolError, match="missing or blocked"):
        resolve_command_sandbox("bwrap")


def test_auto_fails_closed_without_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1-01: auto never silently falls back to running commands on the host."""
    _usable(monkeypatch, False)
    with pytest.raises(ToolError, match="--sandbox none"):
        resolve_command_sandbox("auto")
    with pytest.raises(ToolError):
        resolve_command_sandbox("auto", allow_network=True)


def test_sandbox_none_isolates_network_or_needs_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _unshare(monkeypatch, True)
    mode, note = resolve_command_sandbox("none")
    assert mode == "unshare"
    assert "NOT sandboxed" in note
    assert "network" in note
    mode, note = resolve_command_sandbox("none", allow_network=True)
    assert mode == "none"
    assert "NOT sandboxed" in note
    assert "network access" in note
    _unshare(monkeypatch, False)
    with pytest.raises(ToolError, match="--allow-network"):
        resolve_command_sandbox("none")
    assert resolve_command_sandbox("none", allow_network=True)[0] == "none"


def test_unshare_policy_wraps_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "unshare"
    fake.write_text('#!/bin/sh\nfor a in "$@"; do echo "$a"; done\n')
    fake.chmod(0o755)
    real = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda n, *a, **k: str(fake) if n == "unshare" else real(n, *a, **k),
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = command_tool(Workspace(ws), CommandPolicy.of(["echo"], sandbox="unshare"))
    out = tool.invoke({"command": "echo hi"}).splitlines()
    assert out[0] == "exit_code=0"
    assert out[1:] == ["--user", "--map-root-user", "--net", "--", "echo", "hi"]


def test_unshare_policy_missing_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda *a, **k: None)
    tool = command_tool(Workspace(tmp_path), CommandPolicy.of(["echo"], sandbox="unshare"))
    with pytest.raises(ToolError, match="unshare"):
        tool.invoke({"command": "echo hi"})


@pytest.mark.skipif(shutil.which("unshare") is None, reason="needs util-linux unshare")
def test_unshare_usable_probe(tmp_path: Path) -> None:
    from sebastian.tools.builtin import unshare_usable

    assert not unshare_usable(str(tmp_path / "missing"))
    # The real probe either works or reports False; it never raises.
    assert unshare_usable(shutil.which("unshare") or "") in {True, False}


def test_python_roots_cover_this_interpreter() -> None:
    import sys

    roots = python_roots()
    assert sys.prefix in roots
    assert "/" not in roots


def test_cli_defaults_to_sandboxed_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import io
    import sys

    from sebastian import cli
    from sebastian.llm import ScriptedLLM
    from sebastian.types import LLMResponse

    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    seen: list[CommandPolicy] = []
    from sebastian.tools import command_tool as real

    def spy(ws: Workspace, policy: CommandPolicy) -> object:
        seen.append(policy)
        return real(ws, policy)

    monkeypatch.setattr(cli, "command_tool", spy)
    ws = tmp_path / "ws"
    ws.mkdir()
    base = ["run", "t", "-w", str(ws), "--no-memory", "--allow-cmd", "pytest"]

    def go(*extra: str) -> int:
        return cli.main([*base, *extra], llm_factory=lambda m: ScriptedLLM([LLMResponse("ok")]))

    _usable(monkeypatch, True)
    assert go() == 0
    assert seen[-1].sandbox == "bwrap"
    assert sys.prefix in seen[-1].extra_ro
    _usable(monkeypatch, False)
    n = len(seen)
    assert go() == 2  # P1-01: auto fails closed, nothing is registered
    assert len(seen) == n
    assert "--sandbox none" in capsys.readouterr().err
    assert go("--sandbox", "bwrap") == 2
    assert "required" in capsys.readouterr().err
    _unshare(monkeypatch, True)
    assert go("--sandbox", "none") == 0
    assert seen[-1].sandbox == "unshare"
    assert "NOT sandboxed" in capsys.readouterr().err  # warned on every run
    assert go("--sandbox", "none") == 0
    assert "NOT sandboxed" in capsys.readouterr().err
    _unshare(monkeypatch, False)
    assert go("--sandbox", "none") == 2
    assert "--allow-network" in capsys.readouterr().err
    assert go("--sandbox", "none", "--allow-network") == 0
    assert seen[-1].sandbox == "none"
    assert "network access" in capsys.readouterr().err
    import json

    starts = [
        json.loads(line)
        for line in (tmp_path / "home" / "traces.jsonl").read_text().splitlines()
        if '"run_start"' in line
    ]
    assert starts[0]["sandbox"] == "bwrap"
    assert starts[0]["network"] == "isolated"
    assert starts[-2]["sandbox"] == "none"
    assert starts[-2]["network"] == "isolated"
    assert starts[-1]["sandbox"] == "none"
    assert starts[-1]["network"] == "host"
