"""UniSync reporter against an in-memory fake of the Supabase ledger (no network)."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sebastian import unisync
from sebastian.cli import main
from sebastian.clock import FakeClock
from sebastian.llm import LLM, ScriptedLLM
from sebastian.types import LLMResponse, Usage
from sebastian.unisync import GENESIS, Reporter, UnisyncError, envelope_digest, report, run_summary

URL = "https://ref.supabase.co"
# The first live row in UNISYNCARC, written by Lestinxor's reference implementation (2026-10-30).
LIVE_GENESIS_BODY = {"key": "unisync.status", "value": "ledger live on UNISYNCARC (ap-southeast-2)"}
LIVE_GENESIS_DIGEST = "9f48221d9dbb3aa556b89ffccc31a487c5ef20e201e9204a2734201c7ab35fd0"


class _Resp(io.BytesIO):
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


class FakeLedger:
    """unisync_events rows with the schema's unique seq/prev constraint."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.before_insert: list[Callable[[], None]] = []
        self.fail_with: int | None = None
        self.headers: list[dict[str, str]] = []

    def __call__(self, req: Any, timeout: float | None = None) -> _Resp:
        self.headers.append(dict(req.header_items()))
        if self.fail_with is not None:
            raise urllib.error.HTTPError(req.full_url, self.fail_with, "err", {}, None)  # type: ignore[arg-type]
        if req.get_method() == "GET":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
            assert q["order"] == ["seq.desc"]
            rows = sorted(self.rows, key=lambda r: -r["seq"])[: int(q["limit"][0])]
            return _Resp(json.dumps(rows).encode())
        if self.before_insert:
            self.before_insert.pop(0)()
        row = json.loads(req.data)
        if any(r["seq"] == row["seq"] or r["prev"] == row["prev"] for r in self.rows):
            raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, None)  # type: ignore[arg-type]
        self.rows.append(row)
        return _Resp(b"")

    def add(self, sender: str, kind: str, body: dict[str, Any]) -> None:
        """Insert a valid row as another agent would."""
        seq = len(self.rows)
        prev = self.rows[-1]["digest"] if self.rows else GENESIS
        digest = envelope_digest(seq, sender, kind, "", body, prev)
        self.rows.append(
            {"v": "unisync/1", "seq": seq, "sender": sender, "kind": kind, "task_id": "",
             "body": body, "prev": prev, "digest": digest, "ts": 0}
        )  # fmt: skip


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeLedger:
    f = FakeLedger()
    monkeypatch.setattr("urllib.request.urlopen", f)
    return f


def reporter(fake: FakeLedger, **kw: Any) -> Reporter:
    return Reporter(URL, "svc-key", clock=FakeClock(5.0), opener=fake, **kw)


def chain_ok(rows: list[dict[str, Any]]) -> bool:
    prev = GENESIS
    for i, r in enumerate(rows):
        expect = envelope_digest(
            r["seq"], r["sender"], r["kind"], r["task_id"], r["body"], r["prev"]
        )
        if r["seq"] != i or r["prev"] != prev or r["digest"] != expect:
            return False
        prev = r["digest"]
    return True


def test_digest_matches_the_reference_implementation() -> None:
    digest = envelope_digest(0, "claude", "memory.write", "", LIVE_GENESIS_BODY, GENESIS)
    assert digest == LIVE_GENESIS_DIGEST


def test_from_env_and_https_only() -> None:
    assert Reporter.from_env({}) is None
    assert Reporter.from_env({"UNISYNC_SUPABASE_URL": URL}) is None
    r = Reporter.from_env({"UNISYNC_SUPABASE_URL": URL + "/", "UNISYNC_SUPABASE_KEY": "k"})
    assert r is not None
    assert r.endpoint == URL + "/rest/v1/unisync_events"
    with pytest.raises(UnisyncError, match="https"):
        Reporter("http://ref.supabase.co", "k")


def test_posts_link_onto_the_shared_head(fake: FakeLedger) -> None:
    fake.add("claude", "memory.write", LIVE_GENESIS_BODY)
    r = reporter(fake)
    first = r.post("heartbeat")
    second = r.post("escalate", reason="checks red")
    assert (first.seq, second.seq) == (1, 2)
    assert fake.rows[1]["prev"] == LIVE_GENESIS_DIGEST
    assert all(row["sender"] == "sebastian" and row["task_id"] == "" for row in fake.rows[1:])
    assert fake.rows[2]["ts"] == 5.0
    assert chain_ok(fake.rows)
    assert fake.headers[-1]["Authorization"] == "Bearer svc-key"


def test_empty_ledger_starts_at_genesis(fake: FakeLedger) -> None:
    assert reporter(fake).post("heartbeat").seq == 0
    assert fake.rows[0]["prev"] == GENESIS


def test_lost_race_rereads_head_and_retries(fake: FakeLedger) -> None:
    fake.before_insert.append(lambda: fake.add("grok", "heartbeat", {}))
    posted = reporter(fake).post("heartbeat")
    assert posted.seq == 1
    assert [r["sender"] for r in fake.rows] == ["grok", "sebastian"]
    assert chain_ok(fake.rows)


def test_gives_up_after_retries(fake: FakeLedger) -> None:
    fake.before_insert.extend(lambda: fake.add("grok", "heartbeat", {}) for _ in range(3))
    with pytest.raises(UnisyncError, match="gave up after 2"):
        reporter(fake, retries=2).post("heartbeat")


def test_refuses_to_build_on_a_tampered_head(fake: FakeLedger) -> None:
    fake.add("grok", "heartbeat", {})
    fake.rows[0]["body"] = {"forged": True}
    with pytest.raises(UnisyncError, match="fails verification"):
        reporter(fake).post("heartbeat")
    assert len(fake.rows) == 1


def test_only_task_free_kinds_and_required_fields(fake: FakeLedger) -> None:
    r = reporter(fake)
    with pytest.raises(UnisyncError, match=r"may not post 'approval\.decision'"):
        r.post("approval.decision", approved=True)
    with pytest.raises(UnisyncError, match=r"missing body field.*reason"):
        r.post("escalate")
    assert fake.rows == []


def test_http_errors_become_unisync_errors(fake: FakeLedger) -> None:
    r = reporter(fake)
    fake.fail_with = 401
    with pytest.raises(UnisyncError, match="read failed: HTTP 401"):
        r.post("heartbeat")
    fake.fail_with = None

    def deny_posts(req: Any, timeout: float | None = None) -> _Resp:
        if req.get_method() == "POST":
            raise urllib.error.HTTPError(req.full_url, 403, "no", {}, None)  # type: ignore[arg-type]
        return fake(req, timeout)

    with pytest.raises(UnisyncError, match="write failed: HTTP 403"):
        Reporter(URL, "k", opener=deny_posts).post("heartbeat")


def test_report_is_best_effort(fake: FakeLedger) -> None:
    warnings: list[str] = []
    body = run_summary("run", "ok", run_id="r1", steps=2, tokens=None, stop="")
    value = {"command": "run", "outcome": "ok", "run_id": "r1", "steps": 2}
    assert body == {"key": "last_run", "scope": "sebastian", "value": value}
    assert report(body, reporter(fake), warnings.append) is True
    fake.fail_with = 500
    assert report(body, reporter(fake), warnings.append) is False
    assert warnings
    assert warnings[0].startswith("unisync: report not written")
    assert report(body, None, warnings.append) is False  # no env in tests


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("UNISYNC_SUPABASE_URL", URL)
    monkeypatch.setenv("UNISYNC_SUPABASE_KEY", "svc-key")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))


def _llm(script: list[LLMResponse]) -> Callable[[str], LLM]:
    return lambda _model: ScriptedLLM(script)


def test_cli_run_reports_metadata_only(
    fake: FakeLedger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(monkeypatch, tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    task = "secret-ish task text"
    script = [LLMResponse("the answer", (), Usage(3, 4))]
    assert main(["run", task, "-w", str(ws), "--no-memory"], llm_factory=_llm(script)) == 0
    (row,) = fake.rows
    assert row["kind"] == "memory.write"
    value = row["body"]["value"]
    assert value["command"] == "run"
    assert value["outcome"] == "ok"
    assert (value["stop"], value["steps"], value["tokens"]) == ("completed", 1, 7)
    assert task not in json.dumps(row)
    assert "the answer" not in json.dumps(row)


def test_cli_run_exit_code_unchanged_when_ledger_down(
    fake: FakeLedger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env(monkeypatch, tmp_path)
    fake.fail_with = 503
    ws = tmp_path / "ws"
    ws.mkdir()
    script = [LLMResponse("done", (), Usage(1, 1))]
    assert main(["run", "t", "-w", str(ws), "--no-memory"], llm_factory=_llm(script)) == 0
    assert "unisync: report not written" in capsys.readouterr().err


def test_cli_ping_and_escalate(
    fake: FakeLedger,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _env(monkeypatch, tmp_path)
    assert main(["unisync", "ping"]) == 0
    assert "heartbeat written at seq 0" in capsys.readouterr().out
    assert main(["unisync", "escalate", "watcher saw red CI"]) == 0
    assert fake.rows[1]["body"] == {"reason": "watcher saw red CI"}
    fake.fail_with = 401
    assert main(["unisync", "ping"]) == 1
    assert "HTTP 401" in capsys.readouterr().err
    assert main(["unisync", "escalate"]) == 2
    monkeypatch.delenv("UNISYNC_SUPABASE_KEY")
    assert main(["unisync", "ping"]) == 2


def test_cli_evolve_reports_outcome(
    fake: FakeLedger, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sebastian import evolve

    _env(monkeypatch, tmp_path)
    item = evolve.BacklogItem("B9", "title", "text")
    report = evolve.EvolveReport(
        item, branch="sebastian/evolve-B9", accepted=False, reason="tests red"
    )
    monkeypatch.setattr(evolve, "evolve_once", lambda *a, **k: report)
    assert main(["evolve", "--repo", str(tmp_path), "--no-evals"], llm_factory=_llm([])) == 1
    value = fake.rows[0]["body"]["value"]
    assert value == {"command": "evolve", "outcome": "rejected", "item": "B9",
                     "branch": "sebastian/evolve-B9", "reason": "tests red"}  # fmt: skip


def test_reporter_uses_module_urlopen_by_default(fake: FakeLedger) -> None:
    r = Reporter(URL, "k")
    assert r.opener is fake
    assert unisync.SENDER == "sebastian"
