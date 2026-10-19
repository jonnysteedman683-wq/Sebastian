"""``sebastian dashboard``: one self-contained HTML page showing how Sebastian is doing.

It reads only local files: score history (``history.jsonl``), run traces (``traces.jsonl``)
and the scar directory. No network, no JavaScript, no external assets, so the page can be
opened offline, attached to an issue or published as-is. Every value from those files is
HTML-escaped.
"""

from __future__ import annotations

import html
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sebastian.history import Record
from sebastian.trace import RunSummary

CHART_W, CHART_H, PAD = 640, 180, 28
RECENT_RUNS = 15


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Everything the page shows, gathered from local files."""

    records: list[Record]
    runs: list[RunSummary]
    scars: list[str]  # scar names (file stems)
    generated: float


def gather(home: Path, scar_dir: Path, now: float) -> Snapshot:
    """Read history, traces and scars. Missing files simply mean empty sections."""
    from sebastian import history  # noqa: PLC0415
    from sebastian.trace import summarize_runs  # noqa: PLC0415

    scars = sorted(p.stem for p in scar_dir.glob("*.json")) if scar_dir.is_dir() else []
    return Snapshot(
        records=history.load(home / history.HISTORY_FILE),
        runs=summarize_runs(home / "traces.jsonl", 100_000),
        scars=scars,
        generated=now,
    )


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _date(t: float | None) -> str:
    return "?" if t is None else datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%d %H:%M")


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def line_chart(title: str, points: Sequence[tuple[float, float]], label: str) -> str:
    """Inline SVG line chart of ``(time, value 0..1)``; a note when there is no data."""
    if not points:
        caption = f"<figcaption>{_e(title)}</figcaption>"
        return f'<figure>{caption}<p class="empty">No data yet.</p></figure>'
    t0, t1 = points[0][0], points[-1][0]
    span = (t1 - t0) or 1.0
    inner_w, inner_h = CHART_W - 2 * PAD, CHART_H - 2 * PAD

    def xy(t: float, v: float) -> tuple[float, float]:
        x = PAD + (inner_w * (t - t0) / span if len(points) > 1 else inner_w / 2)
        return round(x, 1), round(PAD + inner_h * (1 - max(0.0, min(1.0, v))), 1)

    coords = [xy(t, v) for t, v in points]
    path = " ".join(f"{x},{y}" for x, y in coords)
    rows = [(g, PAD + inner_h * (1 - g)) for g in (0.0, 0.5, 1.0)]
    grid = "".join(
        f'<line class="grid" x1="{PAD}" x2="{CHART_W - PAD}" y1="{y}" y2="{y}"/>'
        f'<text class="axis" x="4" y="{y + 4}">{int(g * 100)}%</text>'
        for g, y in rows
    )
    dots = "".join(
        f'<circle cx="{x}" cy="{y}" r="3.5"><title>{_e(_date(t))}: {_e(_pct(v))}</title></circle>'
        for (x, y), (t, v) in zip(coords, points, strict=True)
    )
    return (
        f"<figure><figcaption>{_e(title)}</figcaption>"
        f'<svg viewBox="0 0 {CHART_W} {CHART_H}" role="img" aria-label="{_e(label)}">'
        f'{grid}<polyline points="{path}"/>{dots}</svg></figure>'
    )


def _tile(name: str, value: str, note: str = "") -> str:
    return (
        f'<div class="tile"><div class="k">{_e(name)}</div><div class="v">{_e(value)}</div>'
        f'<div class="n">{_e(note)}</div></div>'
    )


def render(s: Snapshot) -> str:
    """The full HTML page."""
    evals = [r for r in s.records if r.kind == "eval"]
    reds = [r for r in s.records if r.kind == "redteam"]
    stops = Counter(r.stop for r in s.runs)
    done = [r for r in s.runs if r.stop != "running"]
    completed = stops.get("completed", 0)
    red_scars = [n for n in s.scars if n.startswith("redteam-")]
    tiles = "".join(
        [
            _tile("Eval score", _pct(evals[-1].score) if evals else "n/a",
                  f"{evals[-1].model}, {_date(evals[-1].t)}" if evals else "run evals --live"),
            _tile("Injection resistance",
                  _pct(reds[-1].resistance) if reds else "n/a",
                  f"latest red-team round, {len(reds)} total" if reds else "run sebastian redteam"),
            _tile("Scars", str(len(s.scars)), f"{len(red_scars)} from the red team"),
            _tile("Runs", str(len(s.runs)),
                  f"{_pct(completed / len(done)) if done else 'n/a'} completed"),
            _tile("Needs help", str(stops.get("needs_help", 0)),
                  "Gauntlet runs that could not prove they were done"),
        ]
    )  # fmt: skip
    eval_chart = line_chart(
        "Live eval score", [(r.t, r.score) for r in evals], "Eval score over time"
    )
    red_chart = line_chart(
        "Red-team resistance",
        [(r.t, r.resistance) for r in reds if r.resistance is not None],
        "Share of red-team attacks resisted over time",
    )
    stop_rows = (
        "".join(f"<tr><td>{_e(k)}</td><td>{v}</td></tr>" for k, v in sorted(stops.items()))
        or '<tr><td colspan="2" class="empty">No runs traced yet.</td></tr>'
    )
    run_rows = (
        "".join(
            f"<tr><td><code>{_e(r.run_id[:12])}</code></td><td>{_e(_date(r.started))}</td>"
            f"<td>{_e(r.stop)}</td><td>{r.steps}</td><td>{r.tokens:,}</td></tr>"
            for r in reversed(s.runs[-RECENT_RUNS:])
        )
        or '<tr><td colspan="5" class="empty">No runs traced yet.</td></tr>'
    )
    scar_items = "".join(f"<li><code>{_e(n)}</code></li>" for n in s.scars) or (
        '<li class="empty">No scars yet.</li>'
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sebastian dashboard</title>
<style>
:root{{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e4e3df;--card:#fff;--accent:#2f6fde}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe7;--muted:#9a9993;
--line:#2c2c2a;--card:#1d1d1c;--accent:#7aa7ff}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:960px;margin:0 auto;padding:24px 16px}}h1{{font-size:22px;margin:0 0 4px}}
h2{{font-size:15px;margin:28px 0 8px}}.sub{{color:var(--muted);margin:0 0 20px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}}
.tile,figure,table{{background:var(--card);border:1px solid var(--line);border-radius:10px}}
.tile{{padding:14px}}.k{{color:var(--muted);font-size:13px}}.v{{font-size:26px;font-weight:600}}
.n{{color:var(--muted);font-size:12px}}figure{{margin:12px 0;padding:12px}}
figcaption{{font-weight:600;margin-bottom:6px}}svg{{width:100%;height:auto;display:block}}
polyline{{fill:none;stroke:var(--accent);stroke-width:2}}circle{{fill:var(--accent)}}
.grid{{stroke:var(--line)}}.axis{{fill:var(--muted);font-size:11px}}
table{{border-collapse:separate;border-spacing:0;width:100%;overflow:hidden}}
td,th{{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left}}
th{{color:var(--muted);font-weight:500;font-size:13px}}tr:last-child td{{border-bottom:0}}
.scroll{{overflow-x:auto;border-radius:10px}}.scroll td{{white-space:nowrap}}
.empty{{color:var(--muted)}}ul{{columns:2;padding-left:18px}}
@media (max-width:560px){{ul{{columns:1}}}}
</style></head><body><main>
<h1>Sebastian</h1>
<p class="sub">Generated {_e(_date(s.generated))} UTC from local history, traces and scars.</p>
<section class="tiles">{tiles}</section>
<h2>Trends</h2>{eval_chart}{red_chart}
<h2>Runs by outcome</h2><table><tr><th>Stop reason</th><th>Runs</th></tr>{stop_rows}</table>
<h2>Recent runs</h2><div class="scroll"><table>
<tr><th>Run</th><th>Started</th><th>Stop</th><th>Steps</th><th>Tokens</th></tr>
{run_rows}</table></div>
<h2>Scars ({len(s.scars)})</h2><ul>{scar_items}</ul>
</main></body></html>
"""
