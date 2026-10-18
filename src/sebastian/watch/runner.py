"""End-to-end watcher run: state -> scan -> Grok draft (or mechanical fallback) -> finalize ->
deliver to CO-ORDINATOR-BOT -> advance state only on acknowledgement.

Designed to run unattended (GitHub Actions). All I/O with the outside world goes through an
injectable ``post`` function so the logic is testable without a network.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sebastian.watch.gitio import Git
from sebastian.watch.report import ReportError, canary, finalize, scan

Post = Callable[[str, Mapping[str, Any], Mapping[str, str]], tuple[int, str]]
Mode = Literal["change", "digest", "canary"]
Action = Literal["quiet", "sent", "published", "dry_run", "failed"]

GROK_FAILURE_ESCALATION = 3
_HIGH_RISK = ("(EXEC)", "(NETWORK)", "-> EXEC", "-> NETWORK")


class StateError(ValueError):
    pass


@dataclass
class State:
    last_reported_sha: str | None = None
    seq: int = 0
    last_run_at: str | None = None
    delivery_failures: int = 0
    grok_failures: int = 0
    coordinator_down_since: str | None = None

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            state = cls(**raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise StateError(f"{path}: unreadable watch state ({exc})") from exc
        if type(state.seq) is not int or state.seq < 0:
            raise StateError(f"{path}: seq must be a non-negative integer")
        return state

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class Endpoints:
    coordinator_url: str
    coordinator_token: str = ""
    grok_url: str = ""
    grok_token: str = ""

    def __post_init__(self) -> None:
        for url in (self.coordinator_url, self.grok_url):
            if url and not url.startswith("https://"):
                raise ValueError(f"endpoint must use https: {url}")


@dataclass
class RunResult:
    action: Action
    state: State
    report: dict[str, Any] | None = None
    detail: str = ""
    notes: list[str] = field(default_factory=list)


def http_post(url: str, payload: Mapping[str, Any], headers: Mapping[str, str]) -> tuple[int, str]:
    """POST JSON. Returns (status, body); status 0 means the request never got a response."""
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(  # noqa: S310 - https enforced by Endpoints
        url, data=data, headers={"Content-Type": "application/json", **headers}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            return resp.status, resp.read(1_000_000).decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(10_000).decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, str(exc)


# --- drafts --------------------------------------------------------------------


def _change(
    category: str,
    title: str,
    path: str,
    sha: str,
    permission: str = "n/a",
    flags: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "category": category,
        "title": title[:120],
        "description": f"Detected automatically from code: {title}.",
        "impact": "unclear (mechanical draft; not reviewed by the watcher)",
        "evidence": [{"sha": sha, "path": path}],
        "permission": permission,
        "flags": list(flags),
    }


def _tool_flags(item: str) -> list[str]:
    return ["SECURITY"] if any(t in item for t in _HIGH_RISK) else []


def _from_added(item: str, sha: str) -> dict[str, Any]:
    kind, _, rest = item.partition(": ")
    if kind == "tool":
        perm = rest.rsplit("(", 1)[-1].rstrip(")")
        return _change(
            "NEW_CAPABILITY",
            f"New tool {rest}",
            "src/sebastian/tools/",
            sha,
            perm,
            _tool_flags(item),
        )
    if kind == "cli":
        return _change("NEW_FEATURE", f"New CLI command {rest}", "src/sebastian/cli.py", sha)
    if kind == "backend":
        return _change("NEW_CAPABILITY", f"New LLM backend {rest}", "src/sebastian/llm/", sha)
    return _change("DEPENDENCY", f"Dependency added: {rest}", "pyproject.toml", sha)


def _from_removed(item: str, sha: str) -> dict[str, Any]:
    kind, _, rest = item.partition(": ")
    if kind == "dependency":
        return _change("DEPENDENCY", f"Dependency removed: {rest}", "pyproject.toml", sha)
    return _change("BREAKING", f"Removed {kind} {rest}", "src/sebastian/", sha, flags=["SECURITY"])


def _from_changed(item: str, sha: str) -> dict[str, Any]:
    if item.startswith("tool:"):
        return _change(
            "PERMISSION_CHANGE", item, "src/sebastian/tools/", sha, flags=_tool_flags(item)
        )
    if item.startswith("default_model"):
        return _change("MODEL_CHANGE", item, "src/sebastian/config.py", sha)
    return _change("BREAKING", item, "src/sebastian/memory.py", sha)


def _file_change(files: Sequence[str], sha: str) -> dict[str, Any]:
    if all(f.startswith("docs/") or f.endswith(".md") for f in files):
        category = "DOCS"
    elif all(f.startswith(("tests/", ".github/")) for f in files):
        category = "QUALITY"
    else:
        category = "ABILITY_CHANGE"
    return _change(category, f"{len(files)} file(s) changed", files[0], sha)


def mechanical_draft(facts: Mapping[str, Any], reason: str) -> dict[str, Any]:
    """Fallback draft built only from computed facts, used when Grok's draft is unavailable."""
    sha = facts["range"]["to_sha"]
    diff = facts["capability_diff"]
    changes = (
        [_from_added(i, sha) for i in diff["added"]]
        + [_from_removed(i, sha) for i in diff["removed"]]
        + [_from_changed(i, sha) for i in diff["changed"]]
    )
    files = list(facts["changed_files"])
    if not changes and files:
        changes.append(_file_change(files, sha))
    subjects = "; ".join(c["subject"] for c in facts["commits"])
    summary = f"{len(facts['commits'])} commit(s): {subjects}" if subjects else "No new commits."
    return {
        "summary": summary[:600],
        "changes": changes,
        "open_questions": [
            f"Mechanical draft ({reason[:200]}): descriptions come from computed capability "
            "changes and commit messages and were not reviewed by the watcher."
        ],
    }


def request_draft(
    post: Post, endpoints: Endpoints, facts: Mapping[str, Any], diff: str
) -> tuple[dict[str, Any] | None, str]:
    if not endpoints.grok_url:
        return None, "GROK_DRAFT_URL not configured"
    headers = {"Authorization": f"Bearer {endpoints.grok_token}"} if endpoints.grok_token else {}
    status, body = post(
        endpoints.grok_url,
        {
            "facts": facts,
            "diff": diff,
            "contract": "docs/watch/GROK_WATCHER_WORK_ORDER.md §2 step 4",
        },
        headers,
    )
    if not 200 <= status < 300:
        return None, f"draft service returned {status}: {body[:200]}"
    try:
        draft = json.loads(body)
    except json.JSONDecodeError:
        return None, "draft service returned non-JSON"
    if not isinstance(draft, dict):
        return None, "draft service returned a non-object"
    return draft, ""


# --- run -----------------------------------------------------------------------


def _deliver(
    post: Post,
    endpoints: Endpoints,
    report: Mapping[str, Any],
    sleep: Callable[[float], None],
    backoff: Sequence[float],
) -> tuple[bool, str]:
    headers = {"X-Watcher-Seq": str(report["seq"])}
    if endpoints.coordinator_token:
        headers["Authorization"] = f"Bearer {endpoints.coordinator_token}"
    last = ""
    for attempt in range(len(backoff) + 1):
        status, body = post(endpoints.coordinator_url, report, headers)
        if 200 <= status < 300:
            return True, f"acknowledged ({status})"
        last = f"status {status}: {body[:200]}"
        if attempt < len(backoff):
            sleep(backoff[attempt])
    return False, last


def _with_reasons(draft: dict[str, Any], reasons: Sequence[str]) -> dict[str, Any]:
    if not reasons:
        return draft
    return {
        **draft,
        "escalate": True,
        "escalation_reasons": [*draft.get("escalation_reasons", []), *reasons],
    }


@dataclass
class _Built:
    report: dict[str, Any]
    grok_failures: int
    notes: list[str]


def _build_change_report(
    git: Git,
    state: State,
    head: str,
    start: str | None,
    reasons: list[str],
    *,
    seq: int,
    key: bytes | None,
    schema: Mapping[str, Any],
    endpoints: Endpoints | None,
    post: Post,
    timestamp: str,
) -> _Built:
    notes: list[str] = []
    facts = scan(git, start, head)
    draft: dict[str, Any]
    if start == head:
        draft, draft_ok = {"summary": f"No changes in last 24h; head = {head[:7]}"}, True
    elif endpoints is None or not endpoints.grok_url:
        # No draft service configured: mechanical draft by design, not a failure.
        draft, draft_ok = mechanical_draft(facts, "no draft service configured"), True
    else:
        got, why = request_draft(post, endpoints, facts, git.diff_text(start, head))
        draft_ok = got is not None
        draft = got if got is not None else mechanical_draft(facts, why)
        if not draft_ok:
            notes.append(f"mechanical draft used: {why}")

    grok_failures = 0 if draft_ok else state.grok_failures + 1
    if grok_failures >= GROK_FAILURE_ESCALATION:
        reasons.append(f"watcher draft service failing for {grok_failures} runs")

    def build(d: dict[str, Any]) -> dict[str, Any]:
        return finalize(
            _with_reasons(d, reasons),
            facts,
            seq=seq,
            key=key,
            schema=schema,
            generated_at=timestamp,
        )

    try:
        report = build(draft)
    except ReportError as exc:
        if not draft_ok:
            raise  # mechanical draft must always be valid; surface the bug
        notes.append(f"watcher draft rejected, mechanical draft used: {str(exc)[:300]}")
        report = build(mechanical_draft(facts, "watcher draft failed validation"))
    return _Built(report, grok_failures, notes)


def run(
    git: Git,
    state: State,
    mode: Mode,
    *,
    key: bytes | None,
    schema: Mapping[str, Any],
    endpoints: Endpoints | None,
    post: Post = http_post,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], str] = lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    ref: str = "origin/main",
    baseline: str | None = None,
    backoff: Sequence[float] = (30, 120, 480),
    dry_run: bool = False,
) -> RunResult:
    """One watcher run.

    * ``endpoints`` set: push to the coordinator; state advances only on a 2xx
      (``seq`` is consumed by any delivery attempt, per the work order).
    * ``endpoints`` None: keyless publish mode; the returned state is advanced and the caller
      commits report + state together (GitHub is the transport and the source of trust).
    * ``key`` None: reports carry a SHA-256 fingerprint instead of an HMAC signature.
    * ``dry_run``: build only; nothing sent, state unchanged.
    """
    sign_key = key
    head = git.resolve(ref)
    timestamp = now()
    new = State(**asdict(state))
    new.seq = state.seq + 1

    if mode == "canary":
        built = _Built(
            canary(git, seq=new.seq, key=sign_key, schema=schema, ref=head, generated_at=timestamp),
            state.grok_failures,
            [],
        )
    else:
        reasons: list[str] = []
        start = state.last_reported_sha or baseline
        if state.last_reported_sha and not git.exists(state.last_reported_sha):
            reasons.append(
                f"history rewritten: last reported {state.last_reported_sha[:7]} no longer exists"
            )
            start = baseline
        if state.coordinator_down_since:
            reasons.append(
                f"coordinator unreachable from {state.coordinator_down_since} to {timestamp}"
            )
        if start == head and mode == "change" and not reasons:
            return RunResult("quiet", state, detail=f"no new commits; head {head[:7]}")
        built = _build_change_report(
            git,
            state,
            head,
            start,
            reasons,
            seq=new.seq,
            key=sign_key,
            schema=schema,
            endpoints=endpoints,
            post=post,
            timestamp=timestamp,
        )

    if dry_run:
        return RunResult("dry_run", state, built.report, "dry run; state unchanged", built.notes)

    if endpoints is None:
        _advance(new, built, head, timestamp)
        return RunResult("published", new, built.report, "ready to publish", built.notes)

    ok, detail = _deliver(post, endpoints, built.report, sleep or time.sleep, backoff)
    if not ok:
        new.delivery_failures = state.delivery_failures + 1
        new.coordinator_down_since = state.coordinator_down_since or timestamp
        return RunResult("failed", new, built.report, f"delivery failed: {detail}", built.notes)

    _advance(new, built, head, timestamp)
    return RunResult("sent", new, built.report, detail, built.notes)


def _advance(new: State, built: _Built, head: str, timestamp: str) -> None:
    new.grok_failures = built.grok_failures
    if built.report["report_type"] != "canary":
        new.last_reported_sha = head
    new.last_run_at = timestamp
    new.delivery_failures = 0
    new.coordinator_down_since = None
