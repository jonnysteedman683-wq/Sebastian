"""Ouroboros (B25): end-to-end cycles on a throwaway git repo with a scripted agent."""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from sebastian.approval import ApprovalGate
from sebastian.cli import main
from sebastian.evolve import (
    DEFAULT_GUARDED,
    EvolveConfig,
    EvolveError,
    ScopedApprover,
    evolve_once,
    load_guarded,
    next_item,
    parse_score,
)
from sebastian.llm import ScriptedLLM
from sebastian.tools import CommandPolicy, Workspace, command_tool, file_tools
from sebastian.types import LLMResponse, ToolCall

PY = sys.executable
BACKLOG = """\
# Backlog

## Queue

- [x] **B1 Done already.** Old.
- [ ] 🔒 **B2 Locked.** Owner only.
- [ ] **B3 Add greeting.** Create `greet.txt` containing hello.
  *Done when:* greet.txt says hello.
- [ ] **B4 Second.** Later.
- [ ] **B5 Inline lock.** Touches a guarded path. 🔒 needs owner-approved.
- [ ] **B6 Continuation lock.** Something.
  *Done when:* x. 🔒 guarded path.
"""
GUARD = """\
        run: |
          guarded='^(\\.github/|secret/)'
"""
# Eval: score is 1/1 if score.txt is absent or says "good", 0/1 if it says "bad".
EVAL = [PY, "-c", (
    "import pathlib;p=pathlib.Path('score.txt');"
    "ok=not p.exists() or p.read_text().strip()=='good';"
    "print(f'score {100 if ok else 0}% ({int(ok)}/1)')"
)]  # fmt: skip
CHECK_GREET = [
    PY,
    "-c",
    "import pathlib,sys;sys.exit(0 if pathlib.Path('greet.txt').exists() else 1)",
]


GIT = shutil.which("git") or "git"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        [GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "seb"
    (r / "docs").mkdir(parents=True)
    (r / ".github/workflows").mkdir(parents=True)
    (r / "docs/BACKLOG.md").write_text(BACKLOG)
    (r / ".github/workflows/guard.yml").write_text(GUARD)
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return r


def write(cid: str, path: str, content: str) -> ToolCall:
    return ToolCall(cid, "write_file", {"path": path, "content": content})


def cfg(tmp_path: Path, **kw: object) -> EvolveConfig:
    base = EvolveConfig(checks=[CHECK_GREET], eval_cmd=EVAL, home=tmp_path / "home")
    for k, v in kw.items():
        setattr(base, k, v)
    return base


def agent(*calls: ToolCall) -> ScriptedLLM:
    return ScriptedLLM(
        [LLMResponse("1. do it")] + [LLMResponse("go", (c,)) for c in calls] + [LLMResponse("done")]
    )


def branches(repo: Path) -> list[str]:
    return git(repo, "branch", "--format=%(refname:short)").split()


# --- policy pieces -----------------------------------------------------------------------


def test_next_item_skips_done_and_locked() -> None:
    item = next_item(BACKLOG)
    assert item is not None
    assert (item.id, item.title) == ("B3", "Add greeting")
    assert "*Done when:*" in item.text
    assert next_item(BACKLOG, "B4") is not None
    assert next_item(BACKLOG, "B2") is None  # locked
    assert next_item(BACKLOG, "B5") is None  # lock marker later in the line
    assert next_item(BACKLOG, "B6") is None  # lock marker on a continuation line
    assert next_item("nothing here") is None


def test_load_guarded(repo: Path, tmp_path: Path) -> None:
    assert load_guarded(repo).pattern == r"^(\.github/|secret/)"
    assert load_guarded(tmp_path).pattern == DEFAULT_GUARDED


def test_parse_score() -> None:
    assert parse_score("x\nscore 80% (4/5)\n") == 0.8
    assert parse_score("no score") is None
    assert parse_score("score 0% (0/0)") is None


def test_scoped_approver(repo: Path) -> None:
    (repo / "link").symlink_to(repo / ".github")
    tools = {t.name: t for t in file_tools(Workspace(repo))}
    run = command_tool(Workspace(repo), CommandPolicy.of(["pytest"]))
    ap = ScopedApprover(repo, load_guarded(repo), frozenset({"pytest"}))
    w = tools["write_file"]
    assert ap.approve(w, write("1", "src/a.py", ""))
    for bad in [".github/x.yml", "./.github/x", "a/../.github/x", "link/x", "secret/k", "../out"]:
        assert not ap.approve(w, write("1", bad, "")), bad
    assert not ap.approve(w, ToolCall("1", "write_file", {"content": "no path"}))
    assert ap.approve(run, ToolCall("2", "run_command", {"command": "pytest -q"}))
    assert not ap.approve(run, ToolCall("2", "run_command", {"command": "rm -rf ."}))
    assert not ap.approve(run, ToolCall("2", "run_command", {"command": "  "}))
    assert not ap.approve(
        tools["read_file"].__class__(  # NETWORK-level tool is never approved
            "net",
            "x",
            tools["read_file"].permission.__class__.NETWORK,
            lambda: "",
            {"type": "object", "properties": {}, "required": []},
        ),
        ToolCall("3", "net", {}),
    )
    assert len(ap.denied) == 10
    assert ApprovalGate(ap).check(tools["read_file"], ToolCall("4", "read_file", {"path": "x"}))


# --- full cycles -------------------------------------------------------------------------


def test_accepted_cycle_commits_on_a_branch(repo: Path, tmp_path: Path) -> None:
    report = evolve_once(repo, agent(write("1", "greet.txt", "hello")), cfg(tmp_path))
    assert report.accepted, report.summary()
    assert report.changed == ["greet.txt"]
    assert report.gates == {
        "non_empty": True,
        "guarded_paths": True,
        "check:" + Path(PY).name + " -c " + CHECK_GREET[2]: True,
        "eval_no_regression": True,
    }
    assert (report.baseline_score, report.candidate_score) == (1.0, 1.0)
    assert report.branch in branches(repo)
    assert git(repo, "show", f"{report.branch}:greet.txt") == "hello"
    assert "B3, evolve" in git(repo, "log", "-1", "--format=%s", report.branch)
    assert git(repo, "log", "-1", "--format=%an", report.branch).strip() == "Sebastian"
    assert git(repo, "worktree", "list").count("\n") == 1  # worktrees cleaned up
    assert "ACCEPTED B3" in report.summary()


def test_regression_is_rejected_and_patch_kept(repo: Path, tmp_path: Path) -> None:
    llm = agent(write("1", "greet.txt", "hello"), write("2", "score.txt", "bad"))
    report = evolve_once(repo, llm, cfg(tmp_path))
    assert not report.accepted
    assert report.gates["eval_no_regression"] is False
    assert (report.baseline_score, report.candidate_score) == (1.0, 0.0)
    assert report.branch not in branches(repo)
    assert report.patch is not None
    assert "score.txt" in report.patch.read_text()
    assert "REJECTED" in report.summary()


def test_failing_check_rejects(repo: Path, tmp_path: Path) -> None:
    report = evolve_once(repo, agent(write("1", "other.txt", "x")), cfg(tmp_path))
    assert not report.accepted
    assert report.reason.startswith("check failed")
    assert "eval_no_regression" not in report.gates  # stops at the first failing gate


def test_no_changes_rejects(repo: Path, tmp_path: Path) -> None:
    report = evolve_once(repo, agent(), cfg(tmp_path))
    assert not report.accepted
    assert report.gates == {"non_empty": False}
    assert report.patch is None


def test_guarded_write_is_denied_and_never_lands(repo: Path, tmp_path: Path) -> None:
    llm = agent(write("1", ".github/workflows/guard.yml", "pwned"), write("2", "greet.txt", "hi"))
    report = evolve_once(repo, llm, cfg(tmp_path))
    assert report.accepted  # the guarded write was denied, the rest is fine
    assert report.changed == ["greet.txt"]
    assert "pwned" not in git(repo, "show", f"{report.branch}:.github/workflows/guard.yml")


def test_guarded_gate_catches_out_of_band_changes(repo: Path, tmp_path: Path) -> None:
    # A check command that edits a guarded file stands in for any side channel.
    sneaky = [PY, "-c", "open('.github/x','w').write('x');open('greet.txt','w').write('h')"]
    report = evolve_once(repo, agent(write("1", "greet.txt", "h")), cfg(tmp_path, checks=[]))
    assert report.accepted
    report = evolve_once(
        repo, agent(write("1", "greet.txt", "h")), cfg(tmp_path, checks=[sneaky], eval_cmd=None)
    )
    assert report.accepted  # checks run after the guarded gate; the edit is not committed
    assert ".github/x" not in git(repo, "ls-tree", "-r", "--name-only", report.branch)


def test_named_item_missing_and_no_evals(repo: Path, tmp_path: Path) -> None:
    report = evolve_once(repo, agent(), cfg(tmp_path, item_id="B2"))
    assert report.item is None
    assert "B2" in report.reason
    assert report.summary() == "evolve: no unlocked backlog item left"
    report = evolve_once(
        repo, agent(write("1", "greet.txt", "x")), cfg(tmp_path, item_id="B4", eval_cmd=None)
    )
    assert report.accepted
    assert report.baseline_score is None


def test_push_and_opener(repo: Path, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run([GIT, "init", "-q", "--bare", str(remote)], check=True)
    git(repo, "remote", "add", "origin", str(remote))
    opened: list[str] = []
    report = evolve_once(
        repo,
        agent(write("1", "greet.txt", "hello")),
        cfg(tmp_path, push=True),
        opener=lambda wt, branch, item: opened.append(f"{branch}:{item.id}"),
    )
    assert report.accepted
    assert opened == [f"{report.branch}:B3"]
    assert report.branch in git(remote, "branch")


def test_not_a_repo(tmp_path: Path) -> None:
    with pytest.raises(EvolveError, match="git rev-parse"):
        evolve_once(tmp_path, agent(), cfg(tmp_path))


def test_cli_evolve(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                    capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    import sebastian.evolve as evo

    real = evo.evolve_once

    def fast(repo: Path, llm: object, config: EvolveConfig, **kw: object) -> object:
        config.checks = [CHECK_GREET]  # keep the CLI test independent of ruff/mypy/pytest
        return real(repo, llm, config, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(evo, "evolve_once", fast)
    llm = agent(write("1", "greet.txt", "hello"))
    code = main(["evolve", "--repo", str(repo), "--no-evals"], llm_factory=lambda m: llm)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "ACCEPTED B3" in out
    llm2 = agent()
    assert main(["evolve", "--repo", str(repo), "--no-evals"], llm_factory=lambda m: llm2) == 1
    assert main(["evolve", "--repo", str(tmp_path)], llm_factory=lambda m: agent()) == 2
