"""Static capability inventory of Sebastian at a commit.

Built by parsing source with ``ast`` from ``git show`` output. Repository code is never imported
or executed, so a malicious commit cannot run anything inside the watcher.
"""

from __future__ import annotations

import ast
import tomllib
from dataclasses import dataclass, field
from typing import Any

from sebastian.watch.gitio import Git

TOOLS_DIR = "src/sebastian/tools/"
LLM_DIR = "src/sebastian/llm/"
CLI_FILE = "src/sebastian/cli.py"
CONFIG_FILE = "src/sebastian/config.py"
MEMORY_FILE = "src/sebastian/memory.py"
PYPROJECT = "pyproject.toml"
_NON_BACKENDS = {"__init__", "base"}
_HIGH_RISK = {"EXEC", "NETWORK"}
_RANK = {"READ": 0, "WRITE": 1, "EXEC": 2, "NETWORK": 3}


@dataclass(frozen=True)
class Inventory:
    tools: dict[str, str] = field(default_factory=dict)  # name -> permission
    cli_commands: frozenset[str] = frozenset()
    llm_backends: frozenset[str] = frozenset()
    default_model: str | None = None
    version: str | None = None
    dependencies: frozenset[str] = frozenset()
    memory_schema: int | None = None

    def to_schema(self) -> dict[str, Any]:
        return {
            "tools": sorted(self.tools),
            "cli_commands": sorted(self.cli_commands),
            "llm_backends": sorted(self.llm_backends),
            "default_model": self.default_model or "unknown",
        }


def _parse(src: str | None) -> ast.Module | None:
    if src is None:
        return None
    try:
        return ast.parse(src)
    except SyntaxError:
        return None


def _tools(tree: ast.Module) -> dict[str, str]:
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _callee(node) == "make_tool" and node.args):
            continue
        name: str | None = None
        perm = "UNKNOWN"
        first = node.args[0]
        if isinstance(first, ast.Name):
            name = first.id
        for kw in node.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                name = str(kw.value.value)
            if kw.arg == "permission" and isinstance(kw.value, ast.Attribute):
                perm = kw.value.attr
        if name:
            found[name] = perm
    return found


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _cli_commands(tree: ast.Module) -> set[str]:
    return {
        str(node.args[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _callee(node) == "add_parser"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }


def _assigned_constant(tree: ast.Module, target: str) -> Any:
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and any(isinstance(t, ast.Name) and t.id == target for t in node.targets)
        ):
            return node.value.value
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == target
            and isinstance(node.value, ast.Constant)
        ):
            return node.value.value
    return None


def build(git: Git, sha: str | None) -> Inventory:
    """Inventory at ``sha``. ``None`` (no previous report) gives an empty inventory."""
    if sha is None:
        return Inventory()
    tools: dict[str, str] = {}
    for path in git.files(sha, TOOLS_DIR):
        if path.endswith(".py") and (tree := _parse(git.show(sha, path))):
            tools.update(_tools(tree))

    cli_tree = _parse(git.show(sha, CLI_FILE))
    config_tree = _parse(git.show(sha, CONFIG_FILE))
    memory_tree = _parse(git.show(sha, MEMORY_FILE))
    backends = {
        p.removeprefix(LLM_DIR).removesuffix(".py")
        for p in git.files(sha, LLM_DIR)
        if p.endswith(".py") and "/" not in p.removeprefix(LLM_DIR)
    } - _NON_BACKENDS

    version: str | None = None
    deps: set[str] = set()
    raw = git.show(sha, PYPROJECT)
    if raw is not None:
        try:
            project = tomllib.loads(raw).get("project", {})
        except tomllib.TOMLDecodeError:
            project = {}
        version = project.get("version")
        deps.update(project.get("dependencies", []))
        for extra, reqs in project.get("optional-dependencies", {}).items():
            deps.update(f"[{extra}] {r}" for r in reqs)

    model = _assigned_constant(config_tree, "DEFAULT_MODEL") if config_tree else None
    schema = _assigned_constant(memory_tree, "SCHEMA_VERSION") if memory_tree else None
    return Inventory(
        tools=tools,
        cli_commands=frozenset(_cli_commands(cli_tree)) if cli_tree else frozenset(),
        llm_backends=frozenset(backends),
        default_model=model if isinstance(model, str) else None,
        version=version if isinstance(version, str) else None,
        dependencies=frozenset(deps),
        memory_schema=schema if isinstance(schema, int) else None,
    )


def diff(old: Inventory, new: Inventory) -> dict[str, list[str]]:
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    for name in sorted(new.tools.keys() - old.tools.keys()):
        added.append(f"tool: {name} ({new.tools[name]})")
    for name in sorted(old.tools.keys() - new.tools.keys()):
        removed.append(f"tool: {name} ({old.tools[name]})")
    for name in sorted(old.tools.keys() & new.tools.keys()):
        if old.tools[name] != new.tools[name]:
            changed.append(f"tool: {name} permission {old.tools[name]} -> {new.tools[name]}")
    for label, o, n in (
        ("cli", old.cli_commands, new.cli_commands),
        ("backend", old.llm_backends, new.llm_backends),
        ("dependency", old.dependencies, new.dependencies),
    ):
        added += [f"{label}: {x}" for x in sorted(n - o)]
        removed += [f"{label}: {x}" for x in sorted(o - n)]
    if old.default_model and old.default_model != new.default_model:
        changed.append(f"default_model: {old.default_model} -> {new.default_model}")
    if old.memory_schema is not None and old.memory_schema != new.memory_schema:
        changed.append(f"memory_schema: {old.memory_schema} -> {new.memory_schema}")
    return {"added": added, "removed": removed, "changed": changed}


def risk_signals(old: Inventory, new: Inventory) -> list[tuple[int, str]]:
    """Deterministic risk points (work order §8.4). Judgement-based points come from the watcher."""
    signals: list[tuple[int, str]] = []
    for name, perm in sorted(new.tools.items()):
        before = old.tools.get(name)
        if perm in _HIGH_RISK and (before is None or _RANK.get(before, -1) < _RANK[perm]):
            verb = "new tool" if before is None else f"permission raised from {before}"
            signals.append((4, f"{verb}: {name} ({perm})"))
    removed_cli = old.cli_commands - new.cli_commands
    removed_tools = old.tools.keys() - new.tools.keys()
    if removed_cli or removed_tools:
        what = sorted([f"cli {c}" for c in removed_cli] + [f"tool {t}" for t in removed_tools])
        signals.append((2, "breaking: removed " + ", ".join(what)))
    if old.memory_schema is not None and old.memory_schema != new.memory_schema:
        signals.append((2, "breaking: memory schema version changed"))
    if old.dependencies != new.dependencies and (old.dependencies or old.version):
        signals.append((1, "dependencies changed"))
    if old.default_model and old.default_model != new.default_model:
        signals.append((1, f"default model changed to {new.default_model}"))
    return signals
