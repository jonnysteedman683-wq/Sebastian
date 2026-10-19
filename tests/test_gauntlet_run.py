"""B11d: the Gauntlet wired into runs (PLAN, PREDICT, ACT, VERIFY, CRITIQUE, DONE/HELP)."""

from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from sebastian import Budget, Sebastian, StopReason
from sebastian.approval import AllowApprover, ApprovalGate, Approver, DenyApprover
from sebastian.checkpoint import Checkpoints
from sebastian.cli import main
from sebastian.clock import FakeClock, SeededIdSource
from sebastian.config import Config
from sebastian.dashboard import Snapshot, render
from sebastian.evals.scar import ScarError, build_check
from sebastian.gauntlet import (
    CRITIC_SYSTEM,
    PREDICT_INSTRUCTION,
    GauntletPolicy,
    Prediction,
    Stage,
    help_report,
    workspace_diff,
)
from sebastian.llm import LLM, ScriptedLLM
from sebastian.llm.scripted import Step
from sebastian.session import SessionStore
from sebastian.tools import CommandPolicy, ToolRegistry, Workspace, command_tool, file_tools
from sebastian.trace import MemoryTracer, RunSummary
from sebastian.types import LLMResponse, ToolCall, Usage

PY = os.path.basename(sys.executable)
CHECK = "import sys, pathlib\nsys.exit(1 if 'bug' in pathlib.Path('app.txt').read_text() else 0)\n"
CHECKS = f"<checks>\nfile: app.txt\nrun: {PY} check.py\n</checks>"


def write(text: str, cid: str = "w") -> LLMResponse:
    return LLMResponse(
        "writing", (ToolCall(cid, "write_file", {"path": "app.txt", "content": text}),)
    )


PLAN = LLMResponse("1. write app.txt\n2. finish", usage=Usage(1, 1))
PREDICT = LLMResponse(CHECKS, usage=Usage(1, 1))
PASS = LLMResponse("VERDICT: PASS")
FAIL = LLMResponse("VERDICT: FAIL\n- app.txt says nothing useful")


def make(
    tmp_path: Path,
    script: Sequence[Step],
    *,
    policy: GauntletPolicy | None = None,
    approver: Approver | None = None,
    checkpoints: bool = True,
    **kw: Any,
) -> tuple[Sebastian, ScriptedLLM, MemoryTracer]:
    ws_dir = tmp_path / "ws"
    ws_dir.mkdir(exist_ok=True)
    (ws_dir / "check.py").write_text(CHECK)
    ws = Workspace(ws_dir)
    ckpt = Checkpoints(tmp_path / "ckpt") if checkpoints else None
    llm = ScriptedLLM(script)
    tracer = MemoryTracer()
    agent = Sebastian(
        llm=llm,
        tools=ToolRegistry([*file_tools(ws, ckpt), command_tool(ws, CommandPolicy.of([PY]))]),
        gate=ApprovalGate(approver or AllowApprover()),
        budget=kw.pop("budget", Budget(max_steps=30)),
        tracer=tracer,
        clock=FakeClock(),
        ids=kw.pop("ids", SeededIdSource(1)),
        checkpoints=ckpt,
        gauntlet=policy or GauntletPolicy(),
        **kw,
    )
    return agent, llm, tracer


def events(tracer: MemoryTracer, name: str) -> list[dict[str, Any]]:
    return [e for e in tracer.events if e["event"] == name]


def user_texts(llm: ScriptedLLM, call: int) -> list[str]:
    return [m.text for m in llm.calls[call][1] if m.role.value == "user" and m.text]


# --- happy path and loops -------------------------------------------------------------------


def test_happy_path_plans_predicts_verifies_and_critiques(tmp_path: Path) -> None:
    a, llm, tracer = make(tmp_path, [PLAN, PREDICT, write("good"), LLMResponse("done"), PASS])
    result = a.run("write app.txt")
    assert result.stop_reason is StopReason.COMPLETED
    assert result.answer == "done"
    assert result.steps == 5  # plan, predict, act x2, critic: every call is charged
    predict_sys, _, predict_tools = llm.calls[1]
    assert predict_sys.endswith(PREDICT_INSTRUCTION)
    assert predict_tools == ()
    critic_sys, critic_msgs, critic_tools = llm.calls[4]
    assert critic_sys == CRITIC_SYSTEM
    assert critic_tools == ()
    assert "+good" in critic_msgs[0].text  # the critic reviews the real diff
    assert events(tracer, "gauntlet_predict")[0]["commands"] == [f"{PY} check.py"]
    assert [e["passed"] for e in events(tracer, "gauntlet_verify")] == [True]
    assert [e["stage"] for e in events(tracer, "gauntlet_stage")] == ["done"]


def test_failed_check_loops_back_to_act_then_passes(tmp_path: Path) -> None:
    script = [PLAN, PREDICT, write("bug"), LLMResponse("done"), write("fixed", "w2"),
              LLMResponse("done again"), PASS]  # fmt: skip
    a, llm, tracer = make(tmp_path, script)
    result = a.run("write app.txt")
    assert result.stop_reason is StopReason.COMPLETED
    assert result.answer == "done again"
    loop_back = user_texts(llm, 4)[-1]
    assert loop_back.startswith('<verify round="1">')
    assert "FAILED" in loop_back
    assert [e["passed"] for e in events(tracer, "gauntlet_verify")] == [False, True]
    assert [(e["stage"], e["loops"]) for e in events(tracer, "gauntlet_stage")] == [
        ("act", 1),
        ("done", 1),
    ]


def test_critic_fail_loops_back_with_its_findings(tmp_path: Path) -> None:
    script = [PLAN, PREDICT, write("good"), LLMResponse("done"), FAIL, LLMResponse("ok"), PASS]
    a, llm, tracer = make(tmp_path, script)
    assert a.run("t").stop_reason is StopReason.COMPLETED
    assert "app.txt says nothing useful" in user_texts(llm, 5)[-1]
    crit = events(tracer, "gauntlet_critique")
    assert [c["passed"] for c in crit] == [False, True]


def test_loop_limit_ends_as_needs_help(tmp_path: Path) -> None:
    policy = GauntletPolicy(max_loops=2, replan_after=5)
    script = [PLAN, PREDICT, write("good"), LLMResponse("done"), FAIL, LLMResponse("done"), FAIL]
    a, llm, tracer = make(tmp_path, script, policy=policy)
    result = a.run("t")
    assert result.stop_reason is StopReason.NEEDS_HELP
    assert "loop limit" in result.detail
    assert result.answer.startswith("NEEDS HELP")
    assert "Stopped at: critique" in result.answer
    assert "app.txt says nothing useful" in result.answer
    assert f"run: {PY} check.py" in result.answer
    assert events(tracer, "gauntlet_help")[0]["loops"] == 2
    assert events(tracer, "run_end")[0]["stop"] == "needs_help"
    assert len(llm.calls) == len(script)  # HELP makes no further calls


def test_replan_rewrites_plan_and_checks(tmp_path: Path) -> None:
    policy = GauntletPolicy(max_loops=3, replan_after=1)
    new_predict = LLMResponse(f"<checks>\nfile: app.txt\nrun: {PY} check.py\n</checks>")
    script = [PLAN, PREDICT, write("bug"), LLMResponse("done"),
              LLMResponse("1. remove the bug"), new_predict,
              write("fixed", "w2"), LLMResponse("done"), PASS]  # fmt: skip
    a, llm, tracer = make(tmp_path, script, policy=policy)
    assert a.run("t").stop_reason is StopReason.COMPLETED
    replan_request = llm.calls[4][1][0].text
    assert "<previous_attempt>" in replan_request
    assert "FAILED" in replan_request
    assert llm.calls[4][2] == ()
    turn = user_texts(llm, 6)[-1]
    assert "<replan>" in turn
    assert "1. remove the bug" in turn
    assert f"- {PY} check.py" in turn
    assert [e["stage"] for e in events(tracer, "gauntlet_stage")] == ["plan", "done"]
    assert len(events(tracer, "plan")) == 2
    assert len(events(tracer, "gauntlet_predict")) == 2


def test_rejected_replan_prediction_ends_in_help(tmp_path: Path) -> None:
    policy = GauntletPolicy(max_loops=3, replan_after=1)
    script = [PLAN, PREDICT, write("bug"), LLMResponse("done"), PLAN, LLMResponse("no checks")]
    a, _, _ = make(tmp_path, script, policy=policy)
    result = a.run("t")
    assert result.stop_reason is StopReason.NEEDS_HELP
    assert "prediction rejected" in result.detail
    assert "Failed rounds: 1" in result.answer


# --- HELP paths -----------------------------------------------------------------------------


def test_rejected_prediction_stops_before_acting(tmp_path: Path) -> None:
    a, llm, tracer = make(tmp_path, [PLAN, LLMResponse("I will just do it")])
    result = a.run("t")
    assert result.stop_reason is StopReason.NEEDS_HELP
    assert "prediction rejected" in result.detail
    assert "Stopped at: predict" in result.answer
    assert len(llm.calls) == 2
    assert not events(tracer, "tool_ok")


def test_denied_check_goes_straight_to_help(tmp_path: Path) -> None:
    a, llm, tracer = make(tmp_path, [PLAN, PREDICT, LLMResponse("done")], approver=DenyApprover())
    result = a.run("t")
    assert result.stop_reason is StopReason.NEEDS_HELP
    assert "denied by the approval gate" in result.detail
    assert "Failed rounds: 0" in result.answer
    assert not events(tracer, "gauntlet_critique")  # a denied check never reaches the critic
    assert len(llm.calls) == 3


# --- budget and errors ----------------------------------------------------------------------


def test_budget_runs_out_before_the_critic(tmp_path: Path) -> None:
    a, llm, _ = make(
        tmp_path, [PLAN, PREDICT, write("good"), LLMResponse("done")], budget=Budget(max_steps=4)
    )
    result = a.run("t")
    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert result.answer == "done"
    assert len(llm.calls) == 4


def test_budget_runs_out_before_predict(tmp_path: Path) -> None:
    a, llm, _ = make(tmp_path, [PLAN], budget=Budget(max_steps=1))
    assert a.run("t").stop_reason is StopReason.BUDGET_EXHAUSTED
    assert len(llm.calls) == 1


def test_budget_runs_out_before_replan(tmp_path: Path) -> None:
    policy = GauntletPolicy(max_loops=3, replan_after=1)
    script = [PLAN, PREDICT, write("bug"), LLMResponse("done")]
    a, _, _ = make(tmp_path, script, policy=policy, budget=Budget(max_steps=4))
    assert a.run("t").stop_reason is StopReason.BUDGET_EXHAUSTED


@pytest.mark.parametrize(
    ("script", "where"),
    [
        ([PLAN], "predict"),  # script runs out at the PREDICT call
        ([PLAN, PREDICT, LLMResponse("done")], "critique"),
        ([PLAN, PREDICT, write("bug"), LLMResponse("done")], "planning"),  # replan call
    ],
)
def test_model_errors_end_the_run(tmp_path: Path, script: list[Step], where: str) -> None:
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "app.txt").write_text("fine")
    a, _, _ = make(tmp_path, script, policy=GauntletPolicy(replan_after=1))
    result = a.run("t")
    assert result.stop_reason is StopReason.ERROR
    assert result.detail.startswith(where)


# --- critic choice and diff -----------------------------------------------------------------


def test_separate_critic_model_gets_only_the_review(tmp_path: Path) -> None:
    critic = ScriptedLLM([PASS])
    a, llm, _ = make(
        tmp_path, [PLAN, PREDICT, write("good"), LLMResponse("done")],
        policy=GauntletPolicy(critic_llm=critic),
    )  # fmt: skip
    assert a.run("t").stop_reason is StopReason.COMPLETED
    assert len(critic.calls) == 1
    assert len(llm.calls) == 4


def test_planner_model_plans_predicts_and_critiques(tmp_path: Path) -> None:
    planner = ScriptedLLM([PLAN, PREDICT, PASS])
    a, llm, _ = make(tmp_path, [write("good"), LLMResponse("done")], planner_llm=planner)
    assert a.run("t").stop_reason is StopReason.COMPLETED
    assert len(planner.calls) == 3
    assert len(llm.calls) == 2


def test_without_checkpoints_the_critic_is_told_the_diff_is_unavailable(tmp_path: Path) -> None:
    a, llm, _ = make(
        tmp_path, [PLAN, PREDICT, write("good"), LLMResponse("done"), PASS], checkpoints=False
    )
    assert a.run("t").stop_reason is StopReason.COMPLETED
    assert "diff unavailable" in llm.calls[4][1][0].text


def test_no_writes_means_no_diff(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "app.txt").write_text("fine")
    a, llm, _ = make(tmp_path, [PLAN, PREDICT, LLMResponse("already done"), PASS])
    assert a.run("t").stop_reason is StopReason.COMPLETED
    assert "(no files changed)" in llm.calls[3][1][0].text


def test_workspace_diff(tmp_path: Path) -> None:
    (tmp_path / "mod.txt").write_text("new\n")
    (tmp_path / "made.txt").write_text("hi\n")
    (tmp_path / "same.txt").write_text("same\n")
    (tmp_path / "bin.dat").write_bytes(b"\xff\x00")
    diff = workspace_diff(
        {
            "mod.txt": b"old\n",
            "made.txt": None,
            "gone.txt": b"bye\n",
            "same.txt": b"same\n",
            "bin.dat": b"\xfe",
        },
        tmp_path,
    )
    assert "--- a/mod.txt\n+++ b/mod.txt" in diff
    assert "-old" in diff
    assert "+new" in diff
    assert "--- /dev/null\n+++ b/made.txt" in diff
    assert "--- a/gone.txt\n+++ /dev/null" in diff
    assert "-bye" in diff
    assert "same.txt" not in diff
    assert "Binary file bin.dat changed" in diff
    assert workspace_diff({}, tmp_path) == "(no files changed)"
    assert workspace_diff({"same.txt": b"same\n"}, tmp_path) == "(no files changed)"


def test_help_report_without_prediction_or_findings() -> None:
    text = help_report(Stage.PREDICT, "prediction rejected: empty", 0, None)
    assert "Predicted checks" not in text
    assert "Last findings" not in text
    assert "sebastian undo <run_id>" in text
    long = help_report(Stage.VERIFY, "x", 1, Prediction(files=("a",)), "y" * 10_000)
    assert "file: a" in long
    assert len(long) < 5000


# --- resume ---------------------------------------------------------------------------------


def test_resume_under_the_gauntlet_predicts_again(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    script = [PLAN, PREDICT, write("good")]
    a, _, _ = make(tmp_path, script, sessions=store, budget=Budget(max_steps=3))
    first = a.run("t")
    assert first.stop_reason is StopReason.BUDGET_EXHAUSTED
    b, llm, tracer = make(
        tmp_path, [PREDICT, LLMResponse("done"), PASS], sessions=store,
        ids=SeededIdSource(7),
    )  # fmt: skip
    assert b.resume(first.run_id).stop_reason is StopReason.COMPLETED
    assert llm.calls[0][0].endswith(PREDICT_INSTRUCTION)
    assert events(tracer, "gauntlet_predict")


def test_needs_help_runs_can_be_resumed(tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    a, _, _ = make(tmp_path, [PLAN, LLMResponse("no")], sessions=store)
    first = a.run("t")
    assert first.stop_reason is StopReason.NEEDS_HELP
    assert store.load(first.run_id).status == "needs_help"


# --- off by default -------------------------------------------------------------------------


def test_gauntlet_is_off_by_default(tmp_path: Path) -> None:
    llm = ScriptedLLM([LLMResponse("done")])
    a = Sebastian(llm=llm, tools=ToolRegistry([]), clock=FakeClock(), ids=SeededIdSource(1))
    assert a.gauntlet is None
    assert a.run("t").stop_reason is StopReason.COMPLETED
    assert len(llm.calls) == 1


# --- CLI, config, dashboard, scars ----------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(h))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return h


def factory(scripts: dict[str, list[LLMResponse]], seen: list[str]) -> Callable[[str], LLM]:
    def build(model: str) -> LLM:
        seen.append(model)
        return ScriptedLLM(scripts[model])

    return build


def _cli_events(home: Path) -> list[str]:
    lines = (home / "traces.jsonl").read_text().splitlines()
    return [json.loads(x)["event"] for x in lines]


def test_cli_gauntlet_flag(tmp_path: Path, home: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    no_code = LLMResponse("<checks>\nfile: notes.md\n</checks>")
    scripts = {"claude-sonnet-5-5": [PLAN, no_code, LLMResponse("done"), PASS]}
    seen: list[str] = []
    args = ["run", "t", "-w", str(ws), "--no-memory", "--gauntlet"]
    assert main(args, llm_factory=factory(scripts, seen)) == 0
    assert seen == ["claude-sonnet-5-5"]
    assert {"plan", "gauntlet_predict", "gauntlet_critique"} <= set(_cli_events(home))


def test_cli_critic_model_implies_gauntlet_and_needs_help_exits_1(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    no_code = LLMResponse("<checks>\nfile: notes.md\n</checks>")
    scripts = {
        "claude-sonnet-5-5": [PLAN, no_code, LLMResponse("done"), LLMResponse("done"),
                              PLAN, no_code, LLMResponse("done")],
        "claude-opus-5-5": [FAIL, FAIL, FAIL],
    }  # fmt: skip
    seen: list[str] = []
    args = ["run", "t", "-w", str(ws), "--no-memory", "--critic-model", "claude-opus-5-5"]
    assert main(args, llm_factory=factory(scripts, seen)) == 1
    assert seen == ["claude-sonnet-5-5", "claude-opus-5-5"]
    out = capsys.readouterr()
    assert out.out.startswith("NEEDS HELP")
    assert "[needs_help]" in out.err


def test_cli_critic_model_same_as_main_builds_one_client(tmp_path: Path, home: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    no_code = LLMResponse("<checks>\nfile: notes.md\n</checks>")
    scripts = {"claude-sonnet-5-5": [PLAN, no_code, LLMResponse("done"), PASS]}
    seen: list[str] = []
    args = ["run", "t", "-w", str(ws), "--no-memory", "--critic-model", "claude-sonnet-5-5"]
    assert main(args, llm_factory=factory(scripts, seen)) == 0
    assert seen == ["claude-sonnet-5-5"]


def test_config_reads_critic_model() -> None:
    assert Config.from_env({"SEBASTIAN_CRITIC_MODEL": "claude-opus-5-5"}).critic_model == (
        "claude-opus-5-5"
    )
    assert Config.from_env({}).critic_model == ""


def test_dashboard_counts_needs_help_runs() -> None:
    runs = [RunSummary("r1", 1.0, "needs_help", 3, 10), RunSummary("r2", 2.0, "completed", 2, 5)]
    html = render(Snapshot(records=[], runs=runs, scars=[], generated=3.0))
    assert "Needs help" in html
    assert "<td>needs_help</td>" in html


def test_scars_may_expect_needs_help() -> None:
    build_check({"stop": "needs_help"})
    with pytest.raises(ScarError, match="needs_help"):
        build_check({"stop": "nope"})
