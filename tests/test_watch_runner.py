"""Unattended watcher run: state handling, drafts, delivery, retries. Fake endpoints, no network."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from sebastian.watch.cli import main as watch_main
from sebastian.watch.gitio import Git
from sebastian.watch.report import verify
from sebastian.watch.runner import (
    DRY_RUN_KEY,
    Endpoints,
    State,
    StateError,
    http_post,
    mechanical_draft,
    run,
)
from test_watch import KEY, SCHEMA, commit, draft, git, v2

COORD = "https://coordinator.example/reports"
GROK = "https://grok.example/draft"
EP = Endpoints(COORD, "coord-token", GROK, "grok-token")
NOW = "2026-10-18T10:00:00Z"


class FakePost:
    """Scripted responses per URL; records every call."""

    def __init__(self, responses: Mapping[str, list[tuple[int, str]]]) -> None:
        self.responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def __call__(
        self, url: str, payload: Mapping[str, Any], headers: Mapping[str, str]
    ) -> tuple[int, str]:
        self.calls.append((url, dict(payload), dict(headers)))
        queue = self.responses.get(url, [])
        return queue.pop(0) if len(queue) > 1 else (queue[0] if queue else (404, "no route"))

    def to(self, url: str) -> list[dict[str, Any]]:
        return [p for u, p, _ in self.calls if u == url]


def ok_draft() -> str:
    return json.dumps(draft(summary="Grok wrote this."))


def go(repo: Path, state: State, post: FakePost, mode: str = "change", **kw: Any) -> Any:
    return run(
        Git(repo),
        state,
        mode,  # type: ignore[arg-type]
        key=kw.pop("key", KEY),
        schema=SCHEMA,
        endpoints=kw.pop("endpoints", EP),
        post=post,
        sleep=kw.pop("sleep", lambda _: None),
        now=lambda: NOW,
        ref="HEAD",
        **kw,
    )


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


# --- state ---------------------------------------------------------------------


def test_state_roundtrip_and_validation(tmp_path: Path) -> None:
    p = tmp_path / "s" / "state.json"
    assert State.load(p) == State()
    State(last_reported_sha="abc", seq=4).save(p)
    assert State.load(p).seq == 4
    p.write_text("{bad")
    with pytest.raises(StateError):
        State.load(p)
    p.write_text(json.dumps({"seq": -1}))
    with pytest.raises(StateError):
        State.load(p)
    p.write_text(json.dumps({"unknown": 1}))
    with pytest.raises(StateError):
        State.load(p)


def test_endpoints_require_https() -> None:
    with pytest.raises(ValueError, match="https"):
        Endpoints("http://insecure.example")
    with pytest.raises(ValueError, match="https"):
        Endpoints(COORD, grok_url="http://insecure.example")


# --- change runs ---------------------------------------------------------------


def test_first_run_sends_grok_draft_and_advances_state(repo: Path) -> None:
    post = FakePost({GROK: [(200, ok_draft())], COORD: [(202, "ok")]})
    result = go(repo, State(), post)
    assert result.action == "sent"
    assert result.state.last_reported_sha == head(repo)
    assert (result.state.seq, result.state.last_run_at) == (1, NOW)
    report = post.to(COORD)[0]
    assert report["summary"] == "Grok wrote this."
    assert verify(report, key=KEY, schema=SCHEMA, expected_seq=1) == []
    grok_req = post.to(GROK)[0]
    assert set(grok_req) == {"facts", "diff", "contract"}
    assert "README.md" in grok_req["diff"]
    headers = {u: h for u, _, h in post.calls}
    assert headers[GROK]["Authorization"] == "Bearer grok-token"
    assert headers[COORD]["Authorization"] == "Bearer coord-token"
    assert headers[COORD]["X-Watcher-Seq"] == "1"


def test_quiet_when_nothing_new(repo: Path) -> None:
    post = FakePost({})
    result = go(repo, State(last_reported_sha=head(repo), seq=3), post)
    assert result.action == "quiet"
    assert result.state.seq == 3
    assert post.calls == []


def test_incremental_run_with_high_risk_change(repo: Path) -> None:
    start = head(repo)
    v2(repo)
    post = FakePost({GROK: [(200, ok_draft())], COORD: [(200, "")]})
    result = go(repo, State(last_reported_sha=start, seq=5), post)
    report = result.report
    assert result.action == "sent"
    assert report["range"]["from_sha"] == start
    assert report["seq"] == 6
    assert report["escalate"] is True
    assert report["priority"] == "HIGH"


@pytest.mark.parametrize(
    "grok_response",
    [(500, "boom"), (200, "not json"), (200, "[1, 2]"), (0, "connection refused")],
)
def test_grok_failure_falls_back_to_mechanical(repo: Path, grok_response: tuple[int, str]) -> None:
    start = head(repo)
    v2(repo)
    post = FakePost({GROK: [grok_response], COORD: [(200, "")]})
    result = go(repo, State(last_reported_sha=start), post)
    assert result.action == "sent"
    assert result.state.grok_failures == 1
    assert any("mechanical draft used" in n for n in result.notes)
    report = post.to(COORD)[0]
    assert report["open_questions"][0].startswith("Mechanical draft")
    titles = [c["title"] for c in report["changes"]]
    assert "New tool fetch_url (NETWORK)" in titles
    assert any(c["category"] == "BREAKING" for c in report["changes"])
    assert any(c["category"] == "MODEL_CHANGE" for c in report["changes"])


def test_invalid_grok_draft_rejected_then_mechanical(repo: Path) -> None:
    start = head(repo)
    commit(repo, {"README.md": "z\n"}, "docs")
    bad = json.dumps({"summary": "x", "capability_inventory": {}})
    post = FakePost({GROK: [(200, bad)], COORD: [(200, "")]})
    result = go(repo, State(last_reported_sha=start), post)
    assert result.action == "sent"
    assert any("watcher draft rejected" in n for n in result.notes)
    assert result.report["changes"][0]["category"] == "DOCS"


def test_no_grok_url_uses_mechanical(repo: Path) -> None:
    post = FakePost({COORD: [(200, "")]})
    result = go(repo, State(), post, endpoints=Endpoints(COORD))
    assert "GROK_DRAFT_URL not configured" in result.notes[0]
    assert post.to(GROK) == []


def test_grok_failing_three_runs_escalates(repo: Path) -> None:
    start = head(repo)
    commit(repo, {"tests/test_x.py": "x = 1\n"}, "tests")
    post = FakePost({GROK: [(503, "")], COORD: [(200, "")]})
    result = go(repo, State(last_reported_sha=start, grok_failures=2), post)
    assert result.report["changes"][0]["category"] == "QUALITY"
    assert result.report["escalate"] is True
    assert "watcher draft service failing for 3 runs" in result.report["escalation_reasons"]


def test_mechanical_code_change_and_dependency_removal(repo: Path) -> None:
    start = head(repo)
    commit(repo, {"src/sebastian/agent.py": "x = 1\n"}, "code")
    facts_draft = mechanical_draft(
        {
            "range": {"to_sha": head(repo)},
            "capability_diff": {
                "added": [],
                "removed": ["dependency: httpx2", "tool: old (READ)"],
                "changed": [],
            },
            "changed_files": ["src/sebastian/agent.py"],
            "commits": [],
        },
        "test",
    )
    cats = [c["category"] for c in facts_draft["changes"]]
    assert cats == ["DEPENDENCY", "BREAKING"]
    assert facts_draft["summary"] == "No new commits."
    post = FakePost({COORD: [(200, "")]})
    r = go(repo, State(last_reported_sha=start), post, endpoints=Endpoints(COORD))
    assert r.report["changes"][0]["category"] == "ABILITY_CHANGE"


# --- delivery ------------------------------------------------------------------


def test_delivery_retries_then_succeeds(repo: Path) -> None:
    sleeps: list[float] = []
    post = FakePost({GROK: [(200, ok_draft())], COORD: [(503, ""), (0, "timeout"), (201, "")]})
    result = go(repo, State(), post, sleep=sleeps.append, backoff=(1, 2, 3))
    assert result.action == "sent"
    assert sleeps == [1, 2]
    assert len(post.to(COORD)) == 3


def test_delivery_failure_keeps_range_consumes_seq_and_flags_outage(repo: Path) -> None:
    start = head(repo)
    commit(repo, {"README.md": "q\n"}, "docs")
    state = State(last_reported_sha=start, seq=10, grok_failures=1)
    post = FakePost({GROK: [(200, ok_draft())], COORD: [(500, "down")]})
    failed = go(repo, state, post, backoff=(0, 0))
    assert failed.action == "failed"
    assert failed.state.last_reported_sha == start  # range not advanced
    assert failed.state.seq == 11  # seq consumed
    assert failed.state.delivery_failures == 1
    assert failed.state.coordinator_down_since == NOW
    assert failed.state.grok_failures == 1  # unchanged on failed delivery
    assert len(post.to(COORD)) == 3

    post2 = FakePost({GROK: [(200, ok_draft())], COORD: [(200, "")]})
    recovered = go(repo, failed.state, post2)
    assert recovered.action == "sent"
    rep = recovered.report
    assert rep["seq"] == 12
    assert rep["range"]["from_sha"] == start  # same range retried
    assert any("coordinator unreachable" in r for r in rep["escalation_reasons"])
    assert recovered.state.coordinator_down_since is None
    assert recovered.state.delivery_failures == 0


def test_outage_notice_sent_even_without_new_commits(repo: Path) -> None:
    state = State(
        last_reported_sha=head(repo), seq=2, coordinator_down_since="2026-10-18T08:00:00Z"
    )
    post = FakePost({COORD: [(200, "")]})
    result = go(repo, state, post)
    assert result.action == "sent"
    assert result.report["escalate"] is True
    assert post.to(GROK) == []


def test_history_rewrite_escalates(repo: Path) -> None:
    gone = "f" * 40
    post = FakePost({GROK: [(200, ok_draft())], COORD: [(200, "")]})
    result = go(repo, State(last_reported_sha=gone, seq=1), post)
    assert result.action == "sent"
    assert any("history rewritten" in r for r in result.report["escalation_reasons"])
    assert result.report["range"]["from_sha"] is None


def test_baseline_used_when_no_state(repo: Path) -> None:
    start = head(repo)
    commit(repo, {"README.md": "b\n"}, "docs")
    post = FakePost({GROK: [(200, ok_draft())], COORD: [(200, "")]})
    result = go(repo, State(), post, baseline=start)
    assert result.report["range"]["from_sha"] == start


# --- digest / canary / dry run ---------------------------------------------------


def test_digest_when_quiet(repo: Path) -> None:
    post = FakePost({COORD: [(200, "")]})
    result = go(repo, State(last_reported_sha=head(repo), seq=1), post, mode="digest")
    rep = result.report
    assert result.action == "sent"
    assert rep["summary"].startswith("No changes in last 24h")
    assert (rep["priority"], rep["changes"], rep["escalate"]) == ("LOW", [], False)
    assert post.to(GROK) == []


def test_canary_does_not_move_range(repo: Path) -> None:
    start = head(repo)
    commit(repo, {"README.md": "c\n"}, "docs")
    post = FakePost({COORD: [(200, "")]})
    result = go(repo, State(last_reported_sha=start, seq=7), post, mode="canary")
    assert result.report["report_type"] == "canary"
    assert result.state.seq == 8
    assert result.state.last_reported_sha == start  # pending changes still reported later


@pytest.mark.parametrize(
    ("key", "endpoints", "why"), [(None, EP, "no signing key"), (KEY, None, "no COORDINATOR_URL")]
)
def test_dry_run_never_sends_or_changes_state(
    repo: Path, key: bytes | None, endpoints: Endpoints | None, why: str
) -> None:
    post = FakePost({GROK: [(200, ok_draft())], COORD: [(200, "")]})
    state = State(seq=4)
    result = go(repo, state, post, key=key, endpoints=endpoints)
    assert result.action == "dry_run"
    assert why in result.detail
    assert result.state is state
    assert post.to(COORD) == []
    used = KEY if key else DRY_RUN_KEY
    assert verify(result.report, key=used, schema=SCHEMA) == []


# --- http_post (no network: patched urlopen) -------------------------------------


def test_http_post_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import urllib.error

    class Resp(io.BytesIO):
        status = 201

        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    seen: list[Any] = []

    def ok(req: Any, timeout: float) -> Resp:
        seen.append(req)
        return Resp(b"done")

    monkeypatch.setattr("urllib.request.urlopen", ok)
    assert http_post(COORD, {"a": 1}, {"X": "y"}) == (201, "done")
    assert json.loads(seen[0].data) == {"a": 1}
    assert seen[0].get_header("Content-type") == "application/json"

    def http_err(req: Any, timeout: float) -> Resp:
        raise urllib.error.HTTPError(COORD, 409, "conflict", {}, io.BytesIO(b"dup"))  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", http_err)
    assert http_post(COORD, {}, {}) == (409, "dup")

    def net_err(req: Any, timeout: float) -> Resp:
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr("urllib.request.urlopen", net_err)
    status, body = http_post(COORD, {}, {})
    assert status == 0
    assert "unreachable" in body


# --- CLI ----------------------------------------------------------------------


def test_cli_run_dry_and_state_file(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    schema = str(Path(__file__).parents[1] / "schemas" / "repo_change_report.schema.json")
    state_file = tmp_path / "state" / "state.json"
    out = tmp_path / "report.json"
    for var in ("WATCHER_HMAC_KEY", "COORDINATOR_URL", "GROK_DRAFT_URL"):
        monkeypatch.delenv(var, raising=False)
    args = [
        "--repo",
        str(repo),
        "--schema",
        schema,
        "run",
        "--state",
        str(state_file),
        "--ref",
        "HEAD",
    ]
    code = watch_main([*args, "--out", str(out)])
    summary = json.loads(capsys.readouterr().out)
    assert code == 0
    assert summary["action"] == "dry_run"
    assert json.loads(out.read_text())["seq"] == 1
    assert not state_file.exists()

    monkeypatch.setenv("WATCHER_HMAC_KEY", "k")
    monkeypatch.setenv("COORDINATOR_URL", "https://127.0.0.1:9/never")  # nothing listens
    monkeypatch.setattr("sebastian.watch.runner.time.sleep", lambda _: None)
    code = watch_main(args)
    summary = json.loads(capsys.readouterr().out)
    assert code == 1
    assert summary["action"] == "failed"
    assert json.loads(state_file.read_text())["seq"] == 1

    monkeypatch.setenv("COORDINATOR_URL", "http://insecure")
    assert watch_main(args) == 2
    state_file.write_text("{bad")
    monkeypatch.setenv("COORDINATOR_URL", "https://127.0.0.1:9/never")
    assert watch_main(args) == 2
