"""watchkit tests against throwaway git repos. No network; repo code is never executed."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from sebastian.watch import inventory as inv
from sebastian.watch.cli import main as watch_main
from sebastian.watch.gitio import Git, GitError
from sebastian.watch.report import (
    CANARY_REASON,
    ReportError,
    canary,
    finalize,
    load_schema,
    scan,
    sign,
    signature_ok,
    verify,
)

ROOT = Path(__file__).parents[1]
SCHEMA = load_schema(ROOT / "schemas" / "repo_change_report.schema.json")
KEY = b"unit-test-key"
GIT = shutil.which("git") or "git"

V1 = {
    "pyproject.toml": '[project]\nname = "sebastian"\nversion = "0.1.0"\ndependencies = []\n',
    "src/sebastian/config.py": 'DEFAULT_MODEL = "claude-sonnet-5-5"\n',
    "src/sebastian/memory.py": "SCHEMA_VERSION = 1\n",
    "src/sebastian/llm/__init__.py": "",
    "src/sebastian/llm/base.py": "",
    "src/sebastian/llm/scripted.py": "",
    "src/sebastian/cli.py": (
        "def p(sub):\n    sub.add_parser('run')\n    sub.add_parser('demo')\n"
        "    sub.add_parser('version')\n"
    ),
    "src/sebastian/tools/builtin.py": (
        "def f(ws):\n"
        "    def read_file(path: str) -> str:\n        return ''\n"
        "    def write_file(path: str) -> str:\n        return ''\n"
        "    return [make_tool(read_file, permission=Permission.READ),\n"
        "            make_tool(write_file, permission=Permission.WRITE)]\n"
    ),
    "README.md": "x\n",
}


def git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(
        [GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def commit(repo: Path, files: Mapping[str, str | None], msg: str) -> str:
    for path, content in files.items():
        target = repo / path
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(r, V1, "v1")
    return r


def v2(repo: Path) -> str:
    """Adds a NETWORK tool, raises write_file to EXEC, swaps model, drops 'demo', bumps memory."""
    return commit(
        repo,
        {
            "pyproject.toml": V1["pyproject.toml"]
            .replace("0.1.0", "0.2.0")
            .replace("dependencies = []", 'dependencies = ["httpx2"]'),
            "src/sebastian/config.py": 'DEFAULT_MODEL = "claude-opus-5-5"\n',
            "src/sebastian/memory.py": "SCHEMA_VERSION = 2\n",
            "src/sebastian/llm/anthropic.py": "",
            "src/sebastian/cli.py": (
                "def p(sub):\n    sub.add_parser('run')\n    sub.add_parser('version')\n"
            ),
            "src/sebastian/tools/builtin.py": V1["src/sebastian/tools/builtin.py"].replace(
                "Permission.WRITE", "Permission.EXEC"
            ),
            "src/sebastian/tools/web.py": (
                "def g():\n    def fetch(url: str) -> str:\n        return ''\n"
                "    return make_tool(fetch, permission=Permission.NETWORK, name='fetch_url')\n"
            ),
        },
        "v2",
    )


# --- gitio -----------------------------------------------------------------


def test_git_basics(repo: Path) -> None:
    g = Git(repo)
    head = g.resolve("HEAD")
    assert g.exists(head)
    assert not g.exists("deadbeef")
    assert g.show(head, "README.md") == "x\n"
    assert g.show(head, "missing") is None
    assert [c.subject for c in g.commits(None, head)] == ["v1"]
    with pytest.raises(GitError):
        g.resolve("nope")


def test_git_missing_executable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("sebastian.watch.gitio.shutil.which", lambda _: None)
    with pytest.raises(GitError, match="not found"):
        Git(tmp_path)


# --- inventory -------------------------------------------------------------


def test_inventory_is_static_and_accurate(repo: Path) -> None:
    g = Git(repo)
    i = inv.build(g, g.resolve("HEAD"))
    assert i.tools == {"read_file": "READ", "write_file": "WRITE"}
    assert i.cli_commands == {"run", "demo", "version"}
    assert i.llm_backends == {"scripted"}
    assert (i.default_model, i.version, i.memory_schema) == ("claude-sonnet-5-5", "0.1.0", 1)
    assert inv.build(g, None) == inv.Inventory()


def test_inventory_survives_broken_files(repo: Path) -> None:
    sha = commit(
        repo,
        {
            "src/sebastian/tools/bad.py": "def (:\n",
            "pyproject.toml": "not = [toml",
            "src/sebastian/config.py": "DEFAULT_MODEL: str = 'annotated'\n",
        },
        "broken",
    )
    i = inv.build(Git(repo), sha)
    assert i.tools  # good files still parsed
    assert i.version is None
    assert i.default_model == "annotated"


def test_inventory_never_executes_repo_code(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    sha = commit(
        repo,
        {"src/sebastian/tools/evil.py": f"open({str(marker)!r}, 'w').write('x')\n"},
        "evil",
    )
    inv.build(Git(repo), sha)
    assert not marker.exists()


def test_diff_and_risk_signals(repo: Path) -> None:
    g = Git(repo)
    old_sha = g.resolve("HEAD")
    new_sha = v2(repo)
    old, new = inv.build(g, old_sha), inv.build(g, new_sha)
    d = inv.diff(old, new)
    assert "tool: fetch_url (NETWORK)" in d["added"]
    assert "backend: anthropic" in d["added"]
    assert "cli: demo" in d["removed"]
    assert "tool: write_file permission WRITE -> EXEC" in d["changed"]
    assert "default_model: claude-sonnet-5-5 -> claude-opus-5-5" in d["changed"]
    assert "memory_schema: 1 -> 2" in d["changed"]
    reasons = dict((r, p) for p, r in inv.risk_signals(old, new))
    assert reasons["new tool: fetch_url (NETWORK)"] == 4
    assert reasons["permission raised from WRITE: write_file (EXEC)"] == 4
    assert reasons["breaking: removed cli demo"] == 2
    assert reasons["breaking: memory schema version changed"] == 2
    assert reasons["dependencies changed"] == 1
    assert reasons["default model changed to claude-opus-5-5"] == 1
    removed_tool = commit(repo, {"src/sebastian/tools/web.py": None}, "drop web")
    sig = inv.risk_signals(new, inv.build(g, removed_tool))
    assert (2, "breaking: removed tool fetch_url") in sig


# --- scan / finalize -------------------------------------------------------


def draft(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "summary": "Change summary.",
        "ci_status": "success",
        "changes": [
            {
                "category": "DOCS",
                "title": "Docs",
                "description": "d",
                "impact": "none",
                "evidence": [{"sha": "abcdef1", "path": "README.md"}],
                "permission": "n/a",
                "flags": [],
            }
        ],
    }
    base.update(overrides)
    return base


def test_scan_and_finalize_high_risk(repo: Path) -> None:
    g = Git(repo)
    start = g.resolve("HEAD")
    v2(repo)
    facts = scan(g, start, "HEAD")
    assert facts["version"] == {"before": "0.1.0", "after": "0.2.0"}
    assert "src/sebastian/tools/web.py" in facts["sensitive_files"]
    assert not facts["history_rewritten"]
    report = finalize(
        draft(), facts, seq=7, key=KEY, schema=SCHEMA, generated_at="2026-10-18T10:00:00Z"
    )
    assert report["risk_score"] == 10  # capped
    assert report["escalate"] is True
    assert report["priority"] == "HIGH"
    assert "new tool: fetch_url (NETWORK)" in report["escalation_reasons"]
    assert "risk_score 10 >= 6" in report["escalation_reasons"]
    assert verify(report, key=KEY, schema=SCHEMA, expected_seq=7, expected_from_sha=start) == []


def test_finalize_low_priority_docs_only(repo: Path) -> None:
    g = Git(repo)
    start = g.resolve("HEAD")
    commit(repo, {"README.md": "y\n"}, "docs")
    report = finalize(draft(), scan(g, start, "HEAD"), seq=1, key=KEY, schema=SCHEMA)
    assert (report["priority"], report["risk_score"], report["escalate"]) == ("LOW", 0, False)


def test_finalize_never_lowers_watcher_judgement(repo: Path) -> None:
    g = Git(repo)
    start = g.resolve("HEAD")
    commit(repo, {"README.md": "y\n"}, "docs")
    facts = scan(g, start, "HEAD")
    r = finalize(
        draft(priority="NORMAL", risk_score=3, escalate=True), facts, seq=1, key=KEY, schema=SCHEMA
    )
    assert (r["priority"], r["risk_score"], r["escalate"]) == ("NORMAL", 3, True)
    assert r["escalation_reasons"] == ["escalated by watcher judgement"]
    r2 = finalize(
        draft(
            changes=[{**draft()["changes"][0], "category": "NEW_FEATURE"}], summary_markdown="## x"
        ),
        facts,
        seq=2,
        key=KEY,
        schema=SCHEMA,
    )
    assert r2["priority"] == "NORMAL"
    assert r2["summary_markdown"] == "## x"
    r3 = finalize(draft(ci_status="failure"), facts, seq=3, key=KEY, schema=SCHEMA)
    assert r3["priority"] == "HIGH"


def test_finalize_rejects_overreach_and_bad_drafts(repo: Path) -> None:
    g = Git(repo)
    facts = scan(g, None, "HEAD")
    with pytest.raises(ReportError, match="may not set"):
        finalize(draft(capability_inventory={}), facts, seq=1, key=KEY, schema=SCHEMA)
    bad = draft(changes=[{**draft()["changes"][0], "evidence": []}])
    with pytest.raises(ReportError, match="fails schema"):
        finalize(bad, facts, seq=1, key=KEY, schema=SCHEMA)
    with pytest.raises(ReportError, match="key is empty"):
        finalize(draft(), facts, seq=1, key=b"", schema=SCHEMA)


def test_first_run_has_null_from_sha(repo: Path) -> None:
    facts = scan(Git(repo), None, "HEAD")
    assert facts["range"]["from_sha"] is None
    assert "README.md" in facts["changed_files"]
    assert facts["commits"][0]["subject"] == "v1"


def test_history_rewrite_detected_and_escalated(repo: Path) -> None:
    g = Git(repo)
    base = g.resolve("HEAD")
    reported = commit(repo, {"README.md": "reported\n"}, "reported")
    git(repo, "reset", "-q", "--hard", base)
    commit(repo, {"README.md": "rewritten\n"}, "rewritten")
    facts = scan(g, reported, "HEAD")
    assert facts["history_rewritten"] is True
    r = finalize(draft(), facts, seq=1, key=KEY, schema=SCHEMA)
    assert r["escalate"]
    assert r["risk_score"] >= 3
    assert any("history rewritten" in x for x in r["escalation_reasons"])


# --- canary / verify / signing ---------------------------------------------


def test_canary(repo: Path) -> None:
    c = canary(
        Git(repo), seq=9, key=KEY, schema=SCHEMA, ref="HEAD", generated_at="2026-10-19T22:15:00Z"
    )
    assert c["report_type"] == "canary"
    assert c["escalation_reasons"] == [CANARY_REASON]
    assert verify(c, key=KEY, schema=SCHEMA, expected_seq=9, expected_from_sha="ignored") == []


def test_verify_catches_tampering_seq_and_gaps(repo: Path) -> None:
    g = Git(repo)
    report = finalize(draft(), scan(g, None, "HEAD"), seq=5, key=KEY, schema=SCHEMA)
    assert verify(report, key=b"other", schema=SCHEMA)[0].startswith("signature")
    tampered = {**report, "summary": "forged"}
    assert any("signature" in p for p in verify(tampered, key=KEY, schema=SCHEMA))
    probs = verify(report, key=KEY, schema=SCHEMA, expected_seq=6, expected_from_sha="abc1234")
    assert any(p.startswith("seq") for p in probs)
    assert any(p.startswith("range.from_sha") for p in probs)


def test_signature_matches_published_test_vector() -> None:
    example = json.loads((ROOT / "schemas" / "examples" / "report.json").read_text())
    resigned = sign(dict(example), b"TEST-VECTOR-KEY-NOT-A-SECRET")
    assert resigned["signature"] == example["signature"]
    assert signature_ok(example, b"TEST-VECTOR-KEY-NOT-A-SECRET")
    assert not signature_ok({}, KEY)
    assert not signature_ok(example, b"")


# --- CLI -------------------------------------------------------------------


def run_cli(args: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = watch_main(args)
    out, err = capsys.readouterr()
    return code, out, err


def test_cli_full_flow(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    schema = str(ROOT / "schemas" / "repo_change_report.schema.json")
    common = ["--repo", str(repo), "--schema", schema]
    start = Git(repo).resolve("HEAD")
    v2(repo)

    code, out, _ = run_cli([*common, "scan", "--from", start, "--to", "HEAD"], capsys)
    assert code == 0
    (tmp_path / "facts.json").write_text(out)
    (tmp_path / "draft.json").write_text(json.dumps(draft()))

    code, _, err = run_cli(
        [
            *common,
            "finalize",
            "--draft",
            str(tmp_path / "draft.json"),
            "--facts",
            str(tmp_path / "facts.json"),
            "--seq",
            "1",
        ],
        capsys,
    )
    assert code == 2
    assert "WATCHER_HMAC_KEY" in err

    monkeypatch.setenv("WATCHER_HMAC_KEY", "cli-key")
    code, out, _ = run_cli(
        [
            *common,
            "finalize",
            "--draft",
            str(tmp_path / "draft.json"),
            "--facts",
            str(tmp_path / "facts.json"),
            "--seq",
            "1",
        ],
        capsys,
    )
    assert code == 0
    assert "cli-key" not in out  # key never printed
    (tmp_path / "report.json").write_text(out)

    code, out, _ = run_cli(
        [
            *common,
            "verify",
            str(tmp_path / "report.json"),
            "--expect-seq",
            "1",
            "--expect-from",
            start,
        ],
        capsys,
    )
    assert code == 0
    assert json.loads(out)["accepted"] is True

    code, out, _ = run_cli(
        [*common, "verify", str(tmp_path / "report.json"), "--expect-seq", "2"], capsys
    )
    assert code == 1
    assert json.loads(out)["accepted"] is False

    code, out, _ = run_cli([*common, "canary", "--seq", "2", "--ref", "HEAD"], capsys)
    assert code == 0
    assert json.loads(out)["report_type"] == "canary"

    (tmp_path / "bad.json").write_text(json.dumps({**draft(), "seq": 1}))
    code, _, err = run_cli(
        [
            *common,
            "finalize",
            "--draft",
            str(tmp_path / "bad.json"),
            "--facts",
            str(tmp_path / "facts.json"),
            "--seq",
            "1",
        ],
        capsys,
    )
    assert code == 1
    assert "may not set" in err


def test_cli_environment_errors(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run_cli(["--repo", str(repo), "scan", "--to", "no-such-ref"], capsys)
    assert code == 2
    assert "error" in err
    code, _, _ = run_cli(
        ["--repo", str(repo), "--schema", "/nonexistent.json", "canary", "--seq", "1"], capsys
    )
    assert code == 2


def test_cli_default_schema_path(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "schemas").mkdir()
    (repo / "schemas" / "repo_change_report.schema.json").write_text(json.dumps(SCHEMA))
    monkeypatch.setenv("WATCHER_HMAC_KEY", "k")
    code, out, _ = run_cli(["--repo", str(repo), "canary", "--seq", "1", "--ref", "HEAD"], capsys)
    assert code == 0
    assert json.loads(out)["seq"] == 1


def test_static_inventory_of_real_repo_matches_runtime() -> None:
    """AST parsing of this repo at HEAD must agree with the published example inventory,
    which test_report_schema checks against the live runtime."""
    example = json.loads((ROOT / "schemas" / "examples" / "report.json").read_text())
    real = inv.build(Git(ROOT), "HEAD").to_schema()
    published = example["capability_inventory"]
    for key in ("tools", "cli_commands", "llm_backends"):
        assert real[key] == sorted(published[key]), key
    assert real["default_model"] == published["default_model"]
