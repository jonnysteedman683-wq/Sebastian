"""B26b: evolve's PRs in the trust ladder."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sebastian import history, prledger
from sebastian.cli import main
from sebastian.prledger import (
    LedgerError,
    PrState,
    load,
    parse_reverts,
    parse_state,
    record_opened,
    sync,
)
from sebastian.trust import Level, assess

M = "claude-sonnet-5-5"
URL1 = "https://github.com/o/r/pull/1"
URL2 = "https://github.com/o/r/pull/2"
URL3 = "https://github.com/o/r/pull/3"
SHA1 = "a" * 40
SHA2 = "b" * 40
GIT = shutil.which("git") or "git"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(h))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return h


def _evals(home: Path, n: int, t0: float = 0.0) -> None:
    for i in range(n):
        history.append(home / history.HISTORY_FILE, history.Record(t0 + i, "eval", M, 10, 10))


def _states(**by_url: PrState) -> dict[str, PrState]:
    return {URL1: PrState("OPEN"), URL2: PrState("OPEN"), URL3: PrState("OPEN"), **by_url}


def test_reverted_pr_drops_a_level(home: Path) -> None:
    """Done when: a reverted PR drops a level in ``assess``."""
    _evals(home, 3)  # three passing rounds: L1
    assert assess(history.load(home / history.HISTORY_FILE)).level is Level.L1_DRAFT_PR
    record_opened(home, URL1, "sebastian/evolve-b3-1", "B3", M, 10.0)
    states = _states(**{URL1: PrState("MERGED", 20.0, SHA1)})
    assert sync(home, states.__getitem__, {SHA1: 30.0}, 99.0) == [
        f"merged   {URL1}",
        f"reverted {URL1} (counts as a failed round)",
    ]
    a = assess(history.load(home / history.HISTORY_FILE))
    assert a.level is Level.L0_SUGGEST
    assert a.reason == f"dropped after a reverted PR ({URL1})"


def test_merged_prs_count_as_passing_rounds(home: Path) -> None:
    _evals(home, 2)
    for i, url in enumerate((URL1, URL2, URL3)):
        record_opened(home, url, f"b{i}", f"B{i}", M, 5.0)
    states = _states(
        **{URL1: PrState("MERGED", 10.0, SHA1), URL2: PrState("CLOSED"), URL3: PrState("OPEN")}
    )
    assert sync(home, states.__getitem__, {}, 50.0) == [
        f"merged   {URL1}",
        f"closed   {URL2} (not a round)",
    ]
    a = assess(history.load(home / history.HISTORY_FILE))
    assert (a.level, a.rounds) == (Level.L1_DRAFT_PR, 3)  # 2 evals + 1 merged PR
    recs = [r for r in history.load(home / history.HISTORY_FILE) if r.kind == "evolve_pr"]
    assert [(r.t, r.model, r.passed, r.total, r.run_id) for r in recs] == [(10.0, M, 1, 1, URL1)]


def test_sync_is_idempotent_and_catches_late_reverts(home: Path) -> None:
    record_opened(home, URL1, "b", "B3", M, 1.0)
    record_opened(home, URL2, "c", "B4", M, 1.0)
    merged = _states(**{URL1: PrState("MERGED", None, SHA1), URL2: PrState("CLOSED")})
    assert len(sync(home, merged.__getitem__, {}, 7.0)) == 2
    calls: list[str] = []

    def fetch(url: str) -> PrState:
        calls.append(url)
        return merged[url]

    assert sync(home, fetch, {}, 8.0) == []
    assert calls == [URL1]  # closed is final; merged is re-checked for a revert
    assert sync(home, fetch, {SHA1: 9.0}, 10.0) == [f"reverted {URL1} (counts as a failed round)"]
    assert sync(home, fetch, {SHA1: 9.0}, 11.0) == []
    prs = load(home)
    assert prs[URL1].outcomes == {"merged", "reverted"}
    assert prs[URL2].outcomes == {"closed"}
    recs = [r for r in history.load(home / history.HISTORY_FILE) if r.kind == "evolve_pr"]
    assert [(r.t, r.passed) for r in recs] == [(7.0, 1), (9.0, 0)]  # merge time falls back to now


def test_ledger_skips_bad_lines(home: Path) -> None:
    home.mkdir()
    lines = [
        "nope",
        json.dumps(["list"]),
        json.dumps({"event": "merged", "url": URL1}),  # outcome before any opened: ignored
        json.dumps({"event": "opened", "url": URL1, "t": "x", "branch": "b", "item": "i"}),
        json.dumps({"event": "opened", "url": URL2, "t": 1, "branch": 2, "item": "i", "model": M}),
        json.dumps(
            {"event": "opened", "url": URL3, "t": 1, "branch": "b", "item": "i", "model": M}
        ),
    ]
    (home / prledger.LEDGER_FILE).write_text("\n".join(lines) + "\n")
    assert list(load(home)) == [URL3]
    assert load(home / "missing") == {}


def test_parse_state() -> None:
    raw = {"state": "MERGED", "mergedAt": "2026-10-31T10:00:00Z", "mergeCommit": {"oid": SHA2}}
    s = parse_state(raw)
    assert s.state == "MERGED"
    assert s.merge_sha == SHA2
    assert s.merged_at == 1_793_440_800.0
    assert parse_state({"state": "OPEN", "mergedAt": "", "mergeCommit": None}) == PrState("OPEN")
    odd = parse_state({"state": "CLOSED", "mergedAt": "soon", "mergeCommit": {"oid": "short"}})
    assert odd == PrState("CLOSED")
    with pytest.raises(LedgerError, match="no PR state"):
        parse_state({"state": "DRAFT"})


def test_parse_reverts() -> None:
    log = (
        f"200\x1fRevert x\n\nThis reverts commit {SHA1}, reversing\n\x1e\n"
        f"100\x1fRevert again\n\nThis reverts commit {SHA1}.\nThis reverts commit {SHA2}.\n\x1e\n"
        "junk\x1fno stamp\x1e"
    )
    assert parse_reverts(log) == {SHA1: 100.0, SHA2: 100.0}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


def test_git_reverts_reads_a_real_revert(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f").write_text("1")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "add f")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "revert", "--no-edit", "HEAD")
    assert list(prledger.git_reverts(repo)) == [sha]
    with pytest.raises(LedgerError, match="git log nope failed"):
        prledger.git_reverts(repo, "nope")


def test_gh_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "gh"
    out = json.dumps({"state": "MERGED", "mergedAt": None, "mergeCommit": {"oid": SHA1}})
    fake.write_text(
        "#!/bin/sh\n"
        '[ "$3" = "bad" ] && { echo boom >&2; exit 1; }\n'
        '[ "$3" = "junk" ] && { echo "{"; exit 0; }\n'
        f"echo '{out}'\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda n: str(fake) if n == "gh" else None)
    assert prledger.gh_fetch(URL1) == PrState("MERGED", None, SHA1)
    with pytest.raises(LedgerError, match="boom"):
        prledger.gh_fetch("bad")
    with pytest.raises(LedgerError, match="unreadable"):
        prledger.gh_fetch("junk")
    monkeypatch.setattr("shutil.which", lambda n: None)
    with pytest.raises(LedgerError, match="gh is not installed"):
        prledger.gh_fetch(URL1)
    with pytest.raises(LedgerError, match="git is not installed"):
        prledger.git_reverts(tmp_path)


def test_cli_trust_sync(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _evals(home, 3)
    record_opened(home, URL1, "b", "B3", M, 10.0)
    monkeypatch.setattr(prledger, "gh_fetch", lambda url: PrState("MERGED", 20.0, SHA1))
    monkeypatch.setattr(prledger, "git_reverts", lambda repo, ref: {SHA1: 30.0})
    assert main(["trust", "--sync"]) == 0
    captured = capsys.readouterr()
    assert f"reverted {URL1}" in captured.err
    assert captured.out.startswith("L0 suggest")

    def broken(url: str) -> PrState:
        raise LedgerError("gh is not installed")

    record_opened(home, URL2, "c", "B4", M, 40.0)
    monkeypatch.setattr(prledger, "gh_fetch", broken)
    assert main(["trust", "--sync"]) == 2
    assert "trust: gh is not installed" in capsys.readouterr().err


def test_open_pr_returns_the_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sebastian.evolve import BacklogItem, open_pr

    fake = tmp_path / "gh"
    fake.write_text(f"#!/bin/sh\necho 'Creating pull request'\necho '{URL2}'\n")
    fake.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda n: str(fake) if n == "gh" else None)
    assert open_pr(tmp_path, "b", BacklogItem("B3", "t", "x")) == URL2


def test_cli_evolve_records_the_pr(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sebastian.evolve as evo
    from sebastian.evolve import BacklogItem, EvolveReport

    item = BacklogItem("B3", "Add greeting", "x")

    def fake(*a: object, **kw: object) -> EvolveReport:
        return EvolveReport(item, branch="sebastian/evolve-b3-1", accepted=True, pr_url=URL1)

    monkeypatch.setattr(evo, "evolve_once", fake)
    unsafe = ["--sandbox", "none", "--unsafe-no-sandbox"]
    args = ["evolve", "--repo", str(tmp_path), "--model", M, *unsafe]
    assert main(args, llm_factory=lambda m: None) == 0  # type: ignore[arg-type,return-value]
    pr = load(home)[URL1]
    assert (pr.branch, pr.item, pr.model) == ("sebastian/evolve-b3-1", "B3", M)
