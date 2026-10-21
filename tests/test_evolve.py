"""Ouroboros (B25): end-to-end cycles on a throwaway git repo with a scripted agent."""

from __future__ import annotations

import io
import re
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
    # CI hosts may lack bubblewrap; P1-02 makes that an error unless explicitly opted out.
    base = EvolveConfig(
        checks=[CHECK_GREET],
        eval_cmd=EVAL,
        home=tmp_path / "home",
        sandbox="none",
        unsafe_no_sandbox=True,
        metrics_cmd=None,  # P1-04 metrics get their own tests below
    )
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
        "git_files": True,
        "tests_kept": True,
        "src_layout": True,
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
        config.metrics_cmd = None
        return real(repo, llm, config, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(evo, "evolve_once", fast)
    llm = agent(write("1", "greet.txt", "hello"))
    unsafe = ["--sandbox", "none", "--unsafe-no-sandbox"]
    code = main(["evolve", "--repo", str(repo), "--no-evals", *unsafe], llm_factory=lambda m: llm)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "ACCEPTED B3" in out
    llm2 = agent()
    args = ["evolve", "--repo", str(repo), "--no-evals", *unsafe]
    assert main(args, llm_factory=lambda m: llm2) == 1
    assert main(["evolve", "--repo", str(tmp_path), *unsafe], llm_factory=lambda m: agent()) == 2
    # P1-02: --sandbox none alone is refused; it needs the explicit confirmation flag.
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: None if n == "bwrap" else GIT)
    code = main(["evolve", "--repo", str(repo), "--no-evals", "--sandbox", "none"],
                llm_factory=lambda m: agent())  # fmt: skip
    assert code == 2
    assert "--unsafe-no-sandbox" in capsys.readouterr().err


def test_open_pr_uses_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sebastian.evolve import BacklogItem, open_pr

    log = tmp_path / "gh.log"
    fake = tmp_path / "gh"
    fake.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\n[ "$4" = "fail" ] && exit 1\nexit 0\n'
    )
    fake.chmod(0o755)
    real = shutil.which
    monkeypatch.setattr("shutil.which", lambda n: str(fake) if n == "gh" else real(n))
    item = BacklogItem("B3", "Add greeting", "- [ ] **B3 Add greeting.**")
    open_pr(tmp_path, "sebastian/evolve-b3-1", item)
    args = log.read_text().splitlines()
    assert args[:4] == ["pr", "create", "--head", "sebastian/evolve-b3-1"]
    assert "feat: Add greeting (B3)" in args
    with pytest.raises(EvolveError, match="gh pr create failed"):
        open_pr(tmp_path, "fail", item)
    monkeypatch.setattr("shutil.which", lambda n: None)
    with pytest.raises(EvolveError, match="gh is not installed"):
        open_pr(tmp_path, "x", item)
    with pytest.raises(EvolveError, match="git is not installed"):
        evolve_once(tmp_path, agent(), cfg(tmp_path))


# --- sandboxed gates (B27) ----------------------------------------------------------------

FAKE_BWRAP = f"""#!{PY}
# Stand-in for bubblewrap: logs its argv, then runs the wrapped command in the bound dir.
import os, subprocess, sys
args = sys.argv[1:]
with open(os.environ.get("FAKE_BWRAP_LOG", "/dev/null"), "a") as fh:
    fh.write(" ".join(args) + "\\n")
if args[:1] == ["--unshare-all"] and "--" not in args:
    sys.exit(0)
cut = args.index("--")
opts, cmd = args[:cut], args[cut + 1:]
cwd = opts[opts.index("--bind") + 1] if "--bind" in opts else None
sys.exit(subprocess.run(cmd, cwd=cwd).returncode)
"""


@pytest.fixture
def fake_bwrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "bin" / "bwrap"
    fake.parent.mkdir()
    log = tmp_path / "bwrap.log"
    # Log path baked in: run_command scrubs the environment it hands to bwrap.
    fake.write_text(
        FAKE_BWRAP.replace('os.environ.get("FAKE_BWRAP_LOG", "/dev/null")', repr(str(log)))
    )
    fake.chmod(0o755)
    real = shutil.which
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: str(fake) if n == "bwrap" else real(n))
    return log


def test_resolve_sandbox_modes(fake_bwrap: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from sebastian.evolve import resolve_sandbox

    assert resolve_sandbox("none", unsafe=True) is None
    assert resolve_sandbox("auto") is not None
    assert resolve_sandbox("bwrap") is not None
    with pytest.raises(EvolveError, match="unknown sandbox"):
        resolve_sandbox("docker")
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    with pytest.raises(EvolveError, match="missing or blocked"):
        resolve_sandbox("bwrap")


def test_auto_without_bwrap_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1-02: agent-written code never runs unsandboxed unless explicitly confirmed."""
    from sebastian.evolve import resolve_sandbox

    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    with pytest.raises(EvolveError, match="--unsafe-no-sandbox"):
        resolve_sandbox("auto")
    with pytest.raises(EvolveError, match="--unsafe-no-sandbox"):
        resolve_sandbox("none")
    assert resolve_sandbox("auto", unsafe=True) is None
    assert resolve_sandbox("none", unsafe=True) is None


def test_auto_without_bwrap_fails_before_any_work(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: None if n == "bwrap" else GIT)
    config = cfg(tmp_path, sandbox="auto", unsafe_no_sandbox=False)
    with pytest.raises(EvolveError, match="--unsafe-no-sandbox"):
        evolve_once(repo, agent(write("1", "greet.txt", "x")), config)
    assert git(repo, "worktree", "list").count("\n") == 1


FAKE_SECRETS = {
    "ANTHROPIC_API_KEY": "sk-ant-test-FAKE0000",
    "OPENAI_API_KEY": "sk-test-FAKE0000",
    "GH_TOKEN": "ghp_FAKE0000000000000000000000000000",
}
DUMP_ENV = [PY, "-c", "import json, os; open('env.json', 'w').write(json.dumps(dict(os.environ)))"]


@pytest.mark.parametrize("sandboxed", [False, True])
def test_gate_env_has_no_secrets(
    sandboxed: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_bwrap: Path
) -> None:
    """P1-02: a gate command's environment never carries the parent's secrets."""
    import json

    from sebastian.evolve import _run

    for k, v in FAKE_SECRETS.items():
        monkeypatch.setenv(k, v)
    work = tmp_path / "wt"
    work.mkdir()
    ok, out = _run(DUMP_ENV, work, shutil.which("bwrap") if sandboxed else None)
    assert ok, out
    env = json.loads((work / "env.json").read_text())
    for k in FAKE_SECRETS:
        assert k not in env
    assert not any(v in json.dumps(env) for v in FAKE_SECRETS.values())
    if not sandboxed:
        assert set(env) <= {"PATH", "LANG", "LC_ALL", "HOME", "PYTHONPATH"} | {"LC_CTYPE"}
        assert env["HOME"] == str(work)
        assert env["PYTHONPATH"] == str(work / "src")


def test_report_says_no_secrets_in_gate_env(repo: Path, tmp_path: Path) -> None:
    report = evolve_once(repo, agent(write("1", "greet.txt", "hello")), cfg(tmp_path))
    assert report.accepted, report.summary()
    assert "no secrets in gate env" in report.summary()


def test_bwrap_usable_probe(fake_bwrap: Path, tmp_path: Path) -> None:
    from sebastian.tools.builtin import bwrap_usable

    assert bwrap_usable(shutil.which("bwrap") or "")
    assert not bwrap_usable(str(tmp_path / "missing"))


def test_gates_run_inside_the_sandbox(repo: Path, tmp_path: Path, fake_bwrap: Path) -> None:
    report = evolve_once(
        repo, agent(write("1", "greet.txt", "hello")), cfg(tmp_path, sandbox="bwrap")
    )
    assert report.accepted, report.summary()
    assert report.sandbox == "bwrap"
    assert "gate sandbox: bwrap" in report.summary()
    log = fake_bwrap.read_text().splitlines()
    wrapped = [line for line in log if line.startswith("--unshare-all --die-with-parent")]
    assert len(wrapped) == 3  # one check + baseline eval + candidate eval
    for line in wrapped:
        assert line.startswith("--unshare-all --die-with-parent --new-session")
        assert " /workspace --chdir /workspace --clearenv " in line
        assert "--setenv HOME /workspace" in line
        assert "--setenv PYTHONPATH /workspace/src" in line
        assert f"--ro-bind-try {sys.prefix} {sys.prefix}" in line
        assert f" -- {PY} " in line


def test_unsandboxed_when_asked(repo: Path, tmp_path: Path, fake_bwrap: Path) -> None:
    report = evolve_once(repo, agent(write("1", "greet.txt", "x")), cfg(tmp_path, sandbox="none"))
    assert report.accepted
    assert report.sandbox == "none"
    assert "--die-with-parent" not in (fake_bwrap.read_text() if fake_bwrap.exists() else "")


def test_required_sandbox_fails_before_any_work(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda n, *a, **k: None if n == "bwrap" else GIT)
    with pytest.raises(EvolveError, match="required"):
        evolve_once(repo, agent(write("1", "greet.txt", "x")), cfg(tmp_path, sandbox="bwrap"))
    assert git(repo, "worktree", "list").count("\n") == 1


def test_agent_commands_share_the_gate_sandbox(
    repo: Path, tmp_path: Path, fake_bwrap: Path
) -> None:
    run = ToolCall("9", "run_command", {"command": "pytest --version"})
    report = evolve_once(
        repo, agent(run, write("1", "greet.txt", "hello")), cfg(tmp_path, sandbox="bwrap")
    )
    assert report.accepted, report.summary()
    wrapped = [
        line for line in fake_bwrap.read_text().splitlines()
        if line.startswith("--unshare-all --die-with-parent") and "pytest --version" in line
    ]  # fmt: skip
    assert len(wrapped) == 1
    assert f"--ro-bind-try {sys.prefix} {sys.prefix}" in wrapped[0]
    assert "--setenv PYTHONPATH src" in wrapped[0]


def test_agent_commands_unsandboxed_when_gates_are(
    repo: Path, tmp_path: Path, fake_bwrap: Path
) -> None:
    run = ToolCall("9", "run_command", {"command": "pytest --version"})
    report = evolve_once(
        repo, agent(run, write("1", "greet.txt", "hello")), cfg(tmp_path, sandbox="none")
    )
    assert report.accepted
    assert not fake_bwrap.exists() or "pytest --version" not in fake_bwrap.read_text()


def test_budget_stopped_agent_is_resumed(repo: Path, tmp_path: Path) -> None:
    from sebastian import Budget

    def llm() -> ScriptedLLM:  # plan + 1 step fits the budget; the greet write needs a resume
        return agent(write("1", "notes.txt", "started"), write("2", "greet.txt", "hello"))

    tight = Budget(max_steps=2)
    report = evolve_once(repo, llm(), cfg(tmp_path, budget=tight))
    assert report.accepted, report.summary()
    assert report.resumes == 1
    assert report.run_stop == "completed"
    assert sorted(report.changed) == ["greet.txt", "notes.txt"]
    assert "agent resumed: 1x" in report.summary()

    report = evolve_once(repo, llm(), cfg(tmp_path, budget=tight, max_resumes=0))
    assert not report.accepted
    assert report.resumes == 0
    assert report.run_stop == "budget_exhausted"
    assert report.reason.startswith("check failed")


# --- P1-03: the safety TCB is guarded; tests are never deleted or renamed ------------------

REAL_ROOT = Path(__file__).resolve().parents[1]
TCB = [
    "src/sebastian/approval.py",
    "src/sebastian/agent.py",
    "src/sebastian/budget.py",
    "src/sebastian/evolve.py",
    "src/sebastian/gauntlet.py",
    "src/sebastian/checkpoint.py",
    "src/sebastian/session.py",
    "src/sebastian/trace.py",
    "src/sebastian/tools/builtin.py",
    "src/sebastian/llm/base.py",
    "src/sebastian/evals/cases.py",
    "src/sebastian/watch/report.py",
    "pyproject.toml",
    "scripts/resign_examples.py",
    ".github/workflows/guard.yml",
    "AGENTS.md",
    "CLAUDE.md",
]
NOT_TCB = [
    "src/sebastian/memory.py",
    "src/sebastian/prompts.py",
    "tests/test_x.py",
    "docs/BACKLOG.md",
]


def test_real_guard_regex_matches_default() -> None:
    """guard.yml and DEFAULT_GUARDED are one guarded set (the fallback never drifts)."""
    assert load_guarded(REAL_ROOT).pattern == DEFAULT_GUARDED


@pytest.mark.parametrize("path", TCB)
def test_tcb_paths_are_guarded(path: str) -> None:
    assert re.compile(DEFAULT_GUARDED).search(path), path


@pytest.mark.parametrize("path", NOT_TCB)
def test_ordinary_paths_stay_writable(path: str) -> None:
    assert not re.compile(DEFAULT_GUARDED).search(path), path


def test_scoped_approver_denies_tcb_writes(tmp_path: Path) -> None:
    tools = {t.name: t for t in file_tools(Workspace(tmp_path))}
    ap = ScopedApprover(tmp_path, re.compile(DEFAULT_GUARDED), frozenset())
    for path in ("src/sebastian/approval.py", "pyproject.toml", "src/sebastian/tools/x.py"):
        assert not ap.approve(tools["write_file"], write("1", path, "pwned")), path
    assert ap.approve(tools["write_file"], write("1", "src/sebastian/memory.py", "ok"))


def test_codeowners_covers_the_guarded_set() -> None:
    lines = (REAL_ROOT / ".github/CODEOWNERS").read_text().splitlines()
    owned = {ln.split()[0]: ln.split()[1:] for ln in lines if ln.strip() and not ln.startswith("#")}
    for owners in owned.values():
        assert owners == ["@jonnysteedman683-wq"]
    for path in TCB:
        assert any(
            ("/" + path).startswith(pat) if pat.endswith("/") else "/" + path == pat
            for pat in owned
        ), f"{path} has no CODEOWNERS entry"


def _with_tests(repo: Path) -> None:
    (repo / "tests").mkdir()
    (repo / "tests/test_keep.py").write_text("def test_keep():\n    assert True\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "tests")


@pytest.mark.parametrize(
    "command", ["rm tests/test_keep.py", "mv tests/test_keep.py tests/test_moved.py"]
)
def test_deleting_or_renaming_a_test_is_rejected(repo: Path, tmp_path: Path, command: str) -> None:
    _with_tests(repo)
    run = ToolCall("9", "run_command", {"command": command})
    config = cfg(tmp_path, agent_commands=frozenset({"rm", "mv"}), eval_cmd=None)
    report = evolve_once(repo, agent(run, write("1", "greet.txt", "hello")), config)
    assert not report.accepted, report.summary()
    assert report.gates.get("tests_kept") is False
    assert "tests/test_keep.py" in report.reason


def test_adding_and_editing_tests_is_fine(repo: Path, tmp_path: Path) -> None:
    _with_tests(repo)
    llm = agent(
        write("1", "tests/test_keep.py", "def test_keep():\n    assert 1\n"),
        write("2", "tests/test_new.py", "def test_new():\n    assert 1\n"),
        write("3", "greet.txt", "hello"),
    )
    report = evolve_once(repo, llm, cfg(tmp_path, eval_cmd=None))
    assert report.accepted, report.summary()
    assert report.gates["tests_kept"] is True


def test_command_side_channel_on_guarded_path_is_rejected(repo: Path, tmp_path: Path) -> None:
    run = ToolCall("9", "run_command", {"command": "rm .github/workflows/guard.yml"})
    config = cfg(tmp_path, agent_commands=frozenset({"rm"}), eval_cmd=None)
    report = evolve_once(repo, agent(run, write("1", "greet.txt", "hello")), config)
    assert not report.accepted
    assert report.gates["guarded_paths"] is False
    assert ".github/workflows/guard.yml" in report.reason


# --- P1-04: gates are independent of the candidate --------------------------------------

# A "test suite" for the fixture repo: tests/check.py decides what impl.txt must say.
CHECK_IMPL = [PY, "tests/check.py"]
CHECK_SRC = (
    "import pathlib,sys\nsys.exit(0 if pathlib.Path('impl.txt').read_text() == 'a' else 1)\n"
)
# Eval: 1/1 when impl.txt matches evals/expect.txt.
EVAL_IMPL = [PY, "-c", (
    "import pathlib;ok=pathlib.Path('impl.txt').read_text()==pathlib.Path('evals/expect.txt')"
    ".read_text();print(f'score {100 if ok else 0}% ({int(ok)}/1)')"
)]  # fmt: skip
# Metrics: one test per "def test_" in tests/, coverage from cov.txt.
METRICS = [PY, "-c", (
    "import pathlib;n=sum(p.read_text().count('def test_') for p in pathlib.Path('tests')"
    ".glob('*.py'));c=pathlib.Path('cov.txt').read_text().strip();"
    "print(f'TOTAL 10 1 2 1 {c}%');print(f'{n} passed in 0.1s')"
)]  # fmt: skip


def _impl_repo(repo: Path) -> None:
    (repo / "tests").mkdir()
    (repo / "evals").mkdir()
    (repo / "impl.txt").write_text("a")
    (repo / "cov.txt").write_text("90")
    (repo / "tests/check.py").write_text(CHECK_SRC)
    (repo / "tests/test_impl.py").write_text("def test_a():\n    pass\n\ndef test_b():\n    pass\n")
    (repo / "evals/expect.txt").write_text("a")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "impl")


def icfg(tmp_path: Path, **kw: object) -> EvolveConfig:
    return cfg(tmp_path, **({"checks": [CHECK_IMPL], "eval_cmd": EVAL_IMPL} | kw))


def test_honest_candidate_passes_independent_gates(repo: Path, tmp_path: Path) -> None:
    _impl_repo(repo)
    llm = agent(
        write("1", "greet.txt", "hello"),
        write("2", "tests/test_more.py", "def test_c():\n    pass\n"),
    )
    report = evolve_once(repo, llm, icfg(tmp_path, metrics_cmd=METRICS))
    assert report.accepted, report.summary()
    assert report.gates["no_test_or_coverage_drop"] is True
    assert "own:" + Path(PY).name + " tests/check.py" in report.gates  # candidate touched tests/
    assert report.baseline_metrics == (2, 90.0)
    assert report.candidate_metrics == (3, 90.0)
    assert "tests/coverage: baseline=(2, 90.0) candidate=(3, 90.0)" in report.summary()


def test_baseline_gate_config_wins_over_the_candidates(repo: Path, tmp_path: Path) -> None:
    _impl_repo(repo)
    (repo / "pyproject.toml").write_text("strict = true\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "config")
    probe = [
        PY,
        "-c",
        "import pathlib,sys;"
        "sys.exit(pathlib.Path('pyproject.toml').read_text()!='strict = true\\n')",
    ]
    llm = agent(write("1", "pyproject.toml", "strict = false\n"), write("2", "greet.txt", "x"))
    report = evolve_once(repo, llm, icfg(tmp_path, checks=[probe], eval_cmd=None))
    # pyproject.toml is not guarded in this fixture; the overlay still restores the baseline's.
    assert report.accepted, report.summary()


def test_unreadable_baseline_metrics(repo: Path, tmp_path: Path) -> None:
    _impl_repo(repo)
    (repo / "cov.txt").unlink()  # the baseline's metrics command crashes
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "no cov")

    def llm() -> ScriptedLLM:
        return agent(write("1", "cov.txt", "95"))

    report = evolve_once(repo, llm(), icfg(tmp_path, metrics_cmd=METRICS))
    assert not report.accepted
    assert "baseline unreadable" in report.reason
    report = evolve_once(
        repo, llm(), icfg(tmp_path, metrics_cmd=METRICS, allow_missing_baseline=True)
    )
    assert report.accepted, report.summary()


def test_candidate_cannot_weaken_its_tests(repo: Path, tmp_path: Path) -> None:
    """Breaking impl.txt and loosening tests/check.py to match: baseline tests still run."""
    _impl_repo(repo)
    loose = "import sys\nsys.exit(0)\n"
    llm = agent(write("1", "impl.txt", "b"), write("2", "tests/check.py", loose))
    report = evolve_once(repo, llm, icfg(tmp_path, eval_cmd=None))
    assert not report.accepted
    assert report.gates["check:" + Path(PY).name + " tests/check.py"] is False


def test_candidate_cannot_inflate_its_eval(repo: Path, tmp_path: Path) -> None:
    """Changing impl.txt and evals/expect.txt together: the baseline's evals score it."""
    _impl_repo(repo)
    llm = agent(write("1", "impl.txt", "b"), write("2", "evals/expect.txt", "b"))
    report = evolve_once(repo, llm, icfg(tmp_path, checks=[]))
    assert not report.accepted
    assert (report.baseline_score, report.candidate_score) == (1.0, 0.0)
    assert report.gates["eval_no_regression"] is False


@pytest.mark.parametrize("name", ["pyproject.toml", "pytest.ini", "conftest.py", "setup.cfg"])
def test_candidate_gate_config_is_ignored(repo: Path, tmp_path: Path, name: str) -> None:
    """A config file the candidate adds or edits never reaches the independent gates."""
    _impl_repo(repo)
    probe = [PY, "-c", f"import pathlib,sys;sys.exit(1 if pathlib.Path({name!r}).exists() else 0)"]
    llm = agent(write("1", name, "[pytest]\naddopts = -k nothing\n"), write("2", "greet.txt", "x"))
    report = evolve_once(repo, llm, icfg(tmp_path, checks=[probe], eval_cmd=None))
    assert report.accepted, report.summary()


def test_new_top_level_module_in_src_is_rejected(repo: Path, tmp_path: Path) -> None:
    """src/ is on PYTHONPATH: a new src/pytest or src/sitecustomize.py would hijack the gates."""
    (repo / "src/pkg").mkdir(parents=True)
    (repo / "src/pkg/__init__.py").write_text("")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "src")
    for path in ("src/sitecustomize.py", "src/pytest/__init__.py"):
        report = evolve_once(repo, agent(write("1", path, "import os\n")), cfg(tmp_path))
        assert not report.accepted, path
        assert report.gates["src_layout"] is False
        assert path.split("/")[1] in report.reason
    report = evolve_once(repo, agent(write("1", "src/pkg/new.py", "x = 1\n"),
                                     write("2", "greet.txt", "hi")), cfg(tmp_path))  # fmt: skip
    assert report.accepted, report.summary()


def test_missing_baseline_score_fails_closed(repo: Path, tmp_path: Path) -> None:
    _impl_repo(repo)
    (repo / "evals/expect.txt").unlink()  # the baseline eval crashes: no score line
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "no expect")
    fix = write("2", "evals/expect.txt", "a")  # the candidate "repairs" it in its own tree
    report = evolve_once(repo, agent(write("1", "greet.txt", "hi"), fix), icfg(tmp_path, checks=[]))
    assert not report.accepted
    assert report.baseline_score is None
    assert "--allow-missing-baseline" in report.reason
    report = evolve_once(
        repo,
        agent(write("1", "greet.txt", "hi")),
        icfg(tmp_path, checks=[], eval_cmd=EVAL, allow_missing_baseline=True),
    )
    assert report.accepted, report.summary()


def test_metrics_drop_is_rejected(repo: Path, tmp_path: Path) -> None:
    _impl_repo(repo)
    gutted = "def test_a():\n    pass\n"  # test_b removed from the file, the file kept
    report = evolve_once(
        repo, agent(write("1", "tests/test_impl.py", gutted)), icfg(tmp_path, metrics_cmd=METRICS)
    )
    assert not report.accepted
    assert report.gates["no_test_or_coverage_drop"] is False
    assert "collected tests dropped 2 -> 1" in report.reason
    report = evolve_once(
        repo, agent(write("1", "cov.txt", "89.5")), icfg(tmp_path, metrics_cmd=METRICS)
    )
    assert not report.accepted
    assert "coverage dropped 90.0 -> 89.5" in report.reason


def test_unreadable_metrics_fail_closed(repo: Path, tmp_path: Path) -> None:
    _impl_repo(repo)
    silent = [PY, "-c", "print('nothing useful')"]
    report = evolve_once(
        repo, agent(write("1", "greet.txt", "x")), icfg(tmp_path, metrics_cmd=silent)
    )
    assert not report.accepted
    assert "unreadable" in report.reason
    report = evolve_once(
        repo,
        agent(write("1", "greet.txt", "x")),
        icfg(tmp_path, metrics_cmd=silent, allow_missing_baseline=True),
    )
    assert not report.accepted  # the candidate's own metrics are never optional


def test_parse_metrics() -> None:
    from sebastian.evolve import parse_metrics

    out = (
        "TOTAL  3759  31  1010  28  99%\nRequired ... Total coverage: 98.76%\n"
        "711 passed, 2 skipped, 1 xfailed in 39.81s\n"
    )
    assert parse_metrics(out) == (714, 98.76)
    assert parse_metrics("TOTAL 10 1 2 1 87%\n3 passed, 1 failed in 0.1s") == (4, 87.0)
    assert parse_metrics("junk") == (None, None)


def test_default_metrics_keeps_the_summary_line() -> None:
    """pyproject addopts has -q; another -q (-qq) hides the "N passed" line we parse."""
    from sebastian.evolve import default_metrics

    assert "-q" not in default_metrics()


def test_overlay_drops_tool_caches(tmp_path: Path) -> None:
    from sebastian.evolve import build_overlay

    cand, base = tmp_path / "c", tmp_path / "b"
    for d in (cand, base):
        (d / "tests").mkdir(parents=True)
    (base / "tests/test_a.py").write_text("base")
    (cand / "tests/test_a.py").write_text("candidate")
    (cand / ".git").write_text("gitdir: elsewhere")
    for cache in (".mypy_cache", ".ruff_cache", ".pytest_cache", "src/pkg/__pycache__"):
        (cand / cache).mkdir(parents=True)
        (cand / cache / "planted").write_text("x")
    (cand / ".coverage").write_text("x")
    (cand / "src/pkg/mod.py").write_text("x = 1\n")
    out = build_overlay(cand, base, tmp_path / "o")
    assert (out / "tests/test_a.py").read_text() == "base"
    assert (out / "src/pkg/mod.py").exists()
    for gone in (".git", ".mypy_cache", ".ruff_cache", ".pytest_cache", "src/pkg/__pycache__",
                 ".coverage"):  # fmt: skip
        assert not (out / gone).exists(), gone


@pytest.mark.parametrize("flag", [False, True])
def test_cli_allow_missing_baseline(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: bool
) -> None:
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    import sebastian.evolve as evo

    seen: list[EvolveConfig] = []

    def spy(repo: Path, llm: object, config: EvolveConfig, **kw: object) -> object:
        seen.append(config)
        return evo.EvolveReport(None)

    monkeypatch.setattr(evo, "evolve_once", spy)
    args = ["evolve", "--repo", str(repo), "--sandbox", "none", "--unsafe-no-sandbox"]
    main([*args, *(["--allow-missing-baseline"] if flag else [])], llm_factory=lambda m: agent())
    assert seen[-1].allow_missing_baseline is flag
    assert seen[-1].metrics_cmd  # metrics gate on by default


def test_metrics_use_baseline_config_with_candidate_tests(repo: Path, tmp_path: Path) -> None:
    """A candidate config that would inflate its coverage is ignored by the metrics gate."""
    _impl_repo(repo)
    (repo / "pyproject.toml").write_text("cov = 90\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "config")
    # Coverage comes from pyproject.toml here, standing in for [tool.coverage] omit rules.
    metrics = [PY, "-c", (
        "import pathlib;n=sum(p.read_text().count('def test_') for p in pathlib.Path('tests')"
        ".glob('*.py'));c=pathlib.Path('pyproject.toml').read_text().split('=')[1].strip();"
        "print(f'TOTAL 1 1 1 1 {c}%');print(f'{n} passed in 0.1s')"
    )]  # fmt: skip
    llm = agent(
        write("1", "pyproject.toml", "cov = 99\n"),
        write("2", "tests/test_more.py", "def test_c():\n    pass\n"),
    )
    report = evolve_once(repo, llm, icfg(tmp_path, checks=[], eval_cmd=None, metrics_cmd=metrics))
    assert report.accepted, report.summary()
    assert report.candidate_metrics == (3, 90.0)  # the candidate's test counts, baseline config


# --- P1-05: lock down .git and host git ------------------------------------------------


def test_scoped_approver_denies_git_config_files(tmp_path: Path) -> None:
    tools = {t.name: t for t in file_tools(Workspace(tmp_path))}
    ap = ScopedApprover(tmp_path, re.compile(r"^secret/"), frozenset())
    for path in (".gitattributes", "sub/.gitattributes", ".gitmodules", "a/b/.gitmodules"):
        assert not ap.approve(tools["write_file"], write("1", path, "x")), path
    assert ap.approve(tools["write_file"], write("1", ".gitignore", "x"))


def test_git_config_file_side_channel_is_rejected(repo: Path, tmp_path: Path) -> None:
    run = ToolCall("9", "run_command", {"command": "cp greet.txt .gitattributes"})
    config = cfg(tmp_path, agent_commands=frozenset({"cp"}), eval_cmd=None)
    report = evolve_once(repo, agent(write("1", "greet.txt", "hello"), run), config)
    assert not report.accepted
    assert report.gates["git_files"] is False
    assert ".gitattributes" in report.reason


def test_git_invocations_are_hardened(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import sebastian.evolve as evo

    seen: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        seen.append((argv, kw["env"]))  # type: ignore[arg-type]
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert evo._git(tmp_path, "status") == "ok"
    argv, env = seen[-1]
    joined = " ".join(argv)
    for flag in ("core.fsmonitor=", "core.hooksPath=/dev/null", "protocol.file.allow=never"):
        assert f"-c {flag}" in joined
    assert argv[-1] == "status"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_repo_hooks_and_fsmonitor_never_run(repo: Path, tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    hook = f"#!/bin/sh\necho hook >> {marker}\n"
    for name in ("pre-commit", "post-commit", "post-checkout", "reference-transaction"):
        h = repo / ".git/hooks" / name
        h.write_text(hook)
        h.chmod(0o755)
    monitor = tmp_path / "fsmon.sh"
    monitor.write_text(f"#!/bin/sh\necho fsmonitor >> {marker}\n")
    monitor.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(monitor))
    report = evolve_once(repo, agent(write("1", "greet.txt", "hello")), cfg(tmp_path))
    assert report.accepted, report.summary()
    assert not marker.exists(), marker.read_text()


def test_tampered_git_pointer_aborts(repo: Path, tmp_path: Path) -> None:
    run = ToolCall("9", "run_command", {"command": "cp evil .git"})
    llm = agent(write("1", "evil", f"gitdir: {tmp_path}/attacker\n"), run)
    config = cfg(tmp_path, agent_commands=frozenset({"cp"}), eval_cmd=None)
    report = evolve_once(repo, llm, config)
    assert not report.accepted
    assert report.gates == {"git_intact": False}
    assert ".git" in report.reason
    assert report.patch is None  # no git ran in the tampered worktree
    assert git(repo, "worktree", "list").count("\n") == 1  # still cleaned up
    assert report.branch not in branches(repo)


def test_git_pointer_replaced_by_a_directory_aborts(repo: Path, tmp_path: Path) -> None:
    calls = [ToolCall(str(i), "run_command", {"command": c}) for i, c in enumerate(
        ["rm .git", "mkdir .git"], start=1)]  # fmt: skip
    config = cfg(tmp_path, agent_commands=frozenset({"rm", "mkdir"}), eval_cmd=None)
    report = evolve_once(repo, agent(*calls), config)
    assert report.gates == {"git_intact": False}
    assert git(repo, "worktree", "list").count("\n") == 1
