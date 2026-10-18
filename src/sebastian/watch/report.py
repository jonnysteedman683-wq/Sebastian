"""Build, finalise, sign and verify GROK-WATCHER reports.

Contract: ``schemas/repo_change_report.schema.json``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sebastian.watch import inventory as inv
from sebastian.watch.gitio import Git

REPO = "jonnysteedman683-wq/Sebastian"
SOURCE = "GROK-WATCHER"
TARGET = "CO-ORDINATOR-BOT"
SCHEMA_PATH = "schemas/repo_change_report.schema.json"
CANARY_REASON = "CANARY - test only, no action needed"

# Files where any change deserves a human look, even if no capability changed.
SENSITIVE_PATHS = (
    "src/sebastian/tools/",
    "src/sebastian/approval.py",
    "src/sebastian/trace.py",
    "src/sebastian/agent.py",
    "src/sebastian/prompts.py",
    "docs/SECURITY.md",
    "pyproject.toml",
    ".github/",
    "schemas/",
)

# Changing these could blind or subvert the watcher, or rewrite the agents' own rules, so any
# change is risk +3 and escalated. (schemas/examples/ is test data and deliberately excluded.)
WATCH_TOOLING_PATHS = (
    "src/sebastian/watch/",
    ".github/workflows/watch.yml",
    ".github/workflows/guard.yml",
    "schemas/repo_change_report.schema.json",
    "schemas/README.md",
    "AGENTS.md",
    "CLAUDE.md",
)

# Fields the watcher (Grok) writes; everything else is computed here.
AUTHORED: dict[str, Any] = {
    "summary": "",
    "summary_markdown": None,
    "changes": [],
    "prs": [],
    "security_alerts": [],
    "breaking_changes": [],
    "open_questions": [],
    "pr_previews": [],
    "escalation_reasons": [],
}

_PRIORITY_RANK = {"LOW": 0, "NORMAL": 1, "HIGH": 2}


class ReportError(ValueError):
    pass


# --- signing -------------------------------------------------------------------


def canonical(report: Mapping[str, Any]) -> bytes:
    body = {k: v for k, v in report.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _expected(report: Mapping[str, Any], key: bytes | None) -> str:
    if key is None:  # keyless: content fingerprint; authenticity comes from GitHub provenance
        return hashlib.sha256(canonical(report)).hexdigest()
    return hmac.new(key, canonical(report), hashlib.sha256).hexdigest()


def sign(report: dict[str, Any], key: bytes | None) -> dict[str, Any]:
    """HMAC-SHA256 with ``key``, or a plain SHA-256 fingerprint when ``key`` is None (keyless)."""
    if key is not None and not key:
        raise ReportError("signing key is empty (pass None for keyless mode)")
    report["signature"] = _expected(report, key)
    return report


def signature_ok(report: Mapping[str, Any], key: bytes | None) -> bool:
    given = report.get("signature")
    if not isinstance(given, str) or (key is not None and not key):
        return False
    return hmac.compare_digest(given, _expected(report, key))


# --- schema --------------------------------------------------------------------


def schema_errors(report: Mapping[str, Any], schema: Mapping[str, Any]) -> list[str]:
    try:
        from jsonschema import Draft202012Validator, FormatChecker  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ReportError("validation needs jsonschema: pip install 'sebastian[watch]'") from exc
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    return sorted(
        f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
        for e in validator.iter_errors(report)
    )


def load_schema(path: str | Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    return data


# --- scan ----------------------------------------------------------------------


def scan(git: Git, from_ref: str | None, to_ref: str) -> dict[str, Any]:
    """Facts for the watcher to write its draft from. Read-only."""
    to_sha = git.resolve(to_ref)
    from_sha = git.resolve(from_ref) if from_ref else None
    history_rewritten = from_sha is not None and not git.is_ancestor(from_sha, to_sha)
    old = inv.build(git, from_sha)
    new = inv.build(git, to_sha)
    changed = git.changed_files(from_sha, to_sha)
    signals = [{"points": p, "reason": r} for p, r in inv.risk_signals(old, new)]
    tooling = sorted(f for f in changed if f.startswith(WATCH_TOOLING_PATHS))
    if tooling and from_sha is not None:
        signals.append(
            {
                "points": 3,
                "reason": "watch tooling or report contract changed: " + ", ".join(tooling[:5]),
            }
        )
    return {
        "range": {"from_sha": from_sha, "to_sha": to_sha},
        "history_rewritten": history_rewritten,
        "commits": [{"sha": c.sha, "subject": c.subject} for c in git.commits(from_sha, to_sha)],
        "changed_files": changed,
        "sensitive_files": [f for f in changed if f.startswith(SENSITIVE_PATHS)],
        "version": {"before": old.version, "after": new.version},
        "capability_inventory": new.to_schema(),
        "capability_diff": inv.diff(old, new),
        "risk_signals": signals,
    }


# --- finalise ------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _computed_priority(report: Mapping[str, Any]) -> str:
    cats = {c["category"] for c in report["changes"]} | {
        f for c in report["changes"] for f in c.get("flags", [])
    }
    high_alert = any(a.get("severity") == "HIGH" for a in report["security_alerts"])
    if (
        report["escalate"]
        or {"SECURITY", "BREAKING"} & cats
        or high_alert
        or report["breaking_changes"]
        or report["ci_status"] == "failure"
        or report["risk_score"] >= 6
    ):
        return "HIGH"
    if not cats or cats <= {"DOCS", "QUALITY"}:
        return "LOW"
    return "NORMAL"


def finalize(
    draft: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    seq: int,
    key: bytes | None,
    schema: Mapping[str, Any],
    branch: str = "main",
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Merge the watcher's draft with computed facts, enforce the rules, validate and sign.

    Computed fields always win over the draft. Priority, risk and escalation can only be raised
    by the draft, never lowered below what the facts require.
    """
    unknown = set(draft) - set(AUTHORED) - {"ci_status", "priority", "risk_score", "escalate"}
    if unknown:
        raise ReportError(f"draft has fields the watcher may not set: {sorted(unknown)}")

    report: dict[str, Any] = {k: draft.get(k, default) for k, default in AUTHORED.items()}
    if report["summary_markdown"] is None:
        del report["summary_markdown"]
    signals = facts["risk_signals"]
    computed_risk = min(10, sum(s["points"] for s in signals))
    if facts.get("history_rewritten"):
        computed_risk = min(10, computed_risk + 3)
    report.update(
        report_type="repo_change_report",
        seq=seq,
        source_bot=SOURCE,
        target_bot=TARGET,
        repo=REPO,
        branch=branch,
        range=dict(facts["range"]),
        generated_at=generated_at or _now(),
        version=dict(facts["version"]),
        ci_status=draft.get("ci_status", "unknown"),
        capability_inventory=dict(facts["capability_inventory"]),
        capability_diff=dict(facts["capability_diff"]),
        risk_score=max(int(draft.get("risk_score", 0)), computed_risk),
    )

    reasons = list(report["escalation_reasons"])
    for s in signals:
        if s["points"] >= 4 or s["reason"].startswith("watch tooling"):
            reasons.append(s["reason"])
    if facts.get("history_rewritten"):
        reasons.append("history rewritten: previous reported commit is not an ancestor of head")
    if report["risk_score"] >= 6:
        reasons.append(f"risk_score {report['risk_score']} >= 6")
    report["escalation_reasons"] = list(dict.fromkeys(reasons))  # de-dup, keep order
    report["escalate"] = bool(draft.get("escalate")) or bool(report["escalation_reasons"])
    if report["escalate"] and not report["escalation_reasons"]:
        report["escalation_reasons"] = ["escalated by watcher judgement"]

    floor = _computed_priority(report)
    asked = draft.get("priority", "LOW")
    report["priority"] = max(floor, asked, key=lambda p: _PRIORITY_RANK.get(p, 0))

    sign(report, key)  # signature excludes itself, so sign before validating
    errors = schema_errors(report, schema)
    if errors:
        raise ReportError("report fails schema:\n  " + "\n  ".join(errors))
    return report


def canary(
    git: Git,
    *,
    seq: int,
    key: bytes | None,
    schema: Mapping[str, Any],
    ref: str = "main",
    generated_at: str | None = None,
) -> dict[str, Any]:
    sha = git.resolve(ref)
    current = inv.build(git, sha)
    report: dict[str, Any] = {
        "report_type": "canary",
        "seq": seq,
        "source_bot": SOURCE,
        "target_bot": TARGET,
        "repo": REPO,
        "branch": "main",
        "range": {"from_sha": sha, "to_sha": sha},
        "generated_at": generated_at or _now(),
        "priority": "HIGH",
        "escalate": True,
        "escalation_reasons": [CANARY_REASON],
        "version": {"before": current.version, "after": current.version},
        "ci_status": "unknown",
        "summary": "Weekly canary.",
        "changes": [],
        "prs": [],
        "security_alerts": [],
        "breaking_changes": [],
        "capability_inventory": current.to_schema(),
        "open_questions": [],
        "risk_score": 0,
        "capability_diff": {"added": [], "removed": [], "changed": []},
        "pr_previews": [],
    }
    sign(report, key)
    errors = schema_errors(report, schema)
    if errors:  # pragma: no cover - canary is fully computed; guards schema drift
        raise ReportError("canary fails schema:\n  " + "\n  ".join(errors))
    return report


def verify(
    report: Mapping[str, Any],
    *,
    key: bytes | None,
    schema: Mapping[str, Any],
    expected_seq: int | None = None,
    expected_from_sha: str | None = None,
) -> list[str]:
    """Coordinator-side checks. Returns a list of problems; empty means accept."""
    problems = schema_errors(report, schema)
    if not signature_ok(report, key):
        problems.append("signature: invalid (possible forgery or tampering)")
    if expected_seq is not None and report.get("seq") != expected_seq:
        problems.append(f"seq: expected {expected_seq}, got {report.get('seq')}")
    if expected_from_sha is not None and report.get("report_type") != "canary":
        got = (report.get("range") or {}).get("from_sha")
        if got != expected_from_sha:
            problems.append(f"range.from_sha: expected {expected_from_sha}, got {got}")
    return problems
