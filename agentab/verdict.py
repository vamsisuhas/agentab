"""Decides whether a setup is better or worse than the baseline, across all metrics.

Pass rate matters most. If it really changed, that decides the verdict. If it
didn't, the efficiency metrics decide: same results for more money or more time
is worse, same results for less is better.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .runner import RunResult
from .stats import Comparison, compare_means, compare_rates, fisher_exact


@dataclass
class Metric:
    key: str             # field on RunResult
    label: str
    lower_is_better: bool | None  # None: neither direction is good or bad
    more: str            # how to describe a real increase; {} is the amount
    less: str            # how to describe a real decrease


EFFICIENCY_METRICS = [
    Metric("cost_usd", "cost", True, "costs {} more", "costs {} less"),
    Metric("duration_s", "time", True, "is {} slower", "is {} faster"),
    Metric("tokens", "tokens", True, "uses {} more tokens", "uses {} fewer tokens"),
    Metric("lines_changed", "code written", None, "writes {} more code", "writes {} less code"),
]


# We check several efficiency metrics at once. Each check at 95% confidence has a
# 5% chance of a false alarm, so with 4 checks a false alarm would happen often.
# Splitting the 5% between them (Bonferroni correction) keeps the overall
# false-alarm chance at about 5%.
EFFICIENCY_CONFIDENCE = 1 - 0.05 / len(EFFICIENCY_METRICS)


@dataclass
class Change:
    label: str
    comparison: Comparison
    relative: bool        # True: difference is a ratio vs baseline; False: percentage points
    effect: str           # "good", "bad", "neutral" or "unclear"
    phrase: str = ""      # e.g. "33% more expensive"


@dataclass
class TaskDiff:
    task: str
    base_passed: int
    base_runs: int
    passed: int
    runs: int
    real: bool  # big enough to be a real difference, even with few runs

    @property
    def difference(self) -> float:
        return self.passed / self.runs - self.base_passed / self.base_runs

    def describe(self) -> str:
        return f"{self.task} ({self.passed}/{self.runs} vs {self.base_passed}/{self.base_runs})"


# "Cheaper with the same pass rate" only counts as BETTER if a pass-rate drop
# larger than this has been ruled out, and "pricier with the same pass rate" only
# counts as WORSE if a gain larger than this has been ruled out.
NON_INFERIORITY = 0.10

# A task is worth pointing out when its pass rate differs this much.
NOTABLE_TASK_DIFFERENCE = 0.4


@dataclass
class Verdict:
    setup: str
    label: str            # BETTER, WORSE, MIXED or NO CLEAR DIFFERENCE
    reason: str
    changes: list[Change]
    task_diffs: list[TaskDiff] = field(default_factory=list)


def format_change(diff: float, relative: bool) -> str:
    if not math.isfinite(diff):
        return "n/a"
    if relative and diff >= 1:
        return f"{1 + diff:.1f}×"
    if 0 < abs(diff) < 0.01:
        return f"{diff * 100:+.1f}%"
    return f"{diff * 100:+.0f}%"


def format_range(low: float, high: float, relative: bool) -> str:
    """Both ends in the same style, e.g. "+18% to +49%" or "1.9× to 4.1×"."""
    if not (math.isfinite(low) and math.isfinite(high)):
        return "not enough data"
    if relative and high >= 1 and low > -1:
        return f"likely {1 + low:.1f}× to {1 + high:.1f}×"
    return f"likely {format_change(low, False)} to {format_change(high, False)}"


def _by_task(results: list[RunResult], setup: str, key: str) -> dict[str, list[float]] | None:
    """Values per task. Runs without a value (e.g. timed out) are skipped."""
    groups: dict[str, list[float]] = {}
    for r in results:
        value = getattr(r, key)
        if r.setup == setup and value is not None:
            groups.setdefault(r.task, []).append(float(value))
    return groups or None  # None: the agent doesn't report this metric at all


def _effect(direction: str, lower_is_better: bool | None) -> str:
    if direction == "unclear":
        return "unclear"
    if lower_is_better is None:
        return "neutral"
    return "good" if (direction == "down") == lower_is_better else "bad"


def _phrase(metric: Metric, c: Comparison) -> str:
    if c.direction == "up":
        ratio = 1 + c.difference
        return metric.more.format(f"{ratio:.1f}×" if ratio >= 2 else f"{c.difference:.0%}")
    if c.direction == "down":
        return metric.less.format(f"{-c.difference:.0%}")
    return ""


def task_differences(results: list[RunResult], baseline: str,
                     candidate: str) -> list[TaskDiff]:
    """Tasks where the two setups' pass rates differ a lot, biggest first."""
    counts: dict[tuple[str, str], list[int]] = {}
    for r in results:
        c = counts.setdefault((r.setup, r.task), [0, 0])
        c[0] += r.passed
        c[1] += 1
    tasks = sorted({t for s, t in counts if s == baseline} & {t for s, t in counts if s == candidate})
    diffs = []
    for t in tasks:
        (bp, bn), (cp, cn) = counts[(baseline, t)], counts[(candidate, t)]
        if abs(cp / cn - bp / bn) >= NOTABLE_TASK_DIFFERENCE:
            # Bonferroni again: we look at every task, so each one must clear a higher bar.
            real = fisher_exact(bp, bn, cp, cn) < 0.05 / len(tasks)
            diffs.append(TaskDiff(t, bp, bn, cp, cn, real))
    return sorted(diffs, key=lambda d: -abs(d.difference))


def judge(results: list[RunResult], baseline: str, candidate: str, seed: int = 0) -> Verdict:
    results = [r for r in results if r.error is None]  # errored runs say nothing
    # Compare on the same tasks only, so a missing task can't tilt the pass rate.
    common = {r.task for r in results if r.setup == baseline} & \
        {r.task for r in results if r.setup == candidate}
    results = [r for r in results if r.task in common]
    base_runs = [r for r in results if r.setup == baseline]
    cand_runs = [r for r in results if r.setup == candidate]
    if not base_runs or not cand_runs:
        return Verdict(candidate, "NO CLEAR DIFFERENCE",
                       "Not enough successful runs to compare.", [])

    rate = compare_rates(sum(r.passed for r in base_runs), len(base_runs),
                         sum(r.passed for r in cand_runs), len(cand_runs))
    rate_effect = {"up": "good", "down": "bad"}.get(rate.direction, "unclear")
    changes = [Change("pass rate", rate, relative=False, effect=rate_effect)]

    for metric in EFFICIENCY_METRICS:
        base_groups = _by_task(results, baseline, metric.key)
        cand_groups = _by_task(results, candidate, metric.key)
        if base_groups is None or cand_groups is None:
            continue
        c = compare_means(base_groups, cand_groups, confidence=EFFICIENCY_CONFIDENCE,
                          seed=seed)
        changes.append(Change(metric.label, c, relative=True,
                              effect=_effect(c.direction, metric.lower_is_better),
                              phrase=_phrase(metric, c)))

    good = [ch.phrase for ch in changes[1:] if ch.effect == "good"]
    bad = [ch.phrase for ch in changes[1:] if ch.effect == "bad"]
    notes = [ch.phrase for ch in changes[1:] if ch.effect == "neutral"]

    if rate.direction == "up":
        label = "BETTER"
        reason = f"Passes more often ({format_change(rate.difference, False)})"
        reason += f", but it {_join(bad)}." if bad else "."
    elif rate.direction == "down":
        label = "WORSE"
        reason = f"Passes less often ({format_change(rate.difference, False)})"
        reason += f", even though it {_join(good)}." if good else "."
    elif bad and not good and rate.high <= NON_INFERIORITY:
        label = "WORSE"
        reason = (f"No clear gain in pass rate (likely {format_change(rate.low, False)} to "
                  f"{format_change(rate.high, False)}), but it {_join(bad)}.")
    elif bad and not good:
        # More expensive, but it might also pass noticeably more often: a gain of up
        # to rate.high hasn't been ruled out, so don't call it worse either.
        label = "MIXED"
        reason = (f"It {_join(bad)}, and it might pass up to {rate.high:.0%} more often "
                  f"(or not: likely {format_change(rate.low, False)} to "
                  f"{format_change(rate.high, False)}). More trials can tell.")
    elif good and not bad and rate.low >= -NON_INFERIORITY:
        label = "BETTER"
        reason = f"No clear change in pass rate, and it {_join(good)}."
    elif good and not bad:
        # Cheaper or faster, but it may be giving up early: a drop in pass rate of up
        # to rate.low can't be ruled out, so don't call it better.
        label = "MIXED"
        reason = (f"It {_join(good)}, but the pass rate could be as much as "
                  f"{-rate.low:.0%} lower. More trials can tell.")
    elif good and bad:
        label = "MIXED"
        reason = (f"No clear change in pass rate (likely {format_change(rate.low, False)} to "
                  f"{format_change(rate.high, False)}); it {_join(good)}, but {_join(bad)}.")
    elif notes:
        label = "NO CLEAR DIFFERENCE"
        reason = "No real difference in pass rate, cost, time or tokens."
    else:
        label = "NO CLEAR DIFFERENCE"
        reason = "Nothing changed beyond what luck explains. More trials can detect smaller effects."

    if notes:
        reason += f" It also {_join(notes)}."

    # Per-task notes only when they add something: the exceptions to the overall
    # result, or detail when the overall pass rate didn't clearly move.
    task_diffs = task_differences(results, baseline, candidate)
    better = [d.describe() for d in task_diffs
              if d.difference > 0 and rate.direction != "up"][:3]
    worse = [d.describe() for d in task_diffs
             if d.difference < 0 and rate.direction != "down"][:3]
    if better or worse:
        parts = []
        if better:
            parts.append(f"better on {_join(better)}")
        if worse:
            parts.append(f"worse on {_join(worse)}")
        sure = "a real difference" if all(d.real for d in task_diffs) else \
            "few runs per task, so treat this as a hint"
        reason += f" By task: {'; '.join(parts)} ({sure})."
    return Verdict(candidate, label, reason, changes, task_diffs)


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]
