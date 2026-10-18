"""Contract tests for the GROK-WATCHER -> CO-ORDINATOR-BOT report schema."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from sebastian.cli import _build_parser
from sebastian.config import DEFAULT_MODEL
from sebastian.tools import CommandPolicy, Workspace, command_tool, file_tools

ROOT = Path(__file__).parents[1]
SCHEMA = json.loads((ROOT / "schemas" / "repo_change_report.schema.json").read_text())
EXAMPLES = ROOT / "schemas" / "examples"
TEST_KEY = b"TEST-VECTOR-KEY-NOT-A-SECRET"  # published test vector, never a real key

VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def canonical(report: dict[str, Any]) -> bytes:
    body = {k: v for k, v in report.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign(report: dict[str, Any], key: bytes = TEST_KEY) -> str:
    return hmac.new(key, canonical(report), hashlib.sha256).hexdigest()


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((EXAMPLES / f"{name}.json").read_text())
    return data


def errors(report: dict[str, Any]) -> list[str]:
    return [e.message for e in VALIDATOR.iter_errors(report)]


def test_schema_is_valid_draft_2020_12() -> None:
    Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("name", ["report", "canary"])
def test_examples_validate_and_signatures_verify(name: str) -> None:
    report = load(name)
    assert errors(report) == []
    assert hmac.compare_digest(report["signature"], sign(report))


def test_signature_detects_tampering() -> None:
    report = load("report")
    report["risk_score"] = 0
    assert not hmac.compare_digest(report["signature"], sign(report))


def test_canonical_form_ignores_key_order_and_whitespace() -> None:
    report = load("report")
    shuffled = json.loads(json.dumps(dict(reversed(list(report.items()))), indent=4))
    assert sign(shuffled) == report["signature"]


def mutate(name: str, **changes: Any) -> dict[str, Any]:
    report = copy.deepcopy(load(name))
    for path, value in changes.items():
        target: Any = report
        keys = path.split("__")
        for k in keys[:-1]:
            target = target[int(k)] if k.isdigit() else target[k]
        if value is DELETE:
            del target[keys[-1]]
        else:
            target[keys[-1]] = value
    return report


DELETE = object()


@pytest.mark.parametrize(
    ("name", "changes"),
    [
        ("canary", {"changes": [load("report")["changes"][0]]}),
        ("canary", {"priority": "NORMAL"}),
        ("canary", {"escalate": False, "escalation_reasons": []}),
        ("report", {"risk_score": 6, "escalate": False, "escalation_reasons": []}),
        ("report", {"escalate": True, "escalation_reasons": []}),
        ("report", {"priority": "NORMAL"}),  # escalated report must be HIGH
        ("report", {"risk_score": 11}),
        ("report", {"seq": 0}),
        ("report", {"signature": "not-hex"}),
        ("report", {"extra_field": 1}),
        ("report", {"source_bot": "SOMEONE-ELSE"}),
        ("report", {"repo": "other/repo"}),
        ("report", {"generated_at": "2026-10-18 09:00"}),
        ("report", {"range__to_sha": "XYZ"}),
        ("report", {"changes__0__evidence": []}),
        ("report", {"changes__0__evidence__0__path": "/etc/passwd"}),
        ("report", {"changes__0__category": "VIBES"}),
        ("report", {"changes__0__flags": ["SECURITY", "SECURITY"]}),
        ("report", {"capability_inventory__tools": ["read_file", "read_file"]}),
        ("report", {"summary": DELETE}),
        (
            "report",
            {
                "priority": "NORMAL",
                "escalate": False,
                "escalation_reasons": [],
                "risk_score": 0,
                "security_alerts": [
                    {
                        "severity": "HIGH",
                        "description": "secret committed",
                        "evidence": [{"sha": "035cf8f", "path": "x.env"}],
                    }
                ],
            },
        ),
    ],
)
def test_invalid_reports_rejected(name: str, changes: dict[str, Any]) -> None:
    assert errors(mutate(name, **changes)), f"should be invalid: {changes}"


def test_example_inventory_matches_real_sebastian(tmp_path: Path) -> None:
    """Keeps the example honest: it must describe what Sebastian actually ships."""
    ws = Workspace(tmp_path)
    py = os.path.basename(sys.executable)
    tools = [t.name for t in file_tools(ws)] + [command_tool(ws, CommandPolicy.of([py])).name]
    sub = next(a for a in _build_parser()._actions if a.dest == "cmd")
    cli = sorted(sub.choices or [])
    backends = sorted(
        p.stem
        for p in (ROOT / "src" / "sebastian" / "llm").glob("*.py")
        if p.stem not in {"__init__", "base"}
    )
    inv = load("report")["capability_inventory"]
    assert sorted(inv["tools"]) == sorted(tools)
    assert sorted(inv["cli_commands"]) == cli
    assert sorted(inv["llm_backends"]) == backends
    assert inv["default_model"] == DEFAULT_MODEL


def test_readme_test_vectors_match_examples() -> None:
    readme = (ROOT / "schemas" / "examples" / "VECTORS.md").read_text()
    for name in ("report", "canary"):
        assert load(name)["signature"] in readme


def test_resign_script_is_idempotent_and_tracks_edits(tmp_path: Path) -> None:
    import importlib.util
    import shutil

    spec = importlib.util.spec_from_file_location("resign", ROOT / "scripts" / "resign_examples.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    work = tmp_path / "examples"
    shutil.copytree(EXAMPLES, work)
    before = {p.name: p.read_text() for p in work.iterdir()}
    mod.resign(work)
    assert {p.name: p.read_text() for p in work.iterdir()} == before  # already in step

    report = json.loads((work / "report.json").read_text())
    report["summary"] = "edited"
    (work / "report.json").write_text(json.dumps(report))
    sigs = mod.resign(work)
    edited = json.loads((work / "report.json").read_text())
    assert edited["signature"] == sigs["report.json"] == sign(edited)
    assert sigs["report.json"] in (work / "VECTORS.md").read_text()
