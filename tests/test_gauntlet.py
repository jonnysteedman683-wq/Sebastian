from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from sebastian.approval import AllowApprover, ApprovalGate, Approver, DenyApprover
from sebastian.gauntlet import (
    MAX_CHECK_OUTPUT,
    PREDICT_INSTRUCTION,
    GauntletError,
    GauntletPolicy,
    Prediction,
    Stage,
    next_stage,
    parse_prediction,
    predict,
    verify,
)
from sebastian.llm import LLMError, ScriptedLLM
from sebastian.tools import CommandPolicy, ToolRegistry, Workspace, command_tool, file_tools
from sebastian.trace import MemoryTracer
from sebastian.types import LLMResponse, Usage

GOOD = """<checks>
file: src/app.py
file: tests/test_app.py
run: pytest -q tests/test_app.py
</checks>"""


# --- stage machine --------------------------------------------------------------------------


def test_happy_path_walks_every_stage_in_order() -> None:
    p = GauntletPolicy()
    stage, seen = Stage.PLAN, []
    while stage not in (Stage.DONE, Stage.HELP):
        seen.append(stage)
        stage = next_stage(stage, True, 0, p).stage
    assert seen == [Stage.PLAN, Stage.PREDICT, Stage.ACT, Stage.VERIFY, Stage.CRITIQUE]
    assert stage is Stage.DONE


@pytest.mark.parametrize("stage", [Stage.PLAN, Stage.PREDICT, Stage.ACT])
def test_early_stage_failure_goes_to_help_without_counting_a_loop(stage: Stage) -> None:
    t = next_stage(stage, False, 0, GauntletPolicy())
    assert (t.stage, t.loops) == (Stage.HELP, 0)
    assert "failed" in t.reason


def test_failed_checks_loop_back_then_replan_then_help() -> None:
    p = GauntletPolicy(max_loops=4, replan_after=2)
    t1 = next_stage(Stage.VERIFY, False, 0, p)
    t2 = next_stage(Stage.CRITIQUE, False, t1.loops, p)
    t3 = next_stage(Stage.VERIFY, False, t2.loops, p)
    t4 = next_stage(Stage.VERIFY, False, t3.loops, p)
    assert [(t.stage, t.loops) for t in (t1, t2, t3, t4)] == [
        (Stage.ACT, 1),
        (Stage.PLAN, 2),
        (Stage.ACT, 3),
        (Stage.HELP, 4),
    ]
    assert "loop limit" in t4.reason


def test_default_policy_reaches_help_after_three_failures() -> None:
    p, loops, stage = GauntletPolicy(), 0, Stage.VERIFY
    for _ in range(3):
        t = next_stage(Stage.VERIFY, False, loops, p)
        loops, stage = t.loops, t.stage
    assert (stage, loops) == (Stage.HELP, 3)


def test_passing_keeps_the_loop_count() -> None:
    assert next_stage(Stage.ACT, True, 2, GauntletPolicy()).loops == 2


@pytest.mark.parametrize("stage", [Stage.DONE, Stage.HELP])
def test_terminal_stages_cannot_transition(stage: Stage) -> None:
    with pytest.raises(GauntletError, match="terminal"):
        next_stage(stage, True, 0, GauntletPolicy())


def test_negative_loops_rejected() -> None:
    with pytest.raises(GauntletError):
        next_stage(Stage.VERIFY, False, -1, GauntletPolicy())


def test_policy_validates_bounds() -> None:
    with pytest.raises(GauntletError, match="max_loops"):
        GauntletPolicy(max_loops=0)
    with pytest.raises(GauntletError, match="replan_after"):
        GauntletPolicy(replan_after=0)


# --- prediction parsing ---------------------------------------------------------------------


def test_parses_files_and_commands() -> None:
    pred = parse_prediction("Sure.\n" + GOOD + "\n")
    assert pred.files == ("src/app.py", "tests/test_app.py")
    assert pred.commands == ("pytest -q tests/test_app.py",)
    assert pred.touches_code


def test_duplicates_collapse_and_paths_normalise() -> None:
    pred = parse_prediction("<checks>\nfile: ./a.txt\nfile: a.txt\nrun: ls\nrun: ls\n</checks>")
    assert pred == Prediction(files=("a.txt",), commands=("ls",))


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("no block here", "exactly one"),
        ("<checks>file: a</checks><checks>file: b</checks>", "exactly one"),
        ("<checks>\n</checks>", "empty"),
        ("<checks>\nfile a.txt\n</checks>", "malformed"),
        ("<checks>\nfile:\n</checks>", "malformed"),
        ("<checks>\nexec: ls\n</checks>", "unknown key"),
        ("<checks>\nfile: /etc/passwd\n</checks>", "escapes"),
        ("<checks>\nfile: ../up.txt\n</checks>", "escapes"),
        ("<checks>\nfile: a\\b\n</checks>", "escapes"),
        ("<checks>\nrun: /usr/bin/pytest\n</checks>", "bare executable"),
        ("<checks>\nrun: pytest; rm -rf .\n</checks>", "shell operators"),
        ("<checks>\nrun: pytest && echo ok\n</checks>", "shell operators"),
        ("<checks>\nrun: echo $HOME\n</checks>", "shell operators"),
        ("<checks>\nrun: echo 'unterminated\n</checks>", "unparseable"),
        ("<checks>\nrun: " + "x" * 400 + "\n</checks>", "too long"),
        ("<checks>\nfile: src/a.py\n</checks>", "no run: check"),
    ],
)
def test_rejects_bad_predictions(text: str, match: str) -> None:
    with pytest.raises(GauntletError, match=match):
        parse_prediction(text)


def test_limits_on_counts() -> None:
    many_files = "\n".join(f"file: f{i}.txt" for i in range(21))
    with pytest.raises(GauntletError, match="too many files"):
        parse_prediction(f"<checks>\n{many_files}\n</checks>")
    many_runs = "\n".join(f"run: t{i}" for i in range(6))
    with pytest.raises(GauntletError, match="too many commands"):
        parse_prediction(f"<checks>\n{many_runs}\n</checks>")


def test_lenient_policy_allows_code_without_checks() -> None:
    pred = parse_prediction(
        "<checks>\nfile: src/a.py\n</checks>", GauntletPolicy(checks_required=False)
    )
    assert pred.files == ("src/a.py",)
    assert not pred.commands


def test_non_code_prediction_needs_no_command() -> None:
    assert parse_prediction("<checks>\nfile: notes.md\n</checks>").commands == ()


# --- the PREDICT call -----------------------------------------------------------------------


def test_predict_is_tool_free_and_carries_task_and_plan() -> None:
    llm = ScriptedLLM([LLMResponse(text=GOOD, usage=Usage(10, 5))])
    out = predict(llm, "SYS", "add a feature", plan="1. edit\n2. test")
    system, messages, tools = llm.calls[0]
    assert tools == ()
    assert system == "SYS" + PREDICT_INSTRUCTION
    assert "add a feature" in messages[0].text
    assert "<plan>" in messages[0].text
    assert out.passed
    assert out.prediction is not None
    assert out.prediction.commands == ("pytest -q tests/test_app.py",)
    assert out.response.usage == Usage(10, 5)


def test_predict_without_plan_omits_plan_block() -> None:
    llm = ScriptedLLM([LLMResponse(text=GOOD)])
    predict(llm, "SYS", "task")
    assert "<plan>" not in llm.calls[0][1][0].text


def test_rejected_prediction_still_returns_usage() -> None:
    llm = ScriptedLLM([LLMResponse(text="I will just do it.", usage=Usage(7, 3))])
    out = predict(llm, "SYS", "task")
    assert not out.passed
    assert out.prediction is None
    assert "exactly one" in out.error
    assert out.response.usage == Usage(7, 3)
    assert next_stage(Stage.PREDICT, out.passed, 0, GauntletPolicy()).stage is Stage.HELP


def test_predict_propagates_llm_errors() -> None:
    with pytest.raises(LLMError):
        predict(ScriptedLLM([]), "SYS", "task")


# --- VERIFY (B11b) ---------------------------------------------------------------------------

PY = os.path.basename(sys.executable)
CHECK = "import sys, pathlib\nsys.exit(1 if 'bug' in pathlib.Path('app.txt').read_text() else 0)\n"


def _verify_env(
    tmp_path: Path, approver: Approver | None = None
) -> tuple[ToolRegistry, ApprovalGate, MemoryTracer]:
    ws = Workspace(tmp_path)
    (tmp_path / "check.py").write_text(CHECK)
    tools = ToolRegistry([*file_tools(ws), command_tool(ws, CommandPolicy.of([PY]))])
    return tools, ApprovalGate(approver or AllowApprover()), MemoryTracer()


def test_failed_check_loops_back_then_fix_passes(tmp_path: Path) -> None:
    tools, gate, tracer = _verify_env(tmp_path)
    pred = parse_prediction(f"<checks>\nfile: app.txt\nrun: {PY} check.py\n</checks>")
    policy = GauntletPolicy()

    (tmp_path / "app.txt").write_text("has a bug")  # ACT, first attempt
    first = verify(pred, tools, gate, tracer, "r1")
    assert not first.passed
    assert first.checks[0].output.startswith("exit_code=1")
    t = next_stage(Stage.VERIFY, first.passed, 0, policy)
    assert (t.stage, t.loops) == (Stage.ACT, 1)
    findings = first.findings(t.loops)
    assert f"`{PY} check.py`: FAILED" in findings
    assert "DATA" in findings

    (tmp_path / "app.txt").write_text("fixed")  # ACT, second attempt
    second = verify(pred, tools, gate, tracer, "r1")
    assert second.passed
    assert next_stage(Stage.VERIFY, second.passed, t.loops, policy).stage is Stage.CRITIQUE
    events = [e for e in tracer.events if e["event"] == "gauntlet_verify"]
    assert [e["passed"] for e in events] == [False, True]


def test_denied_check_fails_the_gate_and_never_runs(tmp_path: Path) -> None:
    tools, gate, tracer = _verify_env(tmp_path, DenyApprover())
    (tmp_path / "app.txt").write_text("fixed")
    out = verify(Prediction(commands=(f"{PY} check.py",)), tools, gate, tracer, "r2")
    assert not out.passed
    assert out.denied
    assert out.checks[0].output == "denied by approval gate"
    assert "denied" in out.findings(1)
    denied = [e for e in tracer.events if e["event"] == "approval_denied"]
    assert denied[0]["stage"] == "verify"


def test_non_allowlisted_command_fails(tmp_path: Path) -> None:
    tools, gate, _ = _verify_env(tmp_path)
    out = verify(Prediction(commands=("ls",)), tools, gate)
    assert not out.passed
    assert "not allowed" in out.checks[0].output


def test_missing_run_command_tool_fails(tmp_path: Path) -> None:
    tools = ToolRegistry(file_tools(Workspace(tmp_path)))
    out = verify(Prediction(commands=("pytest",)), tools, ApprovalGate(AllowApprover()))
    assert not out.passed
    assert "not available" in out.checks[0].output


def test_no_commands_passes_and_output_is_tail_capped(tmp_path: Path) -> None:
    tools, gate, _ = _verify_env(tmp_path)
    assert verify(Prediction(files=("notes.md",)), tools, gate).passed
    (tmp_path / "loud.py").write_text("print('x' * 10000)\nraise SystemExit(3)\n")
    out = verify(Prediction(commands=(f"{PY} loud.py",)), tools, gate)
    assert not out.passed
    assert len(out.checks[0].output) <= MAX_CHECK_OUTPUT
