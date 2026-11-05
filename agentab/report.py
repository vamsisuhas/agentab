"""Turns run results into a terminal summary and a shareable HTML report."""

from __future__ import annotations

import html
import sys
from dataclasses import dataclass
from statistics import mean

from .runner import RunResult
from .stats import wilson_interval
from .verdict import Verdict, format_change, format_range, judge


@dataclass
class SetupSummary:
    name: str
    runs: int
    passed: int
    low: float
    high: float
    avg_cost: float | None
    avg_time: float
    avg_tokens: float | None
    avg_lines: float
    crashed: int

    @property
    def rate(self) -> float:
        return self.passed / self.runs if self.runs else 0.0


def _avg(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return mean(values) if values else None


def summarize(results: list[RunResult], setups: list[str] | None = None) -> list[SetupSummary]:
    names = setups or list(dict.fromkeys(r.setup for r in results))
    summaries = []
    for name in names:
        runs = [r for r in results if r.setup == name and r.error is None]
        if not runs:
            summaries.append(SetupSummary(name, 0, 0, 0.0, 1.0, None, 0.0, None, 0.0, 0))
            continue
        passed = sum(r.passed for r in runs)
        low, high = wilson_interval(passed, len(runs))
        summaries.append(SetupSummary(
            name=name, runs=len(runs), passed=passed, low=low, high=high,
            avg_cost=_avg([r.cost_usd for r in runs]),
            avg_time=mean(r.duration_s for r in runs),
            avg_tokens=_avg([r.tokens for r in runs]),
            avg_lines=mean(r.lines_changed for r in runs),
            crashed=sum(not r.agent_finished for r in runs),
        ))
    return summaries


def verdicts(results: list[RunResult], setups: list[str] | None = None) -> list[Verdict]:
    """Judge every setup against the first one (the baseline)."""
    names = setups or list(dict.fromkeys(r.setup for r in results))
    return [judge(results, names[0], name) for name in names[1:]]


EFFECT_WORDS = {"good": "better", "bad": "worse", "neutral": "real change",
                "unclear": "could be luck"}


def error_summary(results: list[RunResult]) -> list[tuple[str, int]]:
    """Why runs were left out, most common reason first."""
    counts: dict[str, int] = {}
    for r in results:
        if r.error:
            reason = r.error.split(":")[0] if len(r.error) > 80 else r.error
            counts[reason] = counts.get(reason, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


def uninformative_tasks(results: list[RunResult]) -> tuple[list[str], list[str]]:
    """Tasks every setup always passed, and tasks every setup always failed.

    They can't tell setups apart, so they only dilute the comparison.
    """
    by_task: dict[str, list[bool]] = {}
    for r in results:
        if r.error is None:
            by_task.setdefault(r.task, []).append(r.passed)
    setups = {r.setup for r in results}
    complete = {t for t in by_task
                if {r.setup for r in results if r.task == t and r.error is None} == setups}
    always = sorted(t for t in complete if all(by_task[t]))
    never = sorted(t for t in complete if not any(by_task[t]))
    return always, never


def task_grid(results: list[RunResult]) -> tuple[list[str], list[str], dict]:
    setups = list(dict.fromkeys(r.setup for r in results))
    tasks = sorted({r.task for r in results})
    grid = {}
    for r in results:
        if r.error:
            continue
        passed, total = grid.get((r.task, r.setup), (0, 0))
        grid[(r.task, r.setup)] = (passed + r.passed, total + 1)
    return setups, tasks, grid


# ---------------------------------------------------------------- terminal

class _Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text


def _money(v: float | None) -> str:
    return "—" if v is None else f"${v:.3f}"


def _tokens(v: float | None) -> str:
    return "—" if v is None else f"{v / 1000:.0f}k"


def _pct(v: float) -> str:
    return f"{v * 100:.0f}%"


def print_summary(results: list[RunResult], title: str, file=sys.stdout,
                  setups: list[str] | None = None) -> None:
    style = _Style(file.isatty())
    summaries = summarize(results, setups)
    rows = [("Setup", "Passed", "Pass rate", "Likely range", "Avg cost", "Avg time",
             "Avg tokens", "Lines changed")]
    for s in summaries:
        rows.append((
            s.name, f"{s.passed}/{s.runs}", _pct(s.rate),
            f"{_pct(s.low)}–{_pct(s.high)}", _money(s.avg_cost),
            f"{s.avg_time:.0f}s", _tokens(s.avg_tokens), f"{s.avg_lines:.0f}",
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]

    print(file=file)
    print(style(title, "1"), file=file)
    print(file=file)
    for i, row in enumerate(rows):
        line = "  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip()
        print(style(line, "2") if i == 0 else line, file=file)

    if len(summaries) > 1:
        colors = {"BETTER": "32;1", "WORSE": "31;1", "MIXED": "33;1",
                  "NO CLEAR DIFFERENCE": "2;1"}
        effect_colors = {"good": "32", "bad": "31", "neutral": "36", "unclear": "2"}
        print(file=file)
        print(style(f"Compared with {summaries[0].name}:", "1"), file=file)
        for v in verdicts(results, setups):
            print(file=file)
            print(f"  {style(v.setup, '1')}  →  {style(v.label, colors[v.label])}", file=file)
            print(f"  {v.reason}", file=file)
            for ch in v.changes:
                c = ch.comparison
                span = format_range(c.low, c.high, ch.relative)
                print(f"    {ch.label:<13} {format_change(c.difference, ch.relative):>6}   "
                      f"{span:<26} {style(EFFECT_WORDS[ch.effect], effect_colors[ch.effect])}",
                      file=file)

    for note in notes(results):
        print(file=file)
        print(style(note, "33"), file=file)


def notes(results: list[RunResult]) -> list[str]:
    """Warnings worth showing under the results."""
    out = []
    if results and max(r.trial for r in results) < 2:
        out.append("Note: with 1 trial per task, cost and time can't be compared (there's "
                   "no way to tell luck from a real difference). Use --trials 3 or more.")
    crashed = sum(1 for r in results if r.error is None and not r.agent_finished)
    if crashed:
        out.append(f"Note: {crashed} run(s) crashed or timed out (counted as failed).")
    errors = error_summary(results)
    if errors:
        total = sum(n for _, n in errors)
        reasons = "; ".join(f"{reason} ({n})" for reason, n in errors[:3])
        out.append(f"Left out {total} run(s) that hit errors not caused by the setup: "
                   f"{reasons}. Rerun with --resume to fill them in.")
    always, never = uninformative_tasks(results)
    tasks = {r.task for r in results}
    if len(always) + len(never) >= max(2, len(tasks) // 3):
        parts = []
        if always:
            parts.append(f"{len(always)} were passed by every setup every time")
        if never:
            parts.append(f"{len(never)} were failed by every setup every time")
        out.append(f"Of {len(tasks)} tasks, {' and '.join(parts)}. They can't tell setups "
                   "apart, so harder (or easier) tasks would make this comparison sharper.")
    return out


# ---------------------------------------------------------------- html

_CSS = """
:root{--bg:#fafaf9;--card:#fff;--text:#1c1917;--muted:#78716c;--line:#e7e5e4;
--accent:#4f46e5;--good:#15803d;--bad:#b91c1c;--warn:#a16207;--bar:#e0e7ff}
@media (prefers-color-scheme:dark){:root{--bg:#0c0a09;--card:#1c1917;--text:#f5f5f4;
--muted:#a8a29e;--line:#292524;--accent:#818cf8;--good:#4ade80;--bad:#f87171;
--warn:#facc15;--bar:#312e81}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif}
main{max-width:980px;margin:0 auto;padding:32px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:36px 0 12px}
.sub{color:var(--muted);margin:0 0 24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:4px 0;
overflow-x:auto}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--line);white-space:nowrap}
tr:last-child td{border-bottom:none}
th{color:var(--muted);font-weight:500;font-size:13px}
.range{position:relative;width:180px;height:10px;background:var(--line);border-radius:5px}
.range .ci{position:absolute;top:0;height:10px;background:var(--bar);border-radius:5px}
.range .pt{position:absolute;top:-3px;width:4px;height:16px;background:var(--accent);
border-radius:2px;margin-left:-2px}
.verdicts{display:grid;gap:12px}
.vcard{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.vhead{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.vname{font-weight:600;font-size:16px}
.badge{font-size:12px;font-weight:700;letter-spacing:.04em;padding:3px 8px;border-radius:6px}
.b-BETTER{background:color-mix(in srgb,var(--good) 18%,transparent);color:var(--good)}
.b-WORSE{background:color-mix(in srgb,var(--bad) 18%,transparent);color:var(--bad)}
.b-MIXED{background:color-mix(in srgb,var(--warn) 18%,transparent);color:var(--warn)}
.b-NO{background:var(--line);color:var(--muted)}
.reason{margin:8px 0 10px}
.vcard table td,.vcard table th{padding:6px 10px 6px 0}
.good{color:var(--good)}.bad{color:var(--bad)}.neutral{color:var(--accent)}
.unclear{color:var(--muted)}
.cell{display:inline-block;min-width:44px;text-align:center;border-radius:6px;padding:2px 6px}
details{border-bottom:1px solid var(--line);padding:8px 14px}
details:last-child{border-bottom:none}
summary{cursor:pointer}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;
overflow-x:auto;font-size:12px;white-space:pre-wrap}
.pass{color:var(--good)}.fail{color:var(--bad)}
.note{color:var(--muted);font-size:13px;margin-top:10px}
.warn{color:var(--warn)}
"""


def _esc(v) -> str:
    return html.escape(str(v))


def render_html(results: list[RunResult], title: str, subtitle: str,
                setups: list[str] | None = None) -> str:
    summaries = summarize(results, setups)
    parts = [f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
             f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
             f"<title>agentab report</title><style>{_CSS}</style></head><body><main>",
             f"<h1>{_esc(title)}</h1><p class='sub'>{_esc(subtitle)}</p>"]

    if len(summaries) > 1:
        parts.append(f"<h2>Verdict vs {_esc(summaries[0].name)}</h2><div class='verdicts'>")
        for v in verdicts(results, setups):
            badge = "NO" if v.label.startswith("NO") else v.label
            rows = "".join(
                f"<tr><td>{_esc(ch.label)}</td>"
                f"<td>{format_change(ch.comparison.difference, ch.relative)}</td>"
                f"<td class='note'>"
                f"{format_range(ch.comparison.low, ch.comparison.high, ch.relative)}</td>"
                f"<td class='{ch.effect}'>{EFFECT_WORDS[ch.effect]}</td></tr>"
                for ch in v.changes)
            parts.append(
                f"<div class='vcard'><div class='vhead'><span class='vname'>{_esc(v.setup)}"
                f"</span><span class='badge b-{badge}'>{_esc(v.label)}</span></div>"
                f"<p class='reason'>{_esc(v.reason)}</p><table>{rows}</table></div>")
        parts.append("</div><p class='note'>A change counts as real only when its whole "
                     "likely range is above or below zero. Otherwise it could be luck.</p>")
    for note in notes(results):
        parts.append(f"<p class='note warn'>{_esc(note)}</p>")

    parts.append("<h2>Results by setup</h2><div class='card'><table><tr><th>Setup</th>"
                 "<th>Passed</th><th>Pass rate</th><th>Likely range (95%)</th><th>Avg cost</th>"
                 "<th>Avg time</th><th>Avg tokens</th><th>Lines changed</th></tr>")
    for s in summaries:
        bar = (f"<div class='range'><div class='ci' style='left:{s.low * 100:.1f}%;"
               f"width:{(s.high - s.low) * 100:.1f}%'></div>"
               f"<div class='pt' style='left:{s.rate * 100:.1f}%'></div></div>")
        parts.append(
            f"<tr><td><b>{_esc(s.name)}</b></td><td>{s.passed}/{s.runs}</td>"
            f"<td>{_pct(s.rate)}</td><td>{bar}<span class='note'>{_pct(s.low)}–{_pct(s.high)}"
            f"</span></td><td>{_money(s.avg_cost)}</td><td>{s.avg_time:.0f}s</td>"
            f"<td>{_tokens(s.avg_tokens)}</td><td>{s.avg_lines:.0f}</td></tr>")
    parts.append("</table></div>")

    setups, tasks, grid = task_grid(results)
    parts.append("<h2>Results by task</h2><div class='card'><table><tr><th>Task</th>"
                 + "".join(f"<th>{_esc(s)}</th>" for s in setups) + "</tr>")
    for t in tasks:
        cells = []
        for s in setups:
            passed, total = grid.get((t, s), (0, 0))
            share = passed / total if total else 0
            cells.append(f"<td><span class='cell' style='background:color-mix(in srgb,"
                         f"var(--good) {share * 35:.0f}%,transparent)'>{passed}/{total}"
                         f"</span></td>")
        parts.append(f"<tr><td>{_esc(t)}</td>{''.join(cells)}</tr>")
    parts.append("</table></div>")

    parts.append("<h2>Every run</h2><div class='card'>")
    for r in results:
        mark = (f"<span class='warn'>! error: {_esc(r.error[:80])}</span>" if r.error else
                "<span class='pass'>✔ passed</span>" if r.passed else
                "<span class='fail'>✘ failed</span>")
        parts.append(
            f"<details><summary>{mark} &nbsp; <b>{_esc(r.setup)}</b> · {_esc(r.task)} · "
            f"trial {r.trial} · {r.duration_s:.0f}s · {_money(r.cost_usd)}</summary>"
            f"<p class='note'>Agent's final message</p><pre>{_esc(r.agent_message or '—')}</pre>"
            f"<p class='note'>Hidden test output</p><pre>{_esc(r.test_output or '—')}</pre>"
            f"</details>")
    parts.append("</div><p class='note'>Generated by agentab.</p></main></body></html>")
    return "".join(parts)
