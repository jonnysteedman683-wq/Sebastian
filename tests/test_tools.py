from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from sebastian.tools import (
    CommandPolicy,
    Permission,
    Tool,
    ToolError,
    ToolRegistry,
    Workspace,
    WorkspaceError,
    command_tool,
    file_tools,
    make_tool,
)
from sebastian.tools.builtin import (
    MAX_OUTPUT_CHARS,
    MAX_READ_BYTES,
    MAX_SEARCH_MATCHES,
    _truncate,
)


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


# --- workspace jail -------------------------------------------------------


def test_workspace_resolves_inside(ws: Workspace) -> None:
    assert ws.resolve("a/b.txt") == ws.root / "a" / "b.txt"
    assert ws.display(ws.resolve(".")) == "."


@pytest.mark.parametrize("bad", ["", "../x", "/etc/passwd", "a/../../x", "a\x00b"])
def test_workspace_rejects_escape(ws: Workspace, bad: str) -> None:
    with pytest.raises(WorkspaceError):
        ws.resolve(bad)


def test_workspace_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("s")
    (root / "link").symlink_to(outside)
    with pytest.raises(WorkspaceError):
        Workspace(root).resolve("link")


def test_workspace_root_must_be_dir(tmp_path: Path) -> None:
    f = tmp_path / "file"
    f.write_text("x")
    with pytest.raises(WorkspaceError):
        Workspace(f)


# --- file tools -----------------------------------------------------------


def test_write_read_list_roundtrip(ws: Workspace) -> None:
    t = {tool.name: tool for tool in file_tools(ws)}
    assert "wrote 2 chars" in t["write_file"].invoke({"path": "d/x.txt", "content": "hi"})
    assert t["read_file"].invoke({"path": "d/x.txt"}) == "hi"
    assert t["list_dir"].invoke({}) == "d/"
    assert t["list_dir"].invoke({"path": "d"}) == "x.txt"


def test_list_empty_and_errors(ws: Workspace) -> None:
    t = {tool.name: tool for tool in file_tools(ws)}
    assert t["list_dir"].invoke({"path": "."}) == "(empty)"
    (ws.root / "f").write_text("x")
    (ws.root / "d").mkdir()
    with pytest.raises(ToolError, match="not a directory"):
        t["list_dir"].invoke({"path": "f"})
    with pytest.raises(ToolError, match="not a file"):
        t["read_file"].invoke({"path": "d"})
    with pytest.raises(ToolError, match="is a directory"):
        t["write_file"].invoke({"path": "d", "content": "x"})


def test_read_rejects_large_file(ws: Workspace) -> None:
    (ws.root / "big").write_bytes(b"x" * (MAX_READ_BYTES + 1))
    t = {tool.name: tool for tool in file_tools(ws)}
    with pytest.raises(ToolError, match="larger than"):
        t["read_file"].invoke({"path": "big"})


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"start_line": 0, "max_lines": 2}, "l0\nl1\n"),
        ({"start_line": 2, "max_lines": 2}, "l2\nl3\n"),
        ({"start_line": 3}, "l3\nl4"),
        ({"start_line": 4, "max_lines": 10}, "l4"),
        ({"max_lines": 0}, "l0\nl1\nl2\nl3\nl4"),
    ],
)
def test_read_line_ranges(ws: Workspace, args: dict[str, int], expected: str) -> None:
    (ws.root / "f.txt").write_text("l0\nl1\nl2\nl3\nl4")
    t = {tool.name: tool for tool in file_tools(ws)}
    assert t["read_file"].invoke({"path": "f.txt", **args}) == expected


@pytest.mark.parametrize("args", [{"start_line": 5}, {"start_line": -1}, {"max_lines": -1}])
def test_read_line_range_out_of_range(ws: Workspace, args: dict[str, int]) -> None:
    (ws.root / "f.txt").write_text("l0\nl1\nl2\nl3\nl4")
    t = {tool.name: tool for tool in file_tools(ws)}
    with pytest.raises(ToolError, match=r"out of range|must be >= 0"):
        t["read_file"].invoke({"path": "f.txt", **args})


def _search(ws: Workspace, **args: object) -> str:
    return {tool.name: tool for tool in file_tools(ws)}["search_files"].invoke(dict(args))


def test_search_finds_matches(ws: Workspace) -> None:
    (ws.root / "a.txt").write_text("alpha\nneedle one\n")
    (ws.root / "d").mkdir()
    (ws.root / "d" / "b.py").write_text("x = 1\n# needle two\n")
    assert _search(ws, query="needle") == "a.txt:2: needle one\nd/b.py:2: # needle two"
    assert _search(ws, query="needle", path="d") == "d/b.py:2: # needle two"


def test_search_no_match_and_empty_query(ws: Workspace) -> None:
    (ws.root / "a.txt").write_text("alpha\n")
    assert _search(ws, query="zzz") == "(no matches)"
    with pytest.raises(ToolError, match="empty query"):
        _search(ws, query="")
    with pytest.raises(ToolError, match="not a directory"):
        _search(ws, query="a", path="a.txt")


def test_search_caps_matches(ws: Workspace) -> None:
    (ws.root / "many.txt").write_text("hit\n" * (MAX_SEARCH_MATCHES + 5))
    out = _search(ws, query="hit").splitlines()
    assert len(out) == MAX_SEARCH_MATCHES + 1
    assert out[-1] == f"...[capped at {MAX_SEARCH_MATCHES} matches]"


def test_search_skips_binary_large_and_git(ws: Workspace) -> None:
    (ws.root / "bin.dat").write_bytes(b"needle\x00\x01")
    (ws.root / "big.txt").write_bytes(b"needle\n" + b"x" * MAX_READ_BYTES)
    (ws.root / ".git").mkdir()
    (ws.root / ".git" / "config").write_text("needle\n")
    (ws.root / "ok.txt").write_text("needle\n")
    assert _search(ws, query="needle") == "ok.txt:1: needle"


def test_search_stays_in_jail(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("needle\n")
    (tmp_path / "outdir").mkdir()
    (tmp_path / "outdir" / "s.txt").write_text("needle\n")
    (root / "link.txt").symlink_to(tmp_path / "secret.txt")
    (root / "linkdir").symlink_to(tmp_path / "outdir")
    w = Workspace(root)
    assert _search(w, query="needle") == "(no matches)"
    with pytest.raises(WorkspaceError):
        _search(w, query="needle", path="..")


def test_permissions(ws: Workspace) -> None:
    t = {tool.name: tool for tool in file_tools(ws)}
    assert t["read_file"].permission is Permission.READ
    assert t["write_file"].permission is Permission.WRITE
    assert t["search_files"].permission is Permission.READ


# --- schema + argument validation -----------------------------------------


def test_schema_inference_and_validation() -> None:
    def f(a: str, b: int, c: float = 1.0, d: bool = False) -> str:
        """Doc."""
        return f"{a}{b}{c}{d}"

    tool = make_tool(f, permission=Permission.READ)
    assert tool.schema["required"] == ["a", "b"]
    assert tool.schema["properties"]["c"] == {"type": "number", "default": 1.0}
    assert tool.invoke({"a": "x", "b": 2, "c": 3, "d": True}) == "x23True"
    with pytest.raises(ToolError, match="unknown"):
        tool.invoke({"a": "x", "b": 1, "z": 1})
    with pytest.raises(ToolError, match="missing"):
        tool.invoke({"a": "x"})
    with pytest.raises(ToolError, match="must be integer"):
        tool.invoke({"a": "x", "b": True})  # bool must not pass as int
    with pytest.raises(ToolError, match="must be boolean"):
        tool.invoke({"a": "x", "b": 1, "d": 1})
    with pytest.raises(ToolError, match="must be string"):
        tool.invoke({"a": 1, "b": 1})


def test_make_tool_requires_docstring_and_supported_types() -> None:
    def nodoc(a: str) -> str:
        return a

    def badtype(a: list[str]) -> str:
        """Doc."""
        return ""

    with pytest.raises(ValueError, match="docstring"):
        make_tool(nodoc, permission=Permission.READ)
    with pytest.raises(TypeError, match="unsupported"):
        make_tool(badtype, permission=Permission.READ)


def test_registry(ws: Workspace) -> None:
    reg = ToolRegistry(file_tools(ws))
    assert len(reg) == 5
    assert reg.get("nope") is None
    with pytest.raises(ValueError, match="duplicate"):
        reg.register(file_tools(ws)[0])


def test_truncate() -> None:
    assert _truncate("abc") == "abc"
    long = "x" * (MAX_OUTPUT_CHARS + 5)
    assert _truncate(long).endswith("[truncated 5 chars]")


# --- command tool ---------------------------------------------------------

PY = os.path.basename(sys.executable)


@pytest.fixture
def run_cmd(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> Tool:
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("SEBASTIAN_TEST_SECRET", "leak")
    return command_tool(ws, CommandPolicy.of([PY], timeout_seconds=5))


def test_command_requires_allowlist(ws: Workspace) -> None:
    with pytest.raises(ValueError, match="allowlist"):
        command_tool(ws, CommandPolicy())


def test_command_runs_in_workspace_with_scrubbed_env(run_cmd: Tool, ws: Workspace) -> None:
    code = "import os;print(os.getcwd());print(os.environ.get('SEBASTIAN_TEST_SECRET'))"
    out = run_cmd.invoke({"command": f'{PY} -c "{code}"'})
    assert out.startswith("exit_code=0")
    assert str(ws.root) in out
    assert "leak" not in out


def test_command_no_stale_bytecode(run_cmd: Tool, ws: Workspace) -> None:
    (ws.root / "mod.py").write_text("def foo(): print('v1')", encoding="utf-8")
    (ws.root / "script.py").write_text("import mod\nmod.foo()", encoding="utf-8")

    out1 = run_cmd.invoke({"command": f"{PY} script.py"})
    assert "v1" in out1

    (ws.root / "mod.py").write_text("def foo(): print('v2')", encoding="utf-8")

    out2 = run_cmd.invoke({"command": f"{PY} script.py"})
    assert "v2" in out2


@pytest.mark.parametrize(
    ("command", "match"),
    [
        ("", "empty"),
        ("rm -rf .", "not allowed"),
        ("/usr/bin/python3 -c 1", "not allowed"),
        ("python3 'unterminated", "unparseable"),
    ],
)
def test_command_rejections(run_cmd: Tool, command: str, match: str) -> None:
    with pytest.raises(ToolError, match=match):
        run_cmd.invoke({"command": command})


def test_command_no_shell_injection(run_cmd: Tool, ws: Workspace) -> None:
    out = run_cmd.invoke({"command": f'{PY} -c "print(1)" ; touch pwned'})
    assert not (ws.root / "pwned").exists()
    assert "exit_code" in out


def test_command_timeout_kills(ws: Workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"])
    tool = command_tool(ws, CommandPolicy.of([PY], timeout_seconds=0.5))
    with pytest.raises(ToolError, match="timed out"):
        tool.invoke({"command": f'{PY} -c "import time; time.sleep(30)"'})


def test_edit_file_replaces_unique_text(ws: Workspace) -> None:
    edit = {tool.name: tool for tool in file_tools(ws)}["edit_file"]
    assert edit.permission is Permission.WRITE
    (ws.root / "a.py").write_text("x = 1\ny = 2\n")
    assert edit.invoke({"path": "a.py", "old": "y = 2", "new": "y = 3"}) == (
        "replaced 1 occurrence(s) in a.py"
    )
    assert (ws.root / "a.py").read_text() == "x = 1\ny = 3\n"


def test_edit_file_ambiguity_and_replace_all(ws: Workspace) -> None:
    edit = {tool.name: tool for tool in file_tools(ws)}["edit_file"]
    (ws.root / "a.txt").write_text("a a a")
    with pytest.raises(ToolError, match="occurs 3 times"):
        edit.invoke({"path": "a.txt", "old": "a", "new": "b"})
    assert (ws.root / "a.txt").read_text() == "a a a"  # untouched on error
    assert "3 occurrence" in edit.invoke(
        {"path": "a.txt", "old": "a", "new": "b", "replace_all": True}
    )
    assert (ws.root / "a.txt").read_text() == "b b b"


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"path": "a.txt", "old": "", "new": "x"}, "must not be empty"),
        ({"path": "a.txt", "old": "zzz", "new": "x"}, "not found"),
        ({"path": "missing.txt", "old": "a", "new": "x"}, "not a file"),
        ({"path": "big.txt", "old": "a", "new": "x"}, "larger than"),
    ],
)
def test_edit_file_errors(ws: Workspace, args: dict[str, str], error: str) -> None:
    edit = {tool.name: tool for tool in file_tools(ws)}["edit_file"]
    (ws.root / "a.txt").write_text("abc")
    (ws.root / "big.txt").write_text("a" * (MAX_READ_BYTES + 1))
    with pytest.raises(ToolError, match=error):
        edit.invoke(args)


def test_edit_file_is_jailed(ws: Workspace) -> None:
    edit = {tool.name: tool for tool in file_tools(ws)}["edit_file"]
    with pytest.raises(WorkspaceError):
        edit.invoke({"path": "../etc/passwd", "old": "root", "new": "x"})


# --- P1-05: .git is never reachable through the file tools -------------------------------


@pytest.mark.parametrize(
    "bad", [".git", "./.git", "a/.git/config", ".git/hooks/pre-commit", "sub/.GIT/x", ".git/"]
)
def test_workspace_rejects_git_paths(ws: Workspace, bad: str) -> None:
    with pytest.raises(WorkspaceError, match=r"\.git"):
        ws.resolve(bad)


def test_workspace_rejects_symlink_into_git(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / ".git")
    with pytest.raises(WorkspaceError, match=r"\.git"):
        Workspace(tmp_path).resolve("link/config")


@pytest.mark.parametrize("ok", [".gitignore", ".github/x.yml", "a.git", "x/.gitkeep", "git/x"])
def test_workspace_allows_git_lookalikes(ws: Workspace, ok: str) -> None:
    assert ws.resolve(ok).name == Path(ok).name
