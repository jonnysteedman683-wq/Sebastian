from __future__ import annotations

import json
from pathlib import Path

from sebastian.clock import FakeClock
from sebastian.trace import (
    REDACTED,
    JsonlTracer,
    MemoryTracer,
    RunSummary,
    _entropy,
    _looks_random,
    redact,
    redact_text,
    summarize_runs,
)


def test_memory_tracer_names() -> None:
    tracer = MemoryTracer()
    tracer.emit("r1", "event1", data=1)
    tracer.emit("r1", "event2", data=2)
    assert tracer.names() == ["event1", "event2"]


def test_jsonl_tracer(tmp_path: Path) -> None:
    clock = FakeClock()
    path = tmp_path / "traces.jsonl"
    tracer = JsonlTracer(path, clock)
    tracer.emit("run1", "event1", data="secret")

    assert path.exists()
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["run_id"] == "run1"
    assert record["event"] == "event1"
    assert record["seq"] == 1
    assert "t" in record


def test_summarize_runs_fully(tmp_path: Path) -> None:
    path = tmp_path / "traces.jsonl"

    # Write some valid and invalid data
    path.write_text(
        "invalid json\n"
        + json.dumps(["not a dict"])
        + "\n"
        + json.dumps({"event": "run_start"})
        + "\n"  # missing run_id
        + json.dumps({"run_id": "r1", "event": "run_start", "t": 100.5})
        + "\n"
        + json.dumps(
            {
                "run_id": "r1",
                "event": "run_end",
                "stop": "completed",
                "steps": 5,
                "usage": {"input": 10, "output": 20, "cost": 0.05},
            }
        )
        + "\n"
        + json.dumps({"run_id": "r2", "event": "run_start", "t": 200.0})
        + "\n"
        + json.dumps(
            {
                "run_id": "r2",
                "event": "run_end",
                "stop": "error",
                "steps": 2,
                "usage": {"input": 5, "cost": False},
            }
        )
        + "\n"  # invalid cost type
        + json.dumps({"run_id": "r3", "event": "run_start", "t": "not a float"})
        + "\n"
        + json.dumps(
            {
                "run_id": "r3",
                "event": "run_end",
                "stop": "completed",
                "steps": "not an int",
                "usage": {"input": "not an int"},
            }
        )
        + "\n"
    )

    runs = summarize_runs(path, 10)
    assert len(runs) == 3

    assert runs[0] == RunSummary(
        run_id="r1",
        started=100.5,
        stop="completed",
        steps=5,
        tokens=30,
        cost=0.05,
    )

    assert runs[1] == RunSummary(
        run_id="r2",
        started=200.0,
        stop="error",
        steps=2,
        tokens=5,  # 5 input tokens
        cost=0.0,  # default cost since provided cost was bool
    )

    assert runs[2] == RunSummary(
        run_id="r3",
        started=None,
        stop="completed",
        steps=0,  # default
        tokens=0,  # invalid type ignored
        cost=0.0,
    )


def test_summarize_runs_missing_file(tmp_path: Path) -> None:
    assert summarize_runs(tmp_path / "missing.jsonl", 10) == []


def test_entropy() -> None:
    # High entropy string
    high_ent = "aZ3kQ9xW2mN7pL5vR8tY1uB4cD6eF0gH"
    assert _entropy(high_ent) > 4.0
    # Low entropy string
    low_ent = "aaaaaaaabbbbbbbbccccccccdddddddd"
    assert _entropy(low_ent) <= 3.0


def test_looks_random() -> None:
    # Valid high entropy token
    assert _looks_random("aZ3kQ9xW2mN7pL5vR8tY1uB4cD6eF0gH")

    # Not long enough
    assert not _looks_random("aZ3kQ9xW2mN7pL5vR8tY")

    # Just hex
    assert not _looks_random("123e4567-e89b-12d3-a456-426614174000")
    assert not _looks_random("59c3206cf9bb75c65784bdd4bef2b0e19120c6aa")

    # Missing digits
    assert not _looks_random("aZkQxWmNpLvRtYuBcDeFgH" + "A" * 10)

    # Missing upper case
    assert not _looks_random("az3kq9xw2mn7pl5vr8ty1ub4cd6ef0gh")

    # Missing lower case
    assert not _looks_random("AZ3KQ9XW2MN7PL5VR8TY1UB4CD6EF0GH")

    # Low entropy (repeated characters)
    assert not _looks_random("aZ3" * 12)


def test_high_entropy_redaction() -> None:
    high_ent = "aZ3kQ9xW2mN7pL5vR8tY1uB4cD6eF0gH"

    # Standard candidate
    assert redact_text(high_ent) == REDACTED

    # Candidate with path separator
    url = f"https://example.com/api/{high_ent}/data"
    assert redact_text(url) == "https://example.[REDACTED]"

    # Non-random candidate
    non_random = "aZ3" * 12
    assert redact_text(non_random) == non_random


def test_redact_list() -> None:
    high_ent = "aZ3kQ9xW2mN7pL5vR8tY1uB4cD6eF0gH"
    data = [1, high_ent, {"key": "value"}]
    redacted_data = redact(data)
    assert redacted_data[0] == 1
    assert redacted_data[1] == REDACTED
    assert redacted_data[2] == {"key": "value"}


def test_json_decode_error(tmp_path: Path) -> None:
    path = tmp_path / "traces_err.jsonl"
    path.write_text('{"run_id": "r1", "event": "run_start", "t": 100.5}\ninvalid{json\n')
    runs = summarize_runs(path, 10)
    assert len(runs) == 1
    assert runs[0].run_id == "r1"


def test_summarize_runs_usage_no_cost(tmp_path: Path) -> None:
    path = tmp_path / "traces_usage.jsonl"
    path.write_text(
        json.dumps({"run_id": "r1", "event": "run_end", "usage": {"input": 10, "output": 20}})
        + "\n"
    )
    runs = summarize_runs(path, 10)
    assert runs[0].tokens == 30
    assert runs[0].cost == 0.0


def test_summarize_runs_limit(tmp_path: Path) -> None:
    path = tmp_path / "traces_limit.jsonl"
    path.write_text(
        json.dumps({"run_id": "r1", "event": "run_start", "t": 100})
        + "\n"
        + json.dumps({"run_id": "r2", "event": "run_start", "t": 200})
        + "\n"
        + json.dumps({"run_id": "r3", "event": "run_start", "t": 300})
        + "\n"
    )
    runs = summarize_runs(path, 2)
    assert len(runs) == 2
    assert runs[0].run_id == "r2"
    assert runs[1].run_id == "r3"


def test_summarize_runs_usage_negative_cost(tmp_path: Path) -> None:
    path = tmp_path / "traces_usage_neg.jsonl"
    path.write_text(
        json.dumps({"run_id": "r1", "event": "run_end", "usage": {"cost": -0.05}}) + "\n"
    )
    runs = summarize_runs(path, 10)
    assert runs[0].cost == 0.0


def test_null_tracer() -> None:
    from sebastian.trace import NullTracer

    tracer = NullTracer()
    tracer.emit("run", "event")  # Should do nothing


def test_summarize_runs_usage_not_dict(tmp_path: Path) -> None:
    path = tmp_path / "traces_usage_not_dict.jsonl"
    path.write_text(json.dumps({"run_id": "r1", "event": "run_end", "usage": "not a dict"}) + "\n")
    runs = summarize_runs(path, 10)
    assert runs[0].tokens == 0
