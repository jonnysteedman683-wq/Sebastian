"""Typed, permission-scoped tools with JSON schemas inferred from type hints."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, get_type_hints

from sebastian.types import JSON


class Permission(IntEnum):
    """Ordered by risk. Anything at or above the approval threshold needs sign-off."""

    READ = 0
    WRITE = 1
    EXEC = 2
    NETWORK = 3


_PY_TO_JSON: dict[type, str] = {str: "string", int: "integer", float: "number", bool: "boolean"}


class ToolError(Exception):
    """A tool failed in an expected way; the message is returned to the model."""


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    permission: Permission
    func: Callable[..., str]
    schema: JSON

    def invoke(self, arguments: JSON) -> str:
        allowed = set(self.schema["properties"])
        unknown = set(arguments) - allowed
        if unknown:
            raise ToolError(f"unknown argument(s): {sorted(unknown)}")
        missing = [k for k in self.schema["required"] if k not in arguments]
        if missing:
            raise ToolError(f"missing argument(s): {missing}")
        for key, value in arguments.items():
            expected = self.schema["properties"][key]["type"]
            if not _matches(value, expected):
                raise ToolError(f"argument {key!r} must be {expected}")
        return self.func(**arguments)


def _matches(value: Any, json_type: str) -> bool:
    # Exact type checks: bool is a subclass of int and must not pass as one.
    if json_type == "boolean":
        return type(value) is bool
    if json_type == "integer":
        return type(value) is int
    if json_type == "number":
        return type(value) in (int, float)
    return type(value) is str


def build_schema(func: Callable[..., str]) -> JSON:
    hints = get_type_hints(func)
    props: JSON = {}
    required: list[str] = []
    for name, param in inspect.signature(func).parameters.items():
        py_type = hints.get(name)
        if py_type not in _PY_TO_JSON:
            raise TypeError(f"{func.__name__}.{name}: unsupported parameter type {py_type!r}")
        props[name] = {"type": _PY_TO_JSON[py_type]}
        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            props[name]["default"] = param.default
    return {"type": "object", "properties": props, "required": required}


def make_tool(func: Callable[..., str], *, permission: Permission, name: str | None = None) -> Tool:
    doc = inspect.getdoc(func)
    if not doc:
        raise ValueError(f"tool {func.__name__} needs a docstring; it becomes the description")
    return Tool(
        name=name or func.__name__,
        description=doc,
        permission=permission,
        func=func,
        schema=build_schema(func),
    )


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def __iter__(self) -> Iterator[Tool]:
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)
