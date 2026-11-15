"""Send a finished run to Langfuse as one trace and return its link (B79, Bastion G1c-2).

Standard library only: one OTLP/HTTP JSON span to ``{host}/api/public/otel/v1/traces`` (Langfuse's
supported ingestion path; the older batch ingestion API is being retired), then the project id from
``GET {host}/api/public/projects`` to build ``{host}/project/{id}/traces/{trace_id}``.

Keys come from ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY``; ``LANGFUSE_HOST`` defaults to
Langfuse Cloud and must be https. The task and answer are redacted with the trace redactor and
capped before they leave the machine. Every failure is an ``ExportError``; callers warn, go on.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from sebastian.trace import redact_text
from sebastian.types import RunResult, StopReason

DEFAULT_HOST = "https://cloud.langfuse.com"
OTLP_PATH = "/api/public/otel/v1/traces"
PROJECTS_PATH = "/api/public/projects"
MAX_TEXT = 4000  # characters of task / answer sent
TIMEOUT_S = 10.0
_PROJECT_ID = re.compile(r"[A-Za-z0-9_-]{1,100}")

# (method, url, headers, body or None) -> (status, response body); status 0 = no response.
Send = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, bytes]]


class ExportError(Exception):
    """The run could not be sent to Langfuse, or its link could not be built."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Where and as whom to send traces."""

    host: str
    public_key: str
    secret_key: str

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Settings from the environment. Raises ``ExportError`` naming what is missing or wrong."""
        env = os.environ if env is None else env
        public, secret = env.get("LANGFUSE_PUBLIC_KEY", ""), env.get("LANGFUSE_SECRET_KEY", "")
        if not public or not secret:
            raise ExportError("set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
        host = (env.get("LANGFUSE_HOST") or DEFAULT_HOST).rstrip("/")
        parts = urlsplit(host)
        if parts.scheme != "https" or not parts.hostname or parts.path or parts.query:
            raise ExportError(
                "LANGFUSE_HOST must be an https origin such as https://cloud.langfuse.com"
            )
        return cls(host, public, secret)

    def headers(self) -> dict[str, str]:
        """Basic auth plus the v4 ingestion header Langfuse asks direct OTLP senders to set."""
        token = base64.b64encode(f"{self.public_key}:{self.secret_key}".encode()).decode()
        return {"Authorization": f"Basic {token}", "x-langfuse-ingestion-version": "4"}


def _attr(key: str, value: str | int | float) -> dict[str, Any]:
    if isinstance(value, int):
        return {
            "key": key,
            "value": {"intValue": str(value)},
        }  # OTLP JSON encodes int64 as a string
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": value}}


def _text(value: str) -> str:
    return redact_text(value)[:MAX_TEXT]


def build_payload(
    result: RunResult,
    *,
    command: str,
    model: str,
    task: str,
    start_ns: int,
    end_ns: int,
    trace_id: str,
    span_id: str,
) -> dict[str, Any]:
    """The OTLP/JSON request body: one root generation span for the whole run."""
    ok = result.stop_reason is StopReason.COMPLETED
    attrs = [
        _attr("langfuse.trace.name", f"sebastian {command}"),
        _attr("langfuse.observation.type", "generation"),
        _attr("langfuse.observation.input", _text(task)),
        _attr("langfuse.observation.output", _text(result.answer)),
        _attr("langfuse.observation.metadata.run_id", result.run_id),
        _attr("langfuse.observation.metadata.stop_reason", result.stop_reason.value),
        _attr("langfuse.observation.metadata.steps", str(result.steps)),
        _attr("gen_ai.request.model", model or "unknown"),
        _attr("gen_ai.usage.input_tokens", result.usage.input_tokens),
        _attr("gen_ai.usage.output_tokens", result.usage.output_tokens),
    ]
    if result.usage.cost > 0:  # unpriced runs send no cost, never a false $0
        attrs.append(_attr("gen_ai.usage.cost", round(result.usage.cost, 6)))
    if not ok:
        attrs.append(_attr("langfuse.observation.level", "ERROR"))
        attrs.append(
            _attr(
                "langfuse.observation.status_message",
                _text(result.detail or result.stop_reason.value),
            )
        )
    span = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": f"sebastian {command}",
        "kind": 1,  # SPAN_KIND_INTERNAL
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(max(end_ns, start_ns)),
        "attributes": attrs,
        "status": {"code": 1 if ok else 2},  # STATUS_CODE_OK / STATUS_CODE_ERROR
    }
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [_attr("service.name", "sebastian")]},
                "scopeSpans": [{"scope": {"name": "sebastian"}, "spans": [span]}],
            }
        ]
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: urllib would forward the Basic auth header to the new host (P1-19)."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None  # urllib then raises HTTPError with the 3xx code


_OPENER = urllib.request.build_opener(_NoRedirect)


def http_send(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, bytes]:
    """One HTTPS request, timed out, no redirects. Never raises: status 0 means no response."""
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers))  # noqa: S310 - https enforced
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
            return resp.status, resp.read(65536)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, OSError, ValueError):
        return 0, b""


def _project_id(settings: Settings, send: Send) -> str:
    status, body = send("GET", settings.host + PROJECTS_PATH, settings.headers(), None)
    if status != 200:
        raise ExportError(f"Langfuse projects lookup failed (HTTP {status or 'no response'})")
    try:
        pid = json.loads(body)["data"][0]["id"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ExportError("Langfuse projects lookup returned no project id") from exc
    if not isinstance(pid, str) or not _PROJECT_ID.fullmatch(pid):
        raise ExportError("Langfuse returned an unexpected project id")
    return pid


def export_run(
    result: RunResult,
    *,
    command: str,
    model: str,
    task: str,
    start_ns: int,
    end_ns: int,
    settings: Settings,
    send: Send = http_send,
    new_hex: Callable[[int], str] = secrets.token_hex,
) -> str:
    """Send the run to Langfuse and return the trace URL. Raises ``ExportError`` on failure."""
    trace_id, span_id = new_hex(16), new_hex(8)
    payload = build_payload(
        result, command=command, model=model, task=task,
        start_ns=start_ns, end_ns=end_ns, trace_id=trace_id, span_id=span_id,
    )  # fmt: skip
    headers = {**settings.headers(), "Content-Type": "application/json"}
    status, _ = send("POST", settings.host + OTLP_PATH, headers, json.dumps(payload).encode())
    if not 200 <= status < 300:
        raise ExportError(f"Langfuse rejected the trace (HTTP {status or 'no response'})")
    return f"{settings.host}/project/{_project_id(settings, send)}/traces/{trace_id}"
