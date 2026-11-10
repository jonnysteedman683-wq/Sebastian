"""B79: send a finished run to Langfuse over OTLP/JSON and return its trace link. No network."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from sebastian import langfuse
from sebastian.cli import main
from sebastian.types import RunResult, StopReason, Usage

ENV = {"LANGFUSE_PUBLIC_KEY": "pk-lf-1", "LANGFUSE_SECRET_KEY": "sk-lf-2"}
PROJECTS = json.dumps({"data": [{"id": "proj_1", "name": "bastion"}]}).encode()


class FakeLangfuse:
    """Records requests; answers the OTLP POST and the projects GET."""

    def __init__(
        self, post_status: int = 200, projects: tuple[int, bytes] = (200, PROJECTS)
    ) -> None:
        self.post_status = post_status
        self.projects = projects
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        self.calls.append((method, url, dict(headers), body))
        return (self.post_status, b"{}") if method == "POST" else self.projects

    def span(self) -> dict[str, Any]:
        body = self.calls[0][3]
        assert body is not None
        return json.loads(body)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]  # type: ignore[no-any-return]


def attrs(span: dict[str, Any]) -> dict[str, Any]:
    return {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}


def result(
    stop: StopReason = StopReason.COMPLETED, cost: float = 0.0, answer: str = "done"
) -> RunResult:
    return RunResult(
        "run-1",
        stop,
        answer,
        3,
        Usage(120, 30, cost),
        detail="boom" if stop is not StopReason.COMPLETED else "",
    )


def export(
    fake: FakeLangfuse, res: RunResult | None = None, env: Mapping[str, str] = ENV, task: str = "t"
) -> str:
    ids = iter(["a" * 32, "b" * 16])
    return langfuse.export_run(
        res or result(),
        command="run",
        model="claude-haiku-5-5",
        task=task,
        start_ns=1_000,
        end_ns=2_000,
        settings=langfuse.Settings.from_env(env),
        send=fake,
        new_hex=lambda _n: next(ids),
    )


def test_run_is_sent_as_one_otlp_span_and_the_trace_link_is_returned() -> None:
    fake = FakeLangfuse()
    url = export(fake)
    assert url == "https://cloud.langfuse.com/project/proj_1/traces/" + "a" * 32
    method, endpoint, headers, _ = fake.calls[0]
    assert (method, endpoint) == ("POST", "https://cloud.langfuse.com/api/public/otel/v1/traces")
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"pk-lf-1:sk-lf-2").decode()
    assert headers["x-langfuse-ingestion-version"] == "4"
    assert headers["Content-Type"] == "application/json"
    assert fake.calls[1][:2] == ("GET", "https://cloud.langfuse.com/api/public/projects")
    span = fake.span()
    assert (span["traceId"], span["spanId"]) == ("a" * 32, "b" * 16)
    assert (span["startTimeUnixNano"], span["endTimeUnixNano"]) == ("1000", "2000")
    assert span["status"] == {"code": 1}
    a = attrs(span)
    assert a["langfuse.trace.name"] == "sebastian run"
    assert a["langfuse.observation.type"] == "generation"
    assert a["gen_ai.request.model"] == "claude-haiku-5-5"
    assert (a["gen_ai.usage.input_tokens"], a["gen_ai.usage.output_tokens"]) == ("120", "30")
    assert a["langfuse.observation.metadata.run_id"] == "run-1"
    assert "gen_ai.usage.cost" not in a  # unpriced: no false $0
    assert "langfuse.observation.level" not in a


def test_priced_and_failed_runs_carry_cost_and_an_error_level() -> None:
    fake = FakeLangfuse()
    export(fake, result(StopReason.ERROR, cost=0.0123))
    span = fake.span()
    a = attrs(span)
    assert a["gen_ai.usage.cost"] == 0.0123
    assert a["langfuse.observation.level"] == "ERROR"
    assert a["langfuse.observation.status_message"] == "boom"
    assert span["status"] == {"code": 2}


def test_secrets_in_the_task_and_answer_are_redacted_and_capped() -> None:
    fake = FakeLangfuse()
    key = "sk-ant-" + "x" * 30
    export(fake, result(answer="y" * 10_000), task=f"use {key}")
    a = attrs(fake.span())
    assert key not in a["langfuse.observation.input"]
    assert "[REDACTED]" in a["langfuse.observation.input"]
    assert len(a["langfuse.observation.output"]) == langfuse.MAX_TEXT


def test_self_hosted_host_is_used_for_both_calls_and_the_link() -> None:
    fake = FakeLangfuse()
    url = export(fake, env={**ENV, "LANGFUSE_HOST": "https://lf.example.net/"})
    assert url.startswith("https://lf.example.net/project/proj_1/traces/")
    assert all(call[1].startswith("https://lf.example.net/api/public/") for call in fake.calls)


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"LANGFUSE_PUBLIC_KEY": "pk"},
        {**ENV, "LANGFUSE_HOST": "http://lf.example.net"},
        {**ENV, "LANGFUSE_HOST": "https://lf.example.net/path"},
    ],
)
def test_missing_keys_or_a_non_https_host_are_refused(env: dict[str, str]) -> None:
    with pytest.raises(langfuse.ExportError):
        langfuse.Settings.from_env(env)


@pytest.mark.parametrize(
    "fake",
    [
        FakeLangfuse(post_status=401),
        FakeLangfuse(post_status=0),
        FakeLangfuse(projects=(500, b"")),
        FakeLangfuse(projects=(200, b"not json")),
        FakeLangfuse(projects=(200, b'{"data": []}')),
        FakeLangfuse(projects=(200, b'{"data": [{"id": "../evil"}]}')),
    ],
)
def test_every_failure_is_an_export_error(fake: FakeLangfuse) -> None:
    with pytest.raises(langfuse.ExportError):
        export(fake)


def test_http_send_reports_no_response_instead_of_raising() -> None:
    assert langfuse.http_send("GET", "https://127.0.0.1:1/x", {}, None)[0] == 0


def test_http_send_refuses_redirects_so_the_keys_never_reach_another_host() -> None:
    import http.server
    import threading
    import urllib.request

    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    try:
        # Plain http on loopback only to exercise the opener; Settings never allows http.
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/start", headers={"Authorization": "x"}
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            langfuse._OPENER.open(req, timeout=5)
        assert exc.value.code == 302
    finally:
        server.server_close()
    assert seen == ["/start"]  # the redirect target was never requested


# CLI: --langfuse puts the trace link in --usage-out and never changes the exit code.


def test_demo_langfuse_writes_the_trace_link_into_the_usage_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeLangfuse()
    monkeypatch.setattr(langfuse, "http_send", fake)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    ws, out = tmp_path / "ws", tmp_path / "usage.json"
    ws.mkdir()
    assert main(["demo", "-w", str(ws), "--usage-out", str(out), "--langfuse"]) == 0
    usage = json.loads(out.read_text(encoding="utf-8"))
    assert usage["trace_url"].startswith("https://cloud.langfuse.com/project/proj_1/traces/")
    assert (usage["model"], usage["input_tokens"], usage["output_tokens"]) == ("demo", 0, 0)
    assert attrs(fake.span())["langfuse.trace.name"] == "sebastian demo"


def test_langfuse_failure_warns_and_keeps_the_exit_code_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    ws, out = tmp_path / "ws", tmp_path / "usage.json"
    ws.mkdir()
    assert main(["demo", "-w", str(ws), "--usage-out", str(out), "--langfuse"]) == 0
    assert "--langfuse" in capsys.readouterr().err
    usage = json.loads(out.read_text(encoding="utf-8"))
    assert "trace_url" not in usage
    assert usage["model"] == "demo"


def test_without_the_flag_nothing_is_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeLangfuse()
    monkeypatch.setattr(langfuse, "http_send", fake)
    ws, out = tmp_path / "ws", tmp_path / "usage.json"
    ws.mkdir()
    assert main(["demo", "-w", str(ws), "--usage-out", str(out)]) == 0
    assert fake.calls == []
    assert "trace_url" not in json.loads(out.read_text(encoding="utf-8"))
