"""Scar tissue: turn a failed real run into a permanent eval case.

``sebastian scar <run_id>`` reads the run from ``traces.jsonl``, rebuilds the workspace as it
was before the run (current files with the run's checkpointed changes reverted), and writes a
JSON case to ``evals/scars/``. The live eval suite then replays the task on every run, so the
same failure is caught if it ever comes back.

A scar file is plain JSON so it can be reviewed and edited in a PR. Its ``expect`` block is a
small declarative checker; the generated default only demands that the task completes, and you
tighten it (exact files, answer text) once you know what "done" meant.

A scar with ``"target": "critic"`` (B35) replays a fixed ``change`` through the Gauntlet with a
scripted actor and planner; only the live model's critic verdict is under test, and
``expect.critic_rejects`` demands that it never says PASS.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sebastian.checkpoint import CheckpointError, Checkpoints
from sebastian.evals.harness import Check, EvalCase, Outcome
from sebastian.llm.scripted import Step
from sebastian.types import LLMResponse, StopReason, ToolCall

SCAR_VERSION = 1
MAX_SEED_FILES = 50
MAX_SEED_BYTES = 200_000
_STOPS = frozenset(r.value for r in StopReason)
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_EXPECT_KEYS = {
    "stop",
    "files",
    "contains",
    "absent",
    "answer_contains",
    "unchanged",
    "no_gated_success",
    "no_gated_attempt",
    "max_steps",
    "critic_rejects",
}
TARGETS = ("agent", "critic")
_COMMAND = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")


class ScarError(ValueError):
    """A run cannot be turned into a scar, or a scar file is invalid."""


# --- checking --------------------------------------------------------------------------


def build_check(expect: Mapping[str, Any]) -> Check:
    """Compile an ``expect`` block into a checker. Every listed condition must hold."""
    unknown = set(expect) - _EXPECT_KEYS
    if unknown:
        raise ScarError(f"unknown expect key(s): {sorted(unknown)}")
    _validate_expect(expect)

    def check(o: Outcome) -> bool:
        conditions = (
            "stop" not in expect or o.result.stop_reason.value == expect["stop"],
            "max_steps" not in expect or o.result.steps <= expect["max_steps"],
            all(o.read(rel) == c for rel, c in expect.get("files", {}).items()),
            all(n in (o.read(rel) or "") for rel, n in expect.get("contains", {}).items()),
            all(o.read(rel) is None for rel in expect.get("absent", [])),
            all(t in o.result.answer for t in expect.get("answer_contains", [])),
            not (expect.get("unchanged") and o.changed()),
            not (expect.get("no_gated_success") and o.gated_successes()),
            not (expect.get("no_gated_attempt") and o.gated_attempts()),
            not expect.get("critic_rejects") or _critic_rejected(o),
        )
        return all(conditions)

    return check


def _critic_rejected(o: Outcome) -> bool:
    """At least one critic verdict, and none of them PASS."""
    verdicts = [e["passed"] for e in o.events if e["event"] == "gauntlet_critique"]
    return bool(verdicts) and not any(verdicts)


def _validate_expect(e: Mapping[str, Any]) -> None:
    def strmap(key: str) -> None:
        v = e.get(key, {})
        if not isinstance(v, dict) or not all(
            isinstance(k, str) and isinstance(x, str) for k, x in v.items()
        ):
            raise ScarError(f"expect.{key} must map paths to strings")

    def strlist(key: str) -> None:
        v = e.get(key, [])
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            raise ScarError(f"expect.{key} must be a list of strings")

    if "stop" in e and e["stop"] not in _STOPS:
        raise ScarError("expect.stop must be one of " + ", ".join(sorted(_STOPS)))
    if "max_steps" in e and (type(e["max_steps"]) is not int or e["max_steps"] < 1):
        raise ScarError("expect.max_steps must be a positive integer")
    for key in ("unchanged", "no_gated_success", "no_gated_attempt", "critic_rejects"):
        if key in e and type(e[key]) is not bool:
            raise ScarError(f"expect.{key} must be true or false")
    strmap("files")
    strmap("contains")
    strlist("absent")
    strlist("answer_contains")


# --- loading -----------------------------------------------------------------------------


def case_from_spec(spec: Mapping[str, Any]) -> EvalCase:
    """Validate one scar spec and build its (live-only) eval case."""
    if not isinstance(spec, dict) or spec.get("version") != SCAR_VERSION:
        raise ScarError("unsupported scar format")
    name, task, files = spec.get("name"), spec.get("task"), spec.get("files", {})
    if not isinstance(name, str) or not _NAME.match(name):
        raise ScarError(f"invalid scar name {name!r}")
    if not isinstance(task, str) or not task.strip():
        raise ScarError(f"{name}: task must be a non-empty string")
    if not isinstance(files, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in files.items()
    ):
        raise ScarError(f"{name}: files must map paths to text")
    for rel in files:
        if rel.startswith("/") or ".." in Path(rel).parts:
            raise ScarError(f"{name}: seed path escapes the workspace: {rel!r}")
    expect = spec.get("expect", {})
    if not isinstance(expect, dict):
        raise ScarError(f"{name}: expect must be an object")
    allow = spec.get("allow_gated", True)
    if type(allow) is not bool:
        raise ScarError(f"{name}: allow_gated must be true or false")
    commands = spec.get("commands", [])
    if not isinstance(commands, list) or not all(
        isinstance(c, str) and _COMMAND.match(c) for c in commands
    ):
        raise ScarError(f"{name}: commands must be a list of executable names")
    target = spec.get("target", "agent")
    if target not in TARGETS:
        raise ScarError(f"{name}: target must be one of {', '.join(TARGETS)}")
    if target == "critic":
        change = _check_change(name, spec.get("change"))
        return critic_case(f"scar_{name}", task, dict(files), change, build_check(expect))
    return EvalCase(
        name=f"scar_{name}",
        task=task,
        files=dict(files),
        check=build_check(expect),
        allow_gated=allow,
        hostile=bool(spec.get("hostile", False)),
        commands=frozenset(commands),
        live_only=True,
    )


def _check_change(name: str, change: Any) -> dict[str, str]:
    if (
        not isinstance(change, dict)
        or not change
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in change.items())
    ):
        raise ScarError(f"{name}: change must map paths to text")
    for rel in change:
        if rel.startswith("/") or ".." in Path(rel).parts or rel.endswith(".py"):
            raise ScarError(f"{name}: change path must be a workspace text file: {rel!r}")
    return dict(change)


def critic_replay(change: Mapping[str, str]) -> tuple[list[Step], list[Step]]:
    """Fixed actor and planner scripts that apply ``change`` and claim the task is done."""
    writes = [
        LLMResponse(
            "Applying the change.", (ToolCall(f"c{i}", "write_file", {"path": p, "content": c}),)
        )
        for i, (p, c) in enumerate(sorted(change.items()))
    ]
    checks = "\n".join(f"file: {p}" for p in sorted(change))
    planner: list[Step] = [
        LLMResponse("1. Make the change.\n2. Finish."),
        LLMResponse(f"<checks>\n{checks}\n</checks>"),
    ]
    return [*writes, LLMResponse("Done.")], planner


def critic_case(
    name: str, task: str, files: Mapping[str, str], change: Mapping[str, str], check: Check
) -> EvalCase:
    """An eval case that tests only the Gauntlet critic against a fixed, tampered change.

    The actor and planner replay ``critic_replay``; whatever model the case runs with plays the
    critic. Once the critic has given its verdict the scripted actor has nothing left to say, so
    the run ends there and a FAIL costs one critic call.
    """
    actor, planner = critic_replay(change)
    return EvalCase(
        name=name,
        task=task,
        files=dict(files),
        check=check,
        script=actor,
        planner_script=planner,
        critic_only=True,
        gauntlet_only=True,
        targets_critic=True,
        live_only=True,
    )


def load_scars(directory: str | Path) -> list[EvalCase]:
    """Every ``*.json`` scar in ``directory`` (sorted by name); a missing directory is empty."""
    root = Path(directory)
    if not root.is_dir():
        return []
    cases = []
    for path in sorted(root.glob("*.json")):
        try:
            spec = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ScarError(f"{path.name}: invalid JSON") from exc
        try:
            cases.append(case_from_spec(spec))
        except ScarError as exc:
            raise ScarError(f"{path.name}: {exc}") from exc
    return cases


# --- creating ----------------------------------------------------------------------------


def run_record(trace_path: str | Path, run_id: str) -> dict[str, Any]:
    """What the trace says about ``run_id``: task, stop, steps, errors, denials."""
    record: dict[str, Any] = {"errors": [], "denied": []}
    try:
        lines = Path(trace_path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        raise ScarError(f"no trace file at {trace_path}") from None
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict) or ev.get("run_id") != run_id:
            continue
        kind = ev.get("event")
        if kind == "run_start":
            record["task"] = ev.get("task")
        elif kind == "run_end":
            record["stop"], record["steps"] = ev.get("stop"), ev.get("steps")
            record["detail"] = ev.get("detail", "")
        elif kind == "tool_error":
            record["errors"].append(f"{ev.get('tool')}: {ev.get('error')}"[:300])
        elif kind == "approval_denied":
            record["denied"].append(str(ev.get("tool")))
    if not isinstance(record.get("task"), str):
        raise ScarError(f"run {run_id} not found in {trace_path}")
    return record


def _seed_files(workspace: Path, originals: Mapping[str, bytes | None]) -> dict[str, str]:
    """The workspace as it was before the run: current text files, run's changes reverted."""
    files: dict[str, str] = {}
    total = 0
    for p in sorted(workspace.rglob("*")):
        if ".git" in p.relative_to(workspace).parts or not p.is_file() or p.is_symlink():
            continue
        rel = p.relative_to(workspace).as_posix()
        if rel in originals:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        files[rel] = text
        total += len(text)
    for rel, data in originals.items():
        if data is None:
            continue
        try:
            files[rel] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        total += len(files[rel])
    if len(files) > MAX_SEED_FILES or total > MAX_SEED_BYTES:
        raise ScarError(
            f"workspace too large to seed a scar ({len(files)} files, {total} chars; "
            f"limits {MAX_SEED_FILES} files, {MAX_SEED_BYTES} chars). Use a smaller workspace."
        )
    return dict(sorted(files.items()))


def make_scar(
    run_id: str,
    trace_path: str | Path,
    workspace: str | Path,
    checkpoints: Checkpoints | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Build a scar spec for a failed run. Refuses runs that completed without tool errors."""
    rec = run_record(trace_path, run_id)
    if rec.get("stop") == "completed" and not rec["errors"]:
        raise ScarError(f"run {run_id} completed cleanly; nothing to learn from")
    originals: dict[str, bytes | None] = {}
    if checkpoints is not None:
        try:
            originals = checkpoints.originals(run_id)
        except CheckpointError:
            originals = {}  # the run changed no files
    if name:
        slug = name
    else:
        words = re.sub(r"[^a-z0-9]+", "-", rec["task"].lower()).strip("-")[:40] or "run"
        slug = f"{words}-{run_id[:8]}"
    if not _NAME.match(slug):
        raise ScarError(f"invalid scar name {slug!r}")
    return {
        "version": SCAR_VERSION,
        "name": slug,
        "task": rec["task"],
        "files": _seed_files(Path(workspace), originals),
        "expect": {"stop": "completed"},
        "allow_gated": True,
        "origin": {
            "run_id": run_id,
            "stop": rec.get("stop", "unknown"),
            "detail": str(rec.get("detail", ""))[:300],
            "steps": rec.get("steps"),
            "tool_errors": rec["errors"][:20],
            "denied": rec["denied"][:20],
        },
    }


def write_scar(spec: Mapping[str, Any], directory: str | Path) -> Path:
    """Write ``spec`` to ``directory/<name>.json``; never overwrites an existing scar."""
    case_from_spec(spec)  # validate before writing
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{spec['name']}.json"
    if path.exists():
        raise ScarError(f"{path} already exists")
    path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
