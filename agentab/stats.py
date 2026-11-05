"""Small-sample statistics for pass rates.

Agents are random, so "7 of 10 passed" really means "the true pass rate is
probably somewhere between 40% and 89%". These functions compute that range
(a 95% confidence interval) and decide whether two setups really differ.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

Z_95 = 1.959964  # 95% confidence


def wilson_interval(successes: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """95% range for a pass rate. Works well even for small n and 0% / 100%."""
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    # At 0% and 100% the bound is exactly 0 or 1; avoid floating point drift.
    low = 0.0 if successes == 0 else max(0.0, center - half)
    high = 1.0 if successes == n else min(1.0, center + half)
    return low, high


@dataclass
class Comparison:
    difference: float  # candidate minus baseline
    low: float         # 95% range of the difference
    high: float

    @property
    def direction(self) -> str:
        """'up' or 'down' if the change is real, 'unclear' if it could be luck."""
        if self.low > 0:
            return "up"
        if self.high < 0:
            return "down"
        return "unclear"


def compare_means(base: dict[str, list[float]], cand: dict[str, list[float]],
                  confidence: float = 0.95, resamples: int = 4000,
                  seed: int = 0) -> Comparison:
    """Relative difference of two averages (e.g. +0.33 = 33% higher), with a likely range.

    `base` and `cand` map each task name to that task's values. The average is
    the mean of per-task means over the tasks both setups have, so every task
    counts equally even when some runs are missing (a setup that times out on
    the expensive tasks must not look cheaper because of it).

    The range comes from a bootstrap: resample each task's runs many times and
    look at how much the difference moves.
    """
    # A task needs at least 2 runs in each setup: with 1 run there's no way to see
    # how much results vary, and the bootstrap would report false certainty.
    tasks = [t for t in base if t in cand and len(base[t]) >= 2 and len(cand[t]) >= 2]
    if not tasks:
        return Comparison(math.nan, -math.inf, math.inf)

    def task_mean(groups: dict[str, list[float]]) -> float:
        return sum(sum(groups[t]) / len(groups[t]) for t in tasks) / len(tasks)

    base_mean = task_mean(base)
    if base_mean == 0:
        return Comparison(math.nan, -math.inf, math.inf)

    rng = random.Random(seed)
    diffs = []
    for _ in range(resamples):
        b = {t: rng.choices(base[t], k=len(base[t])) for t in tasks}
        c = {t: rng.choices(cand[t], k=len(cand[t])) for t in tasks}
        b_mean = task_mean(b)
        if b_mean > 0:  # a resample with a zero baseline says nothing about the ratio
            diffs.append(task_mean(c) / b_mean - 1)
    if len(diffs) < resamples / 2:
        return Comparison(math.nan, -math.inf, math.inf)
    diffs.sort()
    tail = (1 - confidence) / 2
    low = diffs[int(tail * len(diffs))]
    high = diffs[min(len(diffs) - 1, int((1 - tail) * len(diffs)))]
    return Comparison(task_mean(cand) / base_mean - 1, low, high)


def compare_rates(base_successes: int, base_n: int,
                  cand_successes: int, cand_n: int) -> Comparison:
    """Difference between two pass rates with a 95% range (Newcombe's method).

    If the whole range is above zero the candidate is really better; if it is
    all below zero it is really worse; otherwise we cannot tell yet.
    """
    p_base = base_successes / base_n if base_n else 0.0
    p_cand = cand_successes / cand_n if cand_n else 0.0
    base_low, base_high = wilson_interval(base_successes, base_n)
    cand_low, cand_high = wilson_interval(cand_successes, cand_n)
    diff = p_cand - p_base
    low = diff - math.sqrt((p_cand - cand_low) ** 2 + (base_high - p_base) ** 2)
    high = diff + math.sqrt((cand_high - p_cand) ** 2 + (p_base - base_low) ** 2)
    return Comparison(diff, low, high)


def fisher_exact(a_pass: int, a_n: int, b_pass: int, b_n: int) -> float:
    """Two-sided p-value: how likely a split at least this uneven is by pure luck.

    Used for per-task comparisons, where there are only a handful of runs.
    """
    total_pass = a_pass + b_pass
    total = a_n + b_n

    def prob(k: int) -> float:
        return (math.comb(a_n, k) * math.comb(b_n, total_pass - k)
                / math.comb(total, total_pass))

    observed = prob(a_pass)
    low, high = max(0, total_pass - b_n), min(a_n, total_pass)
    return min(1.0, sum(p for k in range(low, high + 1)
                        if (p := prob(k)) <= observed * (1 + 1e-9)))
