"""``sebastian show <run_id>`` (B42): one run's trace as a readable timeline.

Reads only ``traces.jsonl`` (already redacted when written). Writes a self-contained HTML page,
or prints plain text with ``--text``. No JavaScript, no external assets; every value from the
trace is HTML-escaped, because model output and tool arguments are untrusted.
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MIN_PREFIX = 6
MAX_TEXT = 1200  # chars of model text shown per step; the full text stays in the trace file


class TimelineError(ValueError):
    """The run cannot be found or the id is ambiguous."""


@dataclass(frozen=True, slots=True)
class RunStats:
    """Stats for a single run to show in comparisons."""

    stop: str
    steps: int
    tokens: int
    cost: float
    gauntlet_loops: int


def _extract_stats(events: Sequence[dict[str, Any]]) -> RunStats:
    stop = "running"
    steps = 0
    tokens = 0
    cost = 0.0
    gauntlet_loops = 0
    for e in events:
        ev = e.get("event")
        if ev == "run_end":
            stop = str(e.get("stop", stop))
            steps = e.get("steps", steps) if isinstance(e.get("steps"), int) else steps
            usage = e.get("usage")
            if isinstance(usage, dict):
                tokens = sum(
                    v for k, v in usage.items() if k in {"input", "output"} and type(v) is int
                )
                c = usage.get("cost")
                if isinstance(c, int | float) and not isinstance(c, bool) and c >= 0:
                    cost = float(c)
        elif ev == "gauntlet_stage":
            loops = e.get("loops", 0)
            if isinstance(loops, int) and loops > gauntlet_loops:
                gauntlet_loops = loops
    return RunStats(stop, steps, tokens, cost, gauntlet_loops)


def find_first_diff_step(
    events_a: Sequence[dict[str, Any]], events_b: Sequence[dict[str, Any]]
) -> int | None:
    """Find the step number (1-based) where the tool calls first differ.

    Compares `llm_step` events in order. If they match completely but one run
    has more steps, the difference is the first extra step.
    """
    calls_a = [
        e.get("tool_calls", [])
        for e in sorted(events_a, key=lambda x: x.get("seq", 0))
        if e.get("event") == "llm_step"
    ]
    calls_b = [
        e.get("tool_calls", [])
        for e in sorted(events_b, key=lambda x: x.get("seq", 0))
        if e.get("event") == "llm_step"
    ]

    for i, (ca, cb) in enumerate(zip(calls_a, calls_b, strict=False)):
        if ca != cb:
            return i + 1
    if len(calls_a) != len(calls_b):
        return min(len(calls_a), len(calls_b)) + 1
    return None


@dataclass(frozen=True, slots=True)
class Entry:
    """One timeline row."""

    offset: float  # seconds since run_start (0 when unknown)
    kind: str  # css class / badge: model, tool, error, gate, gauntlet, budget, end, start, info
    title: str
    detail: str = ""


def load_events(path: str | Path, run_id: str) -> tuple[str, list[dict[str, Any]]]:
    """Events for the run whose id is ``run_id`` or starts with it (at least 6 characters)."""
    events: dict[str, list[dict[str, Any]]] = {}
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("run_id"), str):
                    events.setdefault(rec["run_id"], []).append(rec)
    except FileNotFoundError:
        raise TimelineError(f"no trace file at {path}") from None
    if run_id in events:
        return run_id, events[run_id]
    if len(run_id) < MIN_PREFIX:
        raise TimelineError(f"no run {run_id!r}; give the full id or at least {MIN_PREFIX} chars")
    matches = [r for r in events if r.startswith(run_id)]
    if not matches:
        raise TimelineError(f"no run starting with {run_id!r}")
    if len(matches) > 1:
        raise TimelineError(f"{run_id!r} matches {len(matches)} runs; give more of the id")
    return matches[0], events[matches[0]]


def _clip(text: Any, limit: int = MAX_TEXT) -> str:
    s = str(text)
    return s if len(s) <= limit else s[:limit] + f"... [{len(s) - limit} more chars in the trace]"


def _usage(u: Any) -> str:
    if not isinstance(u, dict):
        return ""
    bits = [f"{u.get('input', 0):,} in / {u.get('output', 0):,} out"]
    if isinstance(u.get("cost"), int | float):
        bits.append(f"${u['cost']:.4f}")
    return ", ".join(bits)


def _entry(e: dict[str, Any], offset: float) -> Entry | None:  # noqa: PLR0911, PLR0912
    ev = e.get("event")
    if ev == "run_start":
        note = f"resumed from {e['resumed_from']}" if e.get("resumed_from") else ""
        return Entry(
            offset, "start", "Task", _clip(e.get("task", "")) + (f"\n{note}" if note else "")
        )
    if ev == "plan":
        return Entry(
            offset, "model", "Plan", _clip(e.get("text", "")) + f"\n{_usage(e.get('usage'))}"
        )
    if ev == "llm_step":
        calls = ", ".join(map(str, e.get("tool_calls") or [])) or "none (claims done)"
        text = _clip(e.get("text", ""))
        return Entry(offset, "model", f"Step {e.get('step', '?')}: tools {calls}",
                     f"{text}\n{_usage(e.get('usage'))}".strip())  # fmt: skip
    if ev == "tool_ok":
        return Entry(offset, "tool", f"{e.get('tool')} ok", f"{e.get('chars', 0):,} chars returned")
    if ev == "tool_error":
        return Entry(offset, "error", f"{e.get('tool')} failed", _clip(e.get("error", ""), 400))
    if ev == "approval_denied":
        where = f" ({e['stage']})" if e.get("stage") else ""
        args = json.dumps(e.get("args", {}), ensure_ascii=False, default=str)
        return Entry(offset, "gate", f"Denied: {e.get('tool')}{where}", _clip(args, 400))
    if ev == "tools_parallel":
        return Entry(offset, "info", f"{e.get('count')} read tools ran in parallel")
    if ev == "context_compacted":
        return Entry(offset, "info", f"Context compacted ({e.get('elided')} old results elided)")
    if ev == "replan_nudge":
        return Entry(offset, "info", f"Replan nudge after {e.get('errors')} failed calls")
    if ev == "escalated":
        return Entry(offset, "info", f"Escalation model used for step {e.get('step')}")
    if ev == "budget_warning":
        return Entry(offset, "budget", f"Budget warning: {e.get('limit')}",
                     f"{e.get('used')} of {e.get('max')}")  # fmt: skip
    if ev == "gauntlet_predict":
        checks = "\n".join(
            [*(f"run: {c}" for c in e.get("commands") or []),
             *(f"file: {f}" for f in e.get("files") or [])]
        )  # fmt: skip
        title = "Predict: accepted" if e.get("passed") else "Predict: rejected"
        return Entry(offset, "gauntlet", title, checks or _clip(e.get("error", ""), 400))
    if ev == "gauntlet_verify":
        rows = [
            f"{'denied' if c.get('denied') else 'pass' if c.get('passed') else 'FAIL'}  "
            f"{c.get('command')}"
            for c in e.get("checks") or []
            if isinstance(c, dict)
        ]
        title = "Verify: passed" if e.get("passed") else "Verify: failed"
        return Entry(offset, "gauntlet", title, "\n".join(rows) or "no checks predicted")
    if ev == "gauntlet_critique":
        verdict = (
            "PASS" if e.get("passed") else "FAIL" + (" (malformed)" if e.get("malformed") else "")
        )
        findings = "\n".join(f"- {f}" for f in e.get("findings") or [])
        return Entry(offset, "gauntlet", f"Critique: {verdict}", findings)
    if ev == "gauntlet_stage":
        return Entry(offset, "gauntlet", f"Next: {e.get('stage')} (round {e.get('loops')})",
                     str(e.get("why", "")))  # fmt: skip
    if ev == "gauntlet_revert":
        if e.get("skipped"):
            return Entry(offset, "gauntlet", "Revert skipped", str(e["skipped"]))
        files = [*(e.get("restored") or []), *(e.get("deleted") or [])]
        return Entry(offset, "gauntlet", "Reverted failed attempt", ", ".join(map(str, files)))
    if ev == "gauntlet_help":
        return Entry(offset, "error", f"Needs help at {e.get('stage')}", str(e.get("why", "")))
    if ev == "run_end":
        detail = "\n".join(
            x for x in (str(e.get("detail") or ""), _usage(e.get("usage")),
                        f"{e.get('steps', 0)} model calls") if x
        )  # fmt: skip
        return Entry(offset, "end", f"Ended: {e.get('stop')}", detail)
    return Entry(offset, "info", str(ev))


def build(events: Sequence[dict[str, Any]]) -> list[Entry]:
    """Timeline rows in trace order, with times relative to the run's start."""
    ordered = sorted(events, key=lambda e: e.get("seq") or 0)
    starts = [e["t"] for e in ordered if isinstance(e.get("t"), int | float)]
    t0 = min(starts) if starts else 0.0
    out = []
    for e in ordered:
        t = e.get("t")
        entry = _entry(e, float(t) - t0 if isinstance(t, int | float) else 0.0)
        if entry is not None:
            out.append(entry)
    return out


def render_text(run_id: str, entries: Sequence[Entry]) -> str:
    """Plain-text timeline for a terminal."""
    lines = [f"run {run_id}"]
    for en in entries:
        lines.append(f"+{en.offset:7.1f}s  [{en.kind}] {en.title}")
        lines.extend(f"            {ln}" for ln in en.detail.splitlines() if ln.strip())
    return "\n".join(lines)


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_comparison_text(
    run_a: str,
    entries_a: Sequence[Entry],
    stats_a: RunStats,
    run_b: str,
    entries_b: Sequence[Entry],
    stats_b: RunStats,
    diff_step: int | None,
) -> str:
    """Plain-text comparison for two timelines."""
    lines = [
        f"Comparing {run_a} vs {run_b}",
        f"Summary: A stopped with {stats_a.stop} after {stats_a.steps} steps "
        f"(${stats_a.cost:.4f}, {stats_a.tokens:,} tokens, "
        f"{stats_a.gauntlet_loops} Gauntlet loops)",
        f"         B stopped with {stats_b.stop} after {stats_b.steps} steps "
        f"(${stats_b.cost:.4f}, {stats_b.tokens:,} tokens, "
        f"{stats_b.gauntlet_loops} Gauntlet loops)",
    ]
    if diff_step is not None:
        lines.append(f"Tool calls first differ at step {diff_step}.")
    else:
        lines.append("No differences found in tool calls.")

    lines.append("")
    lines.append(f"--- Run A ({run_a}) ---")
    lines.append(render_text(run_a, entries_a))
    lines.append("")
    lines.append(f"--- Run B ({run_b}) ---")
    lines.append(render_text(run_b, entries_b))
    return "\n".join(lines)


def render_comparison_html(
    run_a: str,
    entries_a: Sequence[Entry],
    stats_a: RunStats,
    run_b: str,
    entries_b: Sequence[Entry],
    stats_b: RunStats,
    diff_step: int | None,
    generated: float,
) -> str:
    """Self-contained HTML comparison of two timelines side by side."""

    def _render_entries(entries: Sequence[Entry]) -> str:
        return "".join(
            f'<li class="{_e(en.kind)}"><div class="when">+{en.offset:.1f}s</div>'
            f'<div class="body"><span class="badge">{_e(en.kind)}</span> <b>{_e(en.title)}</b>'
            + (f"<pre>{_e(en.detail)}</pre>" if en.detail else "")
            + "</div></li>"
            for en in entries
        )

    col_a = _render_entries(entries_a)
    col_b = _render_entries(entries_b)
    stamp = datetime.fromtimestamp(generated, UTC).strftime("%Y-%m-%d %H:%M")

    diff_text = (
        f"Tool calls first differ at step {_e(diff_step)}."
        if diff_step is not None
        else "No differences found in tool calls."
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';
form-action 'none'; frame-ancestors 'none';">
<title>Sebastian comparison</title>
<style>
:root{{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e4e3df;--card:#fff;
--model:#2f6fde;--tool:#15803d;--error:#b91c1c;--gate:#a16207;--gauntlet:#7c3aed}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe7;--muted:#9a9993;
--line:#2c2c2a;--card:#1d1d1c;--model:#7aa7ff;--tool:#4ade80;--error:#f87171;--gate:#facc15;
--gauntlet:#c4b5fd}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:1800px;margin:0 auto;padding:24px 16px}}h1{{font-size:20px;margin:0}}
.sub{{color:var(--muted);margin:2px 0 18px}}ol{{list-style:none;margin:0;padding:0}}
li{{display:grid;grid-template-columns:72px 1fr;gap:10px;padding:10px 12px;margin:6px 0;
background:var(--card);border:1px solid var(--line);border-left:4px solid var(--muted);
border-radius:8px}}.when{{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}}
.badge{{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}}
pre{{white-space:pre-wrap;word-break:break-word;margin:6px 0 0;font-size:13px}}
li.model{{border-left-color:var(--model)}}li.tool{{border-left-color:var(--tool)}}
li.error{{border-left-color:var(--error)}}li.gate{{border-left-color:var(--gate)}}
li.gauntlet{{border-left-color:var(--gauntlet)}}li.end{{border-left-color:var(--fg)}}
.comparison{{display:grid;grid-template-columns:1fr 1fr;gap:24px;align-items:start}}
.summary-table{{width:100%;border-collapse:collapse;margin-bottom:24px;background:var(--card);
border-radius:8px;overflow:hidden;border:1px solid var(--line)}}
.summary-table th, .summary-table td{{padding:8px 12px;border:1px solid var(--line);
text-align:left}}
.summary-table th{{background:var(--line);color:var(--fg)}}
.summary-table caption{{margin-bottom:8px;font-weight:bold;text-align:left}}
@media (max-width:800px){{.comparison{{grid-template-columns:1fr}}}}
@media (max-width:520px){{li{{grid-template-columns:1fr;gap:2px}}}}
</style></head><body><main>
<h1>Comparison: <code>{_e(run_a[:12])}</code> vs <code>{_e(run_b[:12])}</code></h1>
<p class="sub">Generated {_e(stamp)} UTC from traces.jsonl. <b>{diff_text}</b></p>

<table class="summary-table">
  <tr>
    <th>Run</th>
    <th>Stop Reason</th>
    <th>Steps</th>
    <th>Tokens</th>
    <th>Cost</th>
    <th>Gauntlet Loops</th>
  </tr>
  <tr>
    <td><b>A:</b> <code>{_e(run_a[:12])}</code></td>
    <td>{_e(stats_a.stop)}</td>
    <td>{_e(stats_a.steps)}</td>
    <td>{_e("{stats_a.tokens:,}")}</td>
    <td>{_e("${stats_a.cost:.4f}")}</td>
    <td>{_e(stats_a.gauntlet_loops)}</td>
  </tr>
  <tr>
    <td><b>B:</b> <code>{_e(run_b[:12])}</code></td>
    <td>{_e(stats_b.stop)}</td>
    <td>{_e(stats_b.steps)}</td>
    <td>{_e("{stats_b.tokens:,}")}</td>
    <td>{_e("${stats_b.cost:.4f}")}</td>
    <td>{_e(stats_b.gauntlet_loops)}</td>
  </tr>
</table>

<div class="comparison">
  <div>
    <h2>Run A</h2>
    <ol role="list">{col_a}</ol>
  </div>
  <div>
    <h2>Run B</h2>
    <ol role="list">{col_b}</ol>
  </div>
</div>
</main></body></html>
"""


def render_html(run_id: str, entries: Sequence[Entry], generated: float) -> str:
    """Self-contained HTML timeline."""
    end = next((en for en in reversed(entries) if en.kind == "end"), None)
    rows = "".join(
        f'<li class="{_e(en.kind)}"><div class="when">+{en.offset:.1f}s</div>'
        f'<div class="body"><span class="badge">{_e(en.kind)}</span> <b>{_e(en.title)}</b>'
        + (f"<pre>{_e(en.detail)}</pre>" if en.detail else "")
        + "</div></li>"
        for en in entries
    )
    stamp = datetime.fromtimestamp(generated, UTC).strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline';
form-action 'none'; frame-ancestors 'none';">
<title>Sebastian run</title>
<style>
:root{{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e4e3df;--card:#fff;
--model:#2f6fde;--tool:#15803d;--error:#b91c1c;--gate:#a16207;--gauntlet:#7c3aed}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe7;--muted:#9a9993;
--line:#2c2c2a;--card:#1d1d1c;--model:#7aa7ff;--tool:#4ade80;--error:#f87171;--gate:#facc15;
--gauntlet:#c4b5fd}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:880px;margin:0 auto;padding:24px 16px}}h1{{font-size:20px;margin:0}}
.sub{{color:var(--muted);margin:2px 0 18px}}ol{{list-style:none;margin:0;padding:0}}
li{{display:grid;grid-template-columns:72px 1fr;gap:10px;padding:10px 12px;margin:6px 0;
background:var(--card);border:1px solid var(--line);border-left:4px solid var(--muted);
border-radius:8px}}.when{{color:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}}
.badge{{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted)}}
pre{{white-space:pre-wrap;word-break:break-word;margin:6px 0 0;font-size:13px}}
li.model{{border-left-color:var(--model)}}li.tool{{border-left-color:var(--tool)}}
li.error{{border-left-color:var(--error)}}li.gate{{border-left-color:var(--gate)}}
li.gauntlet{{border-left-color:var(--gauntlet)}}li.end{{border-left-color:var(--fg)}}
@media (max-width:520px){{li{{grid-template-columns:1fr;gap:2px}}}}
</style></head><body><main>
<h1>Run <code>{_e(run_id[:12])}</code></h1>
<p class="sub">{_e(end.title if end else "No run_end yet: still running or interrupted")} ·
{len(entries)} events · generated {_e(stamp)} UTC from traces.jsonl</p>
<ol role="list">{rows}</ol>
</main></body></html>
"""
