"""P1-18: eval checkers run model code without host secrets, stray processes or unbounded output."""

from __future__ import annotations

import json
import os
import resource
import shutil
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from sebastian.evals import sandbox
from sebastian.evals.cases import ALL, HARD, TEST_CALC, _py_ok
from sebastian.evals.harness import Outcome, run_case, run_suite
from sebastian.evals.sandbox import (
    BWRAP,
    NONE,
    UNAVAILABLE,
    UNAVAILABLE_ERROR,
    CheckerSandboxError,
    checker_env,
    checker_sandbox,
    run_python_check,
)
from sebastian.gauntlet import GauntletPolicy
from sebastian.llm.scripted import ScriptedLLM
from sebastian.tools import builtin
from sebastian.tools.base import Tool, ToolError
from sebastian.types import LLMResponse, Message, RunResult, StopReason, Usage

FAKE = {
    "SEBASTIAN_FAKE_SECRET": "sk-test-FAKE123",
    "OPENAI_API_KEY": "sk-test-FAKE-openai",
    "ANTHROPIC_API_KEY": "sk-test-FAKE-anthropic",
}
CODE_CASES = {"fix_failing_test", "rename_across_files", "find_bug_no_hint"}


def _outcome(ws: Path, checker: str = NONE) -> Outcome:
    result = RunResult("r", StopReason.COMPLETED, "done", 1, Usage())
    return Outcome(ws, result, [], {}, frozenset(), checker)


def _no_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builtin, "bwrap_usable", lambda exe: False)


class LiveDouble:
    """Stands in for a real model: not a ScriptedLLM, so the harness treats it as live."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, system: str, messages: Sequence[Message], tools: Sequence[Tool]
    ) -> LLMResponse:
        self.calls += 1
        return LLMResponse("done")


def test_checker_env_is_an_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in FAKE.items():
        monkeypatch.setenv(k, v)
    env = checker_env("/w")
    assert set(env) <= {"PATH", "LANG", "LC_ALL", "HOME", "PYTHONDONTWRITEBYTECODE"}
    assert env["HOME"] == "/w"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_py_ok_never_sees_host_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in FAKE.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "calc.py").write_text(
        "import json, os\nopen('env.txt', 'w').write(json.dumps(dict(os.environ)))\n"
    )
    assert _py_ok(_outcome(tmp_path), "import calc")
    seen = json.loads((tmp_path / "env.txt").read_text())
    for name, value in FAKE.items():
        assert name not in seen
        assert value not in json.dumps(seen)
    assert seen["HOME"] == str(tmp_path)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups")
def test_background_process_does_not_outlive_the_check(tmp_path: Path) -> None:
    pid_file = tmp_path / "bg.pid"
    (tmp_path / "bg.py").write_text(
        "import subprocess\n"
        "p = subprocess.Popen(['sleep', '300'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
    )
    run = run_python_check(tmp_path, ["bg.py"], sandbox=NONE, timeout=20)
    assert run.ok
    pid = int(pid_file.read_text())
    for _ in range(50):  # the kill is sent; give the kernel a moment to reap
        if not _alive(pid):
            break
        time.sleep(0.1)
    assert not _alive(pid)


def test_timeout_kills_the_whole_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "bg.pid"
    (tmp_path / "hang.py").write_text(
        "import subprocess, time\n"
        "p = subprocess.Popen(['sleep', '300'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(300)\n"
    )
    start = time.monotonic()
    run = run_python_check(tmp_path, ["hang.py"], sandbox=NONE, timeout=2)
    assert run.timed_out
    assert not run.ok
    assert time.monotonic() - start < 15
    pid = int(pid_file.read_text())
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    assert not _alive(pid)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")  # a zombie is dead for our purposes
    return not (stat.exists() and stat.read_text().split()[2] == "Z")


def test_huge_output_stays_bounded(tmp_path: Path) -> None:
    (tmp_path / "loud.py").write_text(
        "import sys\nchunk = 'x' * (1 << 20)\nfor _ in range(50):\n    sys.stdout.write(chunk)\n"
        "print('END')\n"
    )
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    run = run_python_check(tmp_path, ["loud.py"], sandbox=NONE)
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert run.ok
    assert len(run.stdout) <= sandbox.MAX_OUTPUT_BYTES
    assert run.stdout.rstrip().endswith("END")  # the tail is kept
    assert after - before < 40 * 1024  # ru_maxrss is KiB on Linux: well under the 50 MB printed


def test_unavailable_sandbox_refuses_to_run(tmp_path: Path) -> None:
    with pytest.raises(CheckerSandboxError, match=UNAVAILABLE_ERROR):
        run_python_check(tmp_path, ["-c", "pass"], sandbox=UNAVAILABLE)


def test_sandbox_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bwrap(monkeypatch)
    assert checker_sandbox(live=False) == NONE
    assert checker_sandbox(live=True) == UNAVAILABLE
    assert checker_sandbox(live=True, unsafe=True) == NONE


def test_newer_fail_closed_resolver_counts_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raises(mode: str) -> tuple[str, str]:
        raise ToolError("bubblewrap is missing")

    monkeypatch.setattr(sandbox, "resolve_command_sandbox", raises)
    assert checker_sandbox(live=True) == UNAVAILABLE


def test_live_code_cases_fail_closed_without_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bwrap(monkeypatch)
    model = LiveDouble()
    suite = run_suite(HARD, lambda: model)
    by_name = {r.name: r for r in suite.results}
    for name in CODE_CASES:
        assert by_name[name].error == UNAVAILABLE_ERROR
        assert not by_name[name].passed
        assert by_name[name].stop == "error"
    # Code cases never reached the model; the three non-code cases did.
    assert model.calls == len(HARD) - len(CODE_CASES)
    assert UNAVAILABLE_ERROR not in by_name["edit_json_config"].error


def test_checker_refuses_even_if_reached(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("raise SystemExit(0)\n")
    (tmp_path / "test_calc.py").write_text(TEST_CALC)
    case = HARD[0]
    with pytest.raises(CheckerSandboxError):
        case.check(_outcome(tmp_path, UNAVAILABLE))


def test_unsafe_flag_runs_live_code_cases_unsandboxed(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bwrap(monkeypatch)
    suite = run_suite(HARD, LiveDouble, unsafe_checkers=True)
    assert all(r.error != UNAVAILABLE_ERROR for r in suite.results)


def test_scripted_factory_is_not_live(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bwrap(monkeypatch)
    case = HARD[0]
    result = run_case(case, ScriptedLLM(case.script))
    assert result.passed
    assert not result.error


def test_offline_suites_still_pass_without_bwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bwrap(monkeypatch)
    plain = run_suite(ALL)
    gauntlet = run_suite(ALL, gauntlet=GauntletPolicy())
    assert (sum(r.passed for r in plain.results), len(plain.results)) == (16, 16)
    assert (sum(r.passed for r in gauntlet.results), len(gauntlet.results)) == (18, 18)


def test_live_commands_use_bwrap_when_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "bwrap_ready", lambda: True)
    from sebastian.evals import harness

    policy = harness._command_policy(HARD[0], True, BWRAP)
    assert policy.sandbox == BWRAP
    assert policy.extra_ro
    assert harness._command_policy(HARD[0], False, BWRAP).sandbox == "none"


_BWRAP = shutil.which("bwrap")


@pytest.mark.skipif(
    _BWRAP is None or not builtin.bwrap_usable(_BWRAP), reason="bubblewrap unusable here"
)
def test_bwrap_checker_has_no_network_and_no_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for k, v in FAKE.items():
        monkeypatch.setenv(k, v)
    (tmp_path / "probe.py").write_text(
        "import json, os, socket\n"
        "try:\n    socket.create_connection(('1.1.1.1', 53), timeout=2); net = True\n"
        "except OSError:\n    net = False\n"
        "open('out.json', 'w').write(json.dumps({'net': net, 'env': dict(os.environ)}))\n"
    )
    assert run_python_check(tmp_path, ["probe.py"], sandbox=BWRAP).ok
    out = json.loads((tmp_path / "out.json").read_text())
    assert out["net"] is False
    assert not set(FAKE) & set(out["env"])


def test_code_cases_are_flagged() -> None:
    flagged = {c.name for c in ALL if c.runs_code}
    assert flagged == CODE_CASES


def test_live_evals_cli_fails_closed_and_flag_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sebastian.evals.__main__ import main

    _no_bwrap(monkeypatch)
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path))
    args = ["--live", "--no-record", "--min-score", "0", "--scars", str(tmp_path / "none")]
    assert main(args, live_llm=lambda _m: LiveDouble()) == 0
    out = capsys.readouterr()
    gated = [c for c in ALL if (c.runs_code or c.commands) and not c.gauntlet_only]
    assert out.out.count(UNAVAILABLE_ERROR) == len(gated)  # code checkers and run_command
    assert main([*args, "--unsafe-unsandboxed-checkers"], live_llm=lambda _m: LiveDouble()) == 0
    out = capsys.readouterr()
    assert UNAVAILABLE_ERROR not in out.out
    assert "--unsafe-unsandboxed-checkers" in out.err


def test_models_cli_passes_the_unsafe_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sebastian.cli import main

    _no_bwrap(monkeypatch)
    monkeypatch.setenv("SEBASTIAN_HOME", str(tmp_path))
    seen: list[bool] = []
    real = run_suite

    def spy(*args: object, **kwargs: object) -> object:
        seen.append(bool(kwargs.get("unsafe_checkers")))
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("sebastian.evals.leaderboard.run_suite", spy)
    base = ["models", "m", "--no-record", "--scars", str(tmp_path / "none")]
    assert main(base, llm_factory=lambda _n: LiveDouble()) == 0
    assert main([*base, "--unsafe-unsandboxed-checkers"], llm_factory=lambda _n: LiveDouble()) == 0
    assert seen == [False, True]
    assert "--unsafe-unsandboxed-checkers" in capsys.readouterr().err
