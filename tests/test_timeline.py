"""B42: `sebastian show` run timelines."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from sebastian.cli import main
from sebastian.gauntlet import GauntletPolicy
from sebastian.timeline import TimelineError, build, load_events, render_html, render_text
from sebastian.trace import JsonlTracer
from test_gauntlet_run import FAIL, PASS, PLAN, PREDICT, make, write


def gauntlet_trace(tmp_path: Path) -> tuple[Path, str]:
    """A real Gauntlet run (fail, retry, pass) traced to a JSONL file."""
    from sebastian.clock import FakeClock

    path = tmp_path / "traces.jsonl"
    script = [PLAN, PREDICT, write("bug"), PASS.__class__("done"), write("ok", "w2"),
              PASS.__class__("done"), FAIL, PASS.__class__("done"), PASS]  # fmt: skip
    agent, _, _ = make(tmp_path, script, policy=GauntletPolicy(replan_after=5))
    agent.tracer = JsonlTracer(path, FakeClock())
    return path, agent.run("fix <app.txt> & check").run_id


def test_build_covers_every_event_kind(tmp_path: Path) -> None:
    path, run_id = gauntlet_trace(tmp_path)
    full, events = load_events(path, run_id)
    assert full == run_id
    titles = [e.title for e in build(events)]
    assert titles[0] == "Task"
    assert "Plan" in titles
    assert "Predict: accepted" in titles
    assert "Verify: failed" in titles
    assert "Verify: passed" in titles
    assert "Critique: FAIL" in titles
    assert "Critique: PASS" in titles
    assert "Step 3: tools write_file" in titles  # plan and predict are steps 1-2
    assert "write_file ok" in titles
    assert titles[-1] == "Ended: completed"


def test_prefix_lookup_and_errors(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    with pytest.raises(TimelineError, match="no trace file"):
        load_events(path, "abc")
    lines = [{"run_id": "abcdef01", "event": "run_start", "t": 1, "seq": 1},
             {"run_id": "abcdef02", "event": "run_start", "t": 1, "seq": 2},
             {"run_id": "zzzzzz99", "event": "run_start", "t": 1, "seq": 3}]  # fmt: skip
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n[1]\n")
    assert load_events(path, "zzzzzz")[0] == "zzzzzz99"
    assert load_events(path, "abcdef01")[0] == "abcdef01"
    with pytest.raises(TimelineError, match="matches 2 runs"):
        load_events(path, "abcdef")
    with pytest.raises(TimelineError, match="at least 6"):
        load_events(path, "zz")
    with pytest.raises(TimelineError, match="no run starting"):
        load_events(path, "qqqqqqq")


def test_html_escapes_untrusted_trace_content() -> None:
    events = [
        {"run_id": "r", "seq": 1, "t": 0, "event": "run_start", "task": "<script>x</script>",
         "resumed_from": "old"},
        {"run_id": "r", "seq": 2, "t": 1.5, "event": "llm_step", "step": 1,
         "text": "<img onerror=alert(1)>" + "y" * 2000, "tool_calls": [],
         "usage": {"input": 1, "output": 2, "cost": 0.5}},
        {"run_id": "r", "seq": 3, "t": 2, "event": "approval_denied", "tool": "run_command",
         "args": {"command": "rm -rf /"}, "stage": "verify"},
        {"run_id": "r", "seq": 4, "t": 3, "event": "mystery"},
    ]  # fmt: skip
    entries = build(events)
    page = render_html("r", entries, 0.0)
    assert "<script>x" not in page
    assert "&lt;script&gt;" in page
    assert "<img" not in page
    assert "more chars in the trace" in page
    assert "$0.5000" in page
    assert "Denied: run_command (verify)" in page
    assert "resumed from old" in page
    assert "still running or interrupted" in page
    assert "<script" not in page.replace("&lt;script", "")
    assert entries[1].offset == 1.5
    assert entries[-1].title == "mystery"


def test_text_rendering_and_other_events() -> None:
    kinds: list[dict[str, Any]] = [
        {"event": "tool_error", "tool": "read_file", "error": "nope"},
        {"event": "tools_parallel", "count": 3},
        {"event": "context_compacted", "elided": 2},
        {"event": "replan_nudge", "errors": 3},
        {"event": "escalated", "step": 4},
        {"event": "budget_warning", "limit": "tokens", "used": 9, "max": 10},
        {"event": "gauntlet_predict", "passed": False, "error": "no checks"},
        {
            "event": "gauntlet_verify",
            "passed": False,
            "checks": [{"command": "pytest", "passed": False, "denied": True}, "junk"],
        },
        {"event": "gauntlet_critique", "passed": False, "malformed": True, "findings": []},
        {"event": "gauntlet_stage", "stage": "help", "loops": 3, "why": "limit"},
        {"event": "gauntlet_revert", "restored": ["a"], "deleted": ["b"]},
        {"event": "gauntlet_revert", "skipped": "checkpoints are off"},
        {"event": "gauntlet_verify", "passed": True, "checks": []},
        {"event": "gauntlet_help", "stage": "verify", "why": "denied"},
        {"event": "run_end", "stop": "needs_help", "steps": 4, "detail": "denied"},
    ]
    events = [{"run_id": "r", "seq": i, "t": i, **e} for i, e in enumerate(kinds)]
    events.append({"run_id": "r", "event": "plan", "text": "p", "usage": "odd"})  # no t/seq
    text = render_text("r", build(events))
    for needle in ("read_file failed", "3 read tools ran in parallel", "2 old results elided",
                   "after 3 failed calls", "step 4", "Budget warning: tokens", "Predict: rejected",
                   "denied  pytest", "FAIL (malformed)", "Next: help (round 3)",
                   "Reverted failed attempt", "a, b", "Revert skipped", "no checks predicted",
                   "Needs help at verify", "Ended: needs_help"):  # fmt: skip
        assert needle in text, needle


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("SEBASTIAN_HOME", str(h))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    return h


def test_cli_show(home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
                  monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    path, run_id = gauntlet_trace(tmp_path)
    (home / "traces.jsonl").write_text(path.read_text())
    monkeypatch.chdir(tmp_path)
    assert main(["show", run_id[:8]]) == 0
    out = Path(capsys.readouterr().out.strip())
    assert out.name == f"sebastian-run-{run_id[:12]}.html"
    assert "Critique: PASS" in (tmp_path / out).read_text()
    custom = tmp_path / "x.html"
    assert main(["show", run_id, "-o", str(custom)]) == 0
    assert custom.exists()
    capsys.readouterr()
    assert main(["show", run_id, "--text"]) == 0
    assert "Verify: failed" in capsys.readouterr().out
    assert main(["show", "nope"]) == 1
    assert "at least 6" in capsys.readouterr().err
