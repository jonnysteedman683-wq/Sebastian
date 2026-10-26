from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from sebastian import crosstest as ct
from sebastian.cli import main
from sebastian.clock import FakeClock

# A fake agent: the task text is "write <path> <content>"; it writes that file and exits 0.
GOOD = """\
import pathlib, sys
task = pathlib.Path(sys.argv[1]).read_text()
_, path, content = task.split(" ", 2)
pathlib.Path(path).write_text(content)
"""
LAZY = "import sys\nsys.exit(0)\n"
SLOW = "import time\ntime.sleep(30)\n"
CRASH = "import sys\nprint('boom')\nsys.exit(3)\n"


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("SEBASTIAN_HOME", str(h))
    return h


def case(
    name: str, task: str, expect: dict[str, Any], files: dict[str, str] | None = None
) -> dict[str, Any]:
    return {"version": 1, "name": name, "task": task, "files": files or {}, "expect": expect}


def eco(tmp_path: Path, agents: dict[str, str], suites: dict[str, list[dict[str, Any]]]) -> Path:
    """Write fake agent scripts, their suites and a manifest; return the manifest path."""
    entries = []
    for name, source in agents.items():
        script = tmp_path / f"{name}.py"
        script.write_text(source)
        suite = tmp_path / "suites" / name
        suite.mkdir(parents=True)
        for spec in suites.get(name, []):
            (suite / f"{spec['name']}.json").write_text(json.dumps(spec))
        entries.append(
            {
                "name": name,
                "suite": f"suites/{name}",
                "command": [sys.executable, str(script), "{task_file}"],
                "timeout": 20,
            }
        )
    manifest = tmp_path / "agents.json"
    manifest.write_text(json.dumps({"version": 1, "agents": entries}))
    return manifest


# --- manifest and cases ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ({"version": 2, "agents": []}, "version 1"),
        ({"version": 1, "agents": []}, "1..20 agents"),
        ({"version": 1, "agents": ["x"]}, "must be an object"),
        ({"version": 1, "agents": [{"name": "Bad Name", "suite": "s", "command": ["a"]}]}, "name"),
        ({"version": 1, "agents": [{"name": "a", "suite": "", "command": ["x"]}]}, "suite"),
        ({"version": 1, "agents": [{"name": "a", "suite": "s", "command": "x {task}"}]}, "list"),
        ({"version": 1, "agents": [{"name": "a", "suite": "s", "command": ["x"]}]}, "{task}"),
        (
            {"version": 1, "agents": [{"name": "a", "suite": "s", "command": ["x", "{task}"],
                                       "timeout": 0}]},
            "timeout",
        ),
        (
            {"version": 1, "agents": [{"name": "a", "suite": "s", "command": ["x", "{task}"],
                                       "push": True}]},
            "unknown key",
        ),
        (
            {"version": 1, "agents": [{"name": "a", "suite": "s", "command": ["x", "{task}"]}] * 2},
            "unique",
        ),
    ],
)  # fmt: skip
def test_manifest_validation(tmp_path: Path, raw: dict[str, Any], match: str) -> None:
    path = tmp_path / "m.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ct.CrossTestError, match=match):
        ct.load_manifest(path)


def test_unreadable_manifest(tmp_path: Path) -> None:
    (tmp_path / "m.json").write_text("{not json")
    with pytest.raises(ct.CrossTestError, match="cannot read"):
        ct.load_manifest(tmp_path / "m.json")


def test_suite_paths_resolve_relative_to_manifest(tmp_path: Path) -> None:
    manifest = eco(tmp_path, {"good": GOOD}, {})
    (agent,) = ct.load_manifest(manifest)
    assert agent.suite == (tmp_path / "suites" / "good").resolve()
    assert agent.timeout == 20


@pytest.mark.parametrize(
    ("spec", "match"),
    [
        ({"version": 2}, "version 1"),
        (case("Bad", "t", {}), "case name"),
        (case("a", " ", {}), "task"),
        (case("a", "t", {}, {"../x": "y"}), "escapes"),
        (case("a", "t", {}, {"/etc/x": "y"}), "escapes"),
        (case("a", "t", {"files": {"../x": "y"}}), "escapes"),
        (case("a", "t", {"absent": "x"}), "absent"),
        (case("a", "t", {"exit_code": True}), "exit_code"),
        (case("a", "t", {"contains": ["x"]}), "contains"),
        ({**case("a", "t", {}), "expect": []}, "expect must be"),
        (case("a", "t", {}, {"big": "x" * (ct.MAX_SEED_BYTES + 1)}), "exceed"),
    ],
)
def test_case_validation(spec: dict[str, Any], match: str) -> None:
    with pytest.raises(ct.CrossTestError, match=match):
        ct.parse_case(spec)


def test_scar_files_load_and_unobservable_keys_are_reported() -> None:
    c = ct.parse_case(case("scar-x", "t", {"stop": "completed", "no_gated_success": True}))
    assert c.ignored == ("no_gated_success", "stop")


def test_bad_case_file_names_the_file(tmp_path: Path) -> None:
    (tmp_path / "broken.json").write_text("{")
    with pytest.raises(ct.CrossTestError, match=r"broken\.json"):
        ct.load_suite(tmp_path)
    assert ct.load_suite(tmp_path / "missing") == []


# --- checking -------------------------------------------------------------------------------


def test_check_case_statuses(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello world")
    exact = ct.parse_case(case("c", "t", {"files": {"a.txt": "hello world"}, "exit_code": 0}))
    assert ct.check_case(exact, tmp_path, 0) == ("pass", "")
    status, detail = ct.check_case(exact, tmp_path, 2)
    assert status == "fail"
    assert "exit code 2" in detail
    mixed = ct.parse_case(
        case("c", "t", {"contains": {"a.txt": "nope", "b.txt": "x"}, "absent": ["a.txt"]})
    )
    status, detail = ct.check_case(mixed, tmp_path, 0)
    assert status == "fail"
    assert "a.txt lacks 'nope'" in detail
    assert "b.txt lacks 'x'" in detail
    assert "a.txt exists" in detail
    nothing = ct.parse_case(case("c", "t", {"stop": "completed"}))
    assert ct.check_case(nothing, tmp_path, 0) == ("unchecked", "no checkable expectation (stop)")
    assert ct.check_case(ct.parse_case(case("c", "t", {})), tmp_path, 0)[0] == "unchecked"


def test_symlinked_output_does_not_count(tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_text("x")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    c = ct.parse_case(case("c", "t", {"files": {"link.txt": "x"}}))
    assert ct.check_case(c, tmp_path, 0)[0] == "fail"


def test_build_argv_fills_placeholders_without_a_shell() -> None:
    argv = ct.build_argv(
        ["agent", "run", "{task}", "--file={task_file}", "-w", "{workspace}"],
        "do $(rm -rf ~); echo hi",
        Path("/t/TASK.md"),
        Path("/t/ws"),
    )
    assert argv == ["agent", "run", "do $(rm -rf ~); echo hi", "--file=/t/TASK.md", "-w", "/t/ws"]


# --- running --------------------------------------------------------------------------------


def test_matrix_runs_every_suite_against_every_agent(tmp_path: Path) -> None:
    suites = {
        "good": [case("one", "write out.txt 1", {"files": {"out.txt": "1"}})],
        "lazy": [
            case("two", "write b.txt 2", {"contains": {"b.txt": "2"}}),
            case("seeded", "write s.txt ok", {"files": {"s.txt": "ok", "seed.txt": "kept"}},
                 {"seed.txt": "kept"}),
            case("scar", "write x.txt x", {"stop": "completed"}),
        ],
    }  # fmt: skip
    agents = ct.load_manifest(eco(tmp_path, {"good": GOOD, "lazy": LAZY}, suites))
    seen: list[ct.CaseResult] = []
    m = ct.run_matrix(agents, clock=FakeClock(), on_result=seen.append)
    assert len(m.results) == len(seen) == 8  # 4 cases x 2 targets
    assert m.cell("good", "good") == (1, 1)
    assert m.cell("good", "lazy") == (2, 2)  # the scar-only case is unchecked, not counted
    assert m.cell("lazy", "good") == (0, 1)
    assert m.cell("lazy", "lazy") == (0, 2)
    table = m.render()
    assert "1/1" in table
    assert "0/2" in table
    assert "lazy on good/one: fail" in table
    assert "unchecked" in table
    data = m.to_json()
    assert data["agents"] == ["good", "lazy"]
    assert {"suite", "target", "case", "status", "detail", "seconds"} == set(data["results"][0])
    assert "output" not in data["results"][0]


def test_filters_limit_targets_and_suites(tmp_path: Path) -> None:
    suites = {"good": [case("one", "write a 1", {"files": {"a": "1"}})],
              "lazy": [case("two", "write b 2", {"files": {"b": "2"}})]}  # fmt: skip
    agents = ct.load_manifest(eco(tmp_path, {"good": GOOD, "lazy": LAZY}, suites))
    todo = ct.plan(agents, {"lazy"}, {"good"})
    assert [(s.name, t.name, c.name) for s, t, c in todo] == [("good", "lazy", "one")]
    m = ct.run_matrix(agents, {"lazy"}, {"good"})
    assert m.cell("good", "lazy") == (0, 0)
    assert "-" in m.render()


def test_timeout_and_crash_and_missing_executable(tmp_path: Path) -> None:
    c = ct.parse_case(case("c", "t", {"exit_code": 0}))
    slow = ct.Agent("slow", tmp_path, (sys.executable, "-c", SLOW, "{task}"), timeout=0.5)
    r = ct.run_case("s", slow, c)
    assert r.status == "timeout"
    crash = ct.Agent("crash", tmp_path, (sys.executable, "-c", CRASH, "{task}"))
    r = ct.run_case("s", crash, c)
    assert r.status == "fail"
    assert "exit code 3" in r.detail
    assert "boom" in r.output
    gone = ct.Agent("gone", tmp_path, (str(tmp_path / "no-such-agent"), "{task}"))
    r = ct.run_case("s", gone, c)
    assert r.status == "error"
    assert "could not start" in r.detail


def test_each_case_gets_a_fresh_workspace_that_is_removed(tmp_path: Path) -> None:
    seen: list[tuple[list[str], Path]] = []

    def runner(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int | None, str]:
        seen.append((list(argv), cwd))
        assert (cwd / "seed.txt").read_text() == "s"
        assert not (cwd / ct.TASK_FILE).exists()  # the task file sits outside the workspace
        assert Path(argv[1]).read_text() == "the task"
        (cwd / "seed.txt").write_text("changed")
        return 0, ""

    agent = ct.Agent("a", tmp_path, ("agent", "{task_file}", "{workspace}"))
    c = ct.parse_case(case("c", "the task", {"files": {"seed.txt": "s"}}, {"seed.txt": "s"}))
    r1 = ct.run_case("s", agent, c, runner)
    r2 = ct.run_case("s", agent, c, runner)
    assert r1.status == r2.status == "fail"  # each run starts from the seed again
    assert seen[0][1] != seen[1][1]
    assert seen[0][0][2] == str(seen[0][1])
    assert not seen[0][1].exists()


def test_long_output_is_capped(tmp_path: Path) -> None:
    loud = ct.Agent("loud", tmp_path, (sys.executable, "-c", "print('x' * 100000)", "{task}"))
    r = ct.run_case("s", loud, ct.parse_case(case("c", "t", {"exit_code": 0})))
    assert r.status == "pass"
    assert len(r.output) <= ct.MAX_OUTPUT


# --- CLI ------------------------------------------------------------------------------------


def test_cli_dry_run_runs_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    suites = {"good": [case("one", "write ran.txt 1", {"files": {"ran.txt": "1"}})]}
    manifest = eco(tmp_path, {"good": GOOD, "lazy": LAZY}, suites)
    assert main(["crosstest", "--manifest", str(manifest), "--dry-run"]) == 0
    out = capsys.readouterr()
    assert "good <- good/one" in out.out
    assert "lazy <- good/one" in out.out
    assert "2 case run(s)" in out.err


def test_cli_runs_writes_json_and_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    suites = {"good": [case("one", "write out.txt 1", {"files": {"out.txt": "1"}})]}
    manifest = eco(tmp_path, {"good": GOOD, "lazy": LAZY}, suites)
    out = tmp_path / "res.json"
    args = ["crosstest", "--manifest", str(manifest), "--json", str(out)]
    assert main([*args, "--target", "good"]) == 0
    assert json.loads(out.read_text())["results"][0]["status"] == "pass"
    assert main(args) == 1  # lazy fails good's case
    assert "lazy on good/one: fail" in capsys.readouterr().out


def test_cli_rejects_bad_manifest_and_unknown_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = eco(tmp_path, {"good": GOOD}, {})
    assert main(["crosstest", "--manifest", str(manifest), "--target", "nobody"]) == 2
    assert "no agent named 'nobody'" in capsys.readouterr().err
    assert main(["crosstest", "--manifest", str(tmp_path / "missing.json")]) == 2
