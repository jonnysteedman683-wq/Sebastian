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


Points = Sequence[tuple[float, float]]


def line_chart(title: str, points: Points, label: str) -> str:
    """Inline SVG line chart of ``(time, value 0..1)``; a note when there is no data."""
    return multi_line_chart(title, [("", points)], label)


def multi_line_chart(title: str, series: Sequence[tuple[str, Points]], label: str) -> str:
    """Several ``(name, points)`` series on one shared time axis (B41), with a legend.

    Series are drawn in two alternating styles (solid accent, dashed second colour) so they stay
    apart without relying on colour alone. Empty series are skipped.
    """
    drawn = [(name, sorted(pts)) for name, pts in series if pts]
    if not drawn:
        caption = f"<figcaption>{_e(title)}</figcaption>"
        return f'<figure>{caption}<p class="empty">No data yet.</p></figure>'
    times = [t for _, pts in drawn for t, _ in pts]
    t0, t1 = min(times), max(times)
    span = (t1 - t0) or 1.0
    single_time = t0 == t1
    inner_w, inner_h = CHART_W - 2 * PAD, CHART_H - 2 * PAD

    def xy(t: float, v: float) -> tuple[float, float]:
        x = PAD + (inner_w / 2 if single_time else inner_w * (t - t0) / span)
        return round(x, 1), round(PAD + inner_h * (1 - max(0.0, min(1.0, v))), 1)

    rows = [(g, PAD + inner_h * (1 - g)) for g in (0.0, 0.5, 1.0)]
    body = "".join(
        f'<line class="grid" x1="{PAD}" x2="{CHART_W - PAD}" y1="{y}" y2="{y}"/>'
        f'<text class="axis" x="4" y="{y + 4}">{int(g * 100)}%</text>'
        for g, y in rows
    )
    legend = []
    for i, (name, pts) in enumerate(drawn):
        cls = "s1" if i % 2 == 0 else "s2"
        coords = [xy(t, v) for t, v in pts]
        path = " ".join(f"{x},{y}" for x, y in coords)
        tip = f"{_e(name)} " if name else ""
        body += f'<polyline class="{cls}" points="{path}"/>' + "".join(
            f'<circle class="{cls}" cx="{x}" cy="{y}" r="3.5"><title>{tip}'
            f"{_e(_date(t))}: {_e(_pct(v))}</title></circle>"
            for (x, y), (t, v) in zip(coords, pts, strict=True)
        )
        if name:
            legend.append(f'<span class="key {cls}">{_e(name)}</span>')
    keys = f'<div class="legend">{"".join(legend)}</div>' if legend else ""
    return (
        f"<figure><figcaption>{_e(title)}</figcaption>{keys}"
        f'<svg viewBox="0 0 {CHART_W} {CHART_H}" role="img" aria-label="{_e(label)}">'
        f"{body}</svg></figure>"
    )


def _latest_model(records: Sequence[Record]) -> list[Record]:
    """Records of one kind for the model of the most recent one, so a leaderboard run across
    several models does not turn a trend line into a zigzag."""
    if not records:
        return []
    model = records[-1].model
    return [r for r in records if r.model == model]


def latest_leaderboard(records: Sequence[Record]) -> list[tuple[Record, Record | None]]:
    """The most recent ``sebastian models`` run: (plain, Gauntlet or None) per model, best first.

    Leaderboard rows are their own history kinds (P2-14), so they never move the Eval tile or
    the eval trend, which follow ``evals --live`` for one model.
    """
    board = [r for r in records if r.kind in {"leaderboard", "leaderboard-gauntlet"}]
    if not board:
        return []
    run = board[-1].run_id
    rows = [r for r in board if r.run_id == run] if run else [board[-1]]
    gauntlet = {r.model: r for r in rows if r.kind == "leaderboard-gauntlet"}
    plain = [r for r in rows if r.kind == "leaderboard"]
    plain.sort(key=lambda r: (-r.score, -(r.resistance or 0.0)))
    return [(r, gauntlet.get(r.model)) for r in plain]


def needs_help_by_day(runs: Sequence[RunSummary]) -> list[tuple[float, float]]:
    """Share of finished runs per UTC day that ended in ``needs_help``."""
    days: dict[float, list[bool]] = {}
    for r in runs:
        if r.started is None or r.stop == "running":
            continue
        day = float(int(r.started // 86_400) * 86_400)
        days.setdefault(day, []).append(r.stop == "needs_help")
    return [(d, sum(v) / len(v)) for d, v in sorted(days.items())]


def _tile(name: str, value: str, note: str = "") -> str:
    return (
        f'<div class="tile"><div class="k">{_e(name)}</div><div class="v">{_e(value)}</div>'
        f'<div class="n">{_e(note)}</div></div>'
    )


def render(s: Snapshot) -> str:
    """The full HTML page."""
    evals = [r for r in s.records if r.kind == "eval"]
    reds = [r for r in s.records if r.kind == "redteam"]
    gauntlets = [r for r in s.records if r.kind == "gauntlet"]
    critics = [r for r in s.records if r.kind == "redteam_critic"]
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
            _tile("Gauntlet score", _pct(gauntlets[-1].score) if gauntlets else "n/a",
                  f"{gauntlets[-1].model}, {_date(gauntlets[-1].t)}" if gauntlets
                  else "run evals --live --compare"),
            _tile("Critic resistance", _pct(critics[-1].resistance) if critics else "n/a",
                  f"latest critic red-team round, {len(critics)} total" if critics
                  else "run sebastian redteam --target critic"),
            _tile("Spend", f"${sum(r.cost for r in s.runs):,.2f}" if any(r.cost for r in s.runs)
                  else "n/a", "priced runs in the trace" if any(r.cost for r in s.runs)
                  else "add prices.json to track cost"),
        ]
    )  # fmt: skip
    plain_e, gaunt_e = _latest_model(evals), _latest_model(gauntlets)
    latest = plain_e or gaunt_e
    eval_chart = multi_line_chart(
        "Live eval score" + (f" ({latest[-1].model})" if latest else ""),
        [
            ("plain", [(r.t, r.score) for r in plain_e]),
            ("Gauntlet", [(r.t, r.score) for r in gaunt_e]),
        ],
        "Eval score over time, plain and under the Gauntlet",
    )
    red_chart = multi_line_chart(
        "Red-team resistance",
        [
            ("agent", [(r.t, r.resistance) for r in reds if r.resistance is not None]),
            ("critic", [(r.t, r.resistance) for r in critics if r.resistance is not None]),
        ],
        "Share of red-team attacks resisted over time, against the agent and the critic",
    )
    help_chart = line_chart(
        "Runs needing help (daily share)",
        needs_help_by_day(s.runs),
        "Share of finished runs per day that ended in needs_help",
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
    board = latest_leaderboard(s.records)
    board_rows = (
        "".join(
            f"<tr><td>{i}</td><td><code>{_e(p.model)}</code></td><td>{_e(_pct(p.score))}</td>"
            f"<td>{_e(_pct(p.resistance) if p.resistance is not None else '-')}</td>"
            f"<td>{_e(_pct(g.score) if g else '-')}</td></tr>"
            for i, (p, g) in enumerate(board, 1)
        )
        or '<tr><td colspan="5" class="empty">No leaderboard yet: run sebastian models.</td></tr>'
    )
    board_when = f" ({_e(_date(board[0][0].t))})" if board else ""
    scar_items = "".join(f"<li><code>{_e(n)}</code></li>" for n in s.scars) or (
        '<li class="empty">No scars yet.</li>'
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sebastian dashboard</title>
<style>
:root{{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e4e3df;--card:#fff;--accent:#2f6fde;
--accent2:#c2410c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ecebe7;--muted:#9a9993;
--line:#2c2c2a;--card:#1d1d1c;--accent:#7aa7ff;--accent2:#fb923c}}}}
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
polyline.s2{{stroke:var(--accent2);stroke-dasharray:6 4}}circle.s2{{fill:var(--accent2)}}
.legend{{display:flex;gap:14px;font-size:12px;color:var(--muted);margin-bottom:4px}}
.key::before{{content:"";display:inline-block;width:18px;height:0;vertical-align:middle;
margin-right:6px;border-top:2px solid var(--accent)}}
.key.s2::before{{border-top:2px dashed var(--accent2)}}
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
<h2>Trends</h2>{eval_chart}{red_chart}{help_chart}
<h2>Latest model leaderboard{board_when}</h2><div class="scroll"><table>
<tr><th>#</th><th>Model</th><th>Score</th><th>Injection</th><th>Gauntlet</th></tr>{board_rows}
</table></div>
<h2>Runs by outcome</h2><table><tr><th>Stop reason</th><th>Runs</th></tr>{stop_rows}</table>
<h2>Recent runs</h2><div class="scroll"><table>
<tr><th>Run</th><th>Started</th><th>Stop</th><th>Steps</th><th>Tokens</th></tr>
{run_rows}</table></div>
<h2>Scars ({len(s.scars)})</h2><ul>{scar_items}</ul>
</main></body></html>
"""
