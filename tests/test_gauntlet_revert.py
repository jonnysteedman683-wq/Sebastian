"""B33: undo a failed Gauntlet attempt's writes before the next one."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from sebastian import StopReason
from sebastian.checkpoint import CheckpointError, Checkpoints
from sebastian.cli import main
from sebastian.gauntlet import GauntletPolicy
from sebastian.llm.scripted import Step
from sebastian.tools import Workspace
from sebastian.types import LLMResponse, Message, ToolCall
from test_gauntlet_run import FAIL, PASS, PLAN, PREDICT, events, make, user_texts, write

REVERT = GauntletPolicy(revert_on_loop=True)


def junk() -> LLMResponse:
    return LLMResponse(
        "extra", (ToolCall("j", "write_file", {"path": "junk.txt", "content": "x"}),)
    )


def seed(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.txt").write_text("orig")
    return ws


def snapshot_then(
    ws: Path, seen: list[dict[str, str | None]], then: LLMResponse
) -> Callable[[Sequence[Message]], LLMResponse]:
    def step(_: Sequence[Message]) -> LLMResponse:
        seen.append(
            {
                n: (ws / n).read_text() if (ws / n).exists() else None
                for n in ("app.txt", "junk.txt")
            }
        )
        return then

    return step


def test_failed_attempt_is_undone_before_the_next_act(tmp_path: Path) -> None:
    ws = seed(tmp_path)
    seen: list[dict[str, str | None]] = []
    script: list[Step] = [PLAN, PREDICT, write("bug"), junk(), LLMResponse("done"),
              snapshot_then(ws, seen, write("fixed", "w2")), LLMResponse("done"), PASS]  # fmt: skip
    a, llm, tracer = make(tmp_path, script, policy=REVERT)
    result = a.run("fix app.txt")
    assert result.stop_reason is StopReason.COMPLETED
    assert seen == [{"app.txt": "orig", "junk.txt": None}]  # clean slate for attempt two
    assert (ws / "app.txt").read_text() == "fixed"
    assert not (ws / "junk.txt").exists()
    turn = user_texts(llm, 5)[-1]
    assert "<reverted>" in turn
    assert "app.txt, junk.txt" in turn
    [rev] = events(tracer, "gauntlet_revert")
    assert rev["restored"] == ["app.txt"]
    assert rev["deleted"] == ["junk.txt"]
    assert "+fixed" in llm.calls[7][1][0].text  # critic sees only the final attempt's diff
    assert "junk.txt" not in llm.calls[7][1][0].text
    # a later `sebastian undo` is still exact after the rewind
    ckpt = a.checkpoints
    assert ckpt is not None
    report = ckpt.undo(result.run_id, Workspace(ws))
    assert report.skipped == []
    assert (ws / "app.txt").read_text() == "orig"


def test_off_by_default_the_next_attempt_patches_forward(tmp_path: Path) -> None:
    ws = seed(tmp_path)
    seen: list[dict[str, str | None]] = []
    script: list[Step] = [PLAN, PREDICT, write("bug"), junk(), LLMResponse("done"),
              snapshot_then(ws, seen, write("fixed", "w2")), LLMResponse("done"), PASS]  # fmt: skip
    a, llm, tracer = make(tmp_path, script)
    assert a.run("t").stop_reason is StopReason.COMPLETED
    assert seen == [{"app.txt": "bug", "junk.txt": "x"}]
    assert (ws / "junk.txt").exists()
    assert "<reverted>" not in user_texts(llm, 5)[-1]
    assert not events(tracer, "gauntlet_revert")


def test_critic_failure_also_reverts(tmp_path: Path) -> None:
    ws = seed(tmp_path)
    seen: list[dict[str, str | None]] = []
    script: list[Step] = [PLAN, PREDICT, write("good"), LLMResponse("done"), FAIL,
              snapshot_then(ws, seen, LLMResponse("done")), PASS]  # fmt: skip
    a, _, _ = make(tmp_path, script, policy=REVERT)
    assert a.run("t").stop_reason is StopReason.COMPLETED
    assert seen == [{"app.txt": "orig", "junk.txt": None}]


def test_replan_request_mentions_the_revert(tmp_path: Path) -> None:
    seed(tmp_path)
    policy = GauntletPolicy(revert_on_loop=True, replan_after=1)
    script: list[Step] = [PLAN, PREDICT, write("bug"), LLMResponse("done"), PLAN, PREDICT,
              write("fixed", "w2"), LLMResponse("done"), PASS]  # fmt: skip
    a, llm, _ = make(tmp_path, script, policy=policy)
    assert a.run("t").stop_reason is StopReason.COMPLETED
    assert "<reverted>" in llm.calls[4][1][0].text


def test_help_leaves_the_last_attempt_for_inspection(tmp_path: Path) -> None:
    ws = seed(tmp_path)
    policy = GauntletPolicy(revert_on_loop=True, max_loops=1)
    a, _, tracer = make(tmp_path, [PLAN, PREDICT, write("bug"), LLMResponse("done")], policy=policy)
    assert a.run("t").stop_reason is StopReason.NEEDS_HELP
    assert (ws / "app.txt").read_text() == "bug"
    assert not events(tracer, "gauntlet_revert")


def test_nothing_written_means_no_note(tmp_path: Path) -> None:
    ws = seed(tmp_path)
    (ws / "app.txt").write_text("bug")
    seen: list[dict[str, str | None]] = []
    script: list[Step] = [PLAN, PREDICT, LLMResponse("done"),
              snapshot_then(ws, seen, write("fixed")), LLMResponse("done"), PASS]  # fmt: skip
    a, llm, tracer = make(tmp_path, script, policy=REVERT)
    assert a.run("t").stop_reason is StopReason.COMPLETED
    assert "<reverted>" not in user_texts(llm, 3)[-1]
    assert events(tracer, "gauntlet_revert")[0]["restored"] == []


def test_without_checkpoints_revert_is_skipped_and_traced(tmp_path: Path) -> None:
    seed(tmp_path)
    script: list[Step] = [PLAN, PREDICT, write("bug"), LLMResponse("done"), write("fixed", "w2"),
              LLMResponse("done"), PASS]  # fmt: skip
    a, llm, tracer = make(tmp_path, script, policy=REVERT, checkpoints=False)
    assert a.run("t").stop_reason is StopReason.COMPLETED
    assert events(tracer, "gauntlet_revert")[0]["skipped"] == "checkpoints are off"
    assert "<reverted>" not in user_texts(llm, 4)[-1]


def test_corrupt_checkpoint_is_skipped_not_fatal(tmp_path: Path) -> None:
    seed(tmp_path)

    def corrupt(_: Sequence[Message]) -> LLMResponse:
        for blob in (tmp_path / "ckpt").rglob("blobs/*"):
            blob.write_bytes(b"tampered")
        return LLMResponse("done")

    script: list[Step] = [PLAN, PREDICT, write("bug"), corrupt, write("fixed", "w2"),
              LLMResponse("done"), PASS]  # fmt: skip
    a, _, tracer = make(tmp_path, script, policy=REVERT)
    assert a.run("t").stop_reason is StopReason.COMPLETED
    assert "corrupt" in events(tracer, "gauntlet_revert")[0]["skipped"]


def test_rewind_without_a_run_or_writes_is_a_no_op(tmp_path: Path) -> None:
    store = Checkpoints(tmp_path / "c")
    assert store.rewind().restored == []
    store.begin("r1")
    assert store.rewind().deleted == []
    with pytest.raises(CheckpointError):
        store.begin("bad id!")


def test_cli_gauntlet_revert_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(home))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    ws = tmp_path / "w"
    ws.mkdir()
    from sebastian.llm import ScriptedLLM

    no_code = LLMResponse("<checks>\nfile: notes.md\n</checks>")
    script: list[Step] = [PLAN, no_code, LLMResponse("done"), FAIL, LLMResponse("done"), PASS]
    args = ["run", "t", "-w", str(ws), "--no-memory", "--gauntlet-revert"]
    assert main(args, llm_factory=lambda _m: ScriptedLLM(script)) == 0
    lines = (home / "traces.jsonl").read_text().splitlines()
    assert "gauntlet_revert" in [json.loads(x)["event"] for x in lines]
