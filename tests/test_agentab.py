from pathlib import Path

import pytest

from agentab.agents import MockAgent, make_agent
from agentab.report import render_html, summarize
from agentab.runner import RunResult, load_results, run_all
from agentab.stats import compare_means, compare_rates, wilson_interval
from agentab.tasks import load_setups, load_tasks
from agentab.verdict import judge

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_wilson_interval_known_values():
    low, high = wilson_interval(7, 10)
    assert low == pytest.approx(0.3968, abs=1e-3)
    assert high == pytest.approx(0.8922, abs=1e-3)


def test_wilson_interval_edges():
    assert wilson_interval(0, 0) == (0.0, 1.0)
    low, high = wilson_interval(10, 10)
    assert high == 1.0 and 0.6 < low < 0.75


def test_compare_rates_direction():
    assert compare_rates(20, 40, 38, 40).direction == "up"
    assert compare_rates(38, 40, 20, 40).direction == "down"
    assert compare_rates(5, 10, 6, 10).direction == "unclear"


def test_compare_means():
    base = {"a": [1.0, 1.1, 0.9], "b": [2.0, 2.2, 1.8]}
    pricier = {"a": [1.5, 1.6, 1.4], "b": [3.0, 3.3, 2.7]}
    noisy = {"a": [0.5, 1.6, 1.0], "b": [1.0, 3.0, 2.0]}
    c = compare_means(base, pricier)
    assert c.difference == pytest.approx(0.5) and c.direction == "up"
    assert compare_means(base, noisy).direction == "unclear"


def _runs(setup, passed, costs):
    return [RunResult(setup=setup, task=f"t{i % 2}", trial=i, passed=p, agent_finished=True,
                      duration_s=10.0, cost_usd=c, turns=1, tokens=1000, lines_changed=5,
                      agent_message="", test_output="")
            for i, (p, c) in enumerate(zip(passed, costs))]


def test_verdict_same_results_but_more_expensive_is_worse():
    # Enough runs to rule out a meaningful pass-rate gain.
    results = (_runs("base", [True] * 60, [1.0, 1.1] * 30)
               + _runs("bloated", [True] * 60, [1.5, 1.6] * 30))
    v = judge(results, "base", "bloated")
    assert v.label == "WORSE"
    assert "costs 48% more" in v.reason  # (1.5+1.6) / (1.0+1.1)


def test_verdict_pricier_but_possibly_better_is_mixed():
    results = (_runs("base", [True] * 4 + [False] * 6, [1.0] * 10)
               + _runs("big", [True] * 6 + [False] * 4, [3.0] * 10))
    v = judge(results, "base", "big")
    assert v.label == "MIXED" and "might pass up to" in v.reason


def test_verdict_pass_rate_wins():
    results = (_runs("base", [False] * 8 + [True] * 2, [1.0] * 10)
               + _runs("strong", [True] * 10, [2.0] * 10))
    v = judge(results, "base", "strong")
    assert v.label == "BETTER" and "costs" in v.reason


def test_verdict_no_difference():
    results = _runs("a", [True, False] * 5, [1.0, 1.2] * 5) + \
        _runs("b", [False, True] * 5, [1.2, 1.0] * 5)
    assert judge(results, "a", "b").label == "NO CLEAR DIFFERENCE"


def test_load_examples():
    tasks = load_tasks(EXAMPLES / "tasks")
    assert {"fix-average", "semver-sort", "cron-next", "shop-checkout"} <= {t.name for t in tasks}
    setups = load_setups(sorted((EXAMPLES / "setups").iterdir()))
    files = {s.name: [str(f) for f in s.files()] for s in setups}
    assert files["no-instructions"] == []
    assert files["focused-rules"] == ["CLAUDE.md"]  # .agentab.toml is not copied


def test_unknown_agent():
    with pytest.raises(ValueError):
        make_agent("nope")


def test_buggy_repo_fails_and_solution_passes(tmp_path):
    """Grading works: a do-nothing agent fails, a perfect agent passes."""
    tasks = load_tasks(EXAMPLES / "tasks")
    always, never = tmp_path / "always", tmp_path / "never"
    for path, rate in ((always, 1.0), (never, 0.0)):
        path.mkdir()
        (path / ".agentab.toml").write_text(f"[mock]\nsuccess_rate = {rate}\n")

    results = run_all(MockAgent(), tasks, load_setups([never, always]), trials=1,
                      out_dir=tmp_path / "out", parallel=2)

    by_setup = {s.name: s for s in summarize(results)}
    assert by_setup["never"].passed == 0
    assert by_setup["always"].passed == len(tasks)
    assert by_setup["always"].avg_lines > 0

    # Results are saved and can be reloaded to rebuild the report.
    reloaded = load_results(tmp_path / "out")
    assert len(reloaded) == len(results)
    assert "agentab" in render_html(reloaded, "t", "s")
    assert any((tmp_path / "out" / "patches" / "always").iterdir())


def test_verdict_keeps_cost_when_one_run_has_none():
    """A single timed-out run (no cost reported) must not hide cost for the whole setup."""
    pricey = _runs("pricey", [True] * 60, [3.0, 3.2] * 30)
    pricey[0].cost_usd = None
    results = _runs("base", [True] * 60, [1.0, 1.1] * 30) + pricey
    v = judge(results, "base", "pricey")
    assert "cost" in [ch.label for ch in v.changes]
    assert v.label == "WORSE"


def _setup_dir(tmp_path, name, toml):
    folder = tmp_path / name
    folder.mkdir()
    (folder / ".agentab.toml").write_text(toml)
    return folder


def test_errors_are_excluded_retried_and_resumable(tmp_path):
    tasks = load_tasks(EXAMPLES / "tasks" / "fix-roman")
    flaky = _setup_dir(tmp_path, "flaky",
                       "[mock]\nsuccess_rate = 1.0\nerror_rate = 1.0\nretryable = false\n")
    out = tmp_path / "out"
    results = run_all(MockAgent(), tasks, load_setups([flaky]), trials=3, out_dir=out,
                      max_consecutive_errors=0, sleep=lambda s: None)
    assert len(results) == 3 and all(r.error for r in results)
    assert summarize(results)[0].runs == 0  # errored runs don't count as failures

    # Fix the problem and resume: only the errored runs are redone.
    (flaky / ".agentab.toml").write_text("[mock]\nsuccess_rate = 1.0\n")
    results = run_all(MockAgent(), tasks, load_setups([flaky]), trials=3, out_dir=out,
                      sleep=lambda s: None)
    assert [r.error for r in results] == [None, None, None]
    assert len(load_results(out)) == 3


def test_stops_after_consecutive_errors(tmp_path):
    tasks = load_tasks(EXAMPLES / "tasks")
    broken = _setup_dir(tmp_path, "broken", "[mock]\nerror_rate = 1.0\nretryable = false\n")
    results = run_all(MockAgent(), tasks, load_setups([broken]), trials=5,
                      out_dir=tmp_path / "out", parallel=1, max_consecutive_errors=3,
                      sleep=lambda s: None)
    assert len(results) == 3  # stopped instead of burning through all 45 runs


def test_retryable_errors_are_retried(tmp_path, monkeypatch):
    calls = []
    agent = MockAgent()
    real_run = agent.run

    def flaky_run(workdir, task, setup, run_id):
        calls.append(run_id)
        if len(calls) == 1:
            from agentab.agents import AgentResult
            return AgentResult(False, 1.0, error="temporary API error", retryable=True)
        return real_run(workdir, task, setup, run_id)

    monkeypatch.setattr(agent, "run", flaky_run)
    always = _setup_dir(tmp_path, "always", "[mock]\nsuccess_rate = 1.0\n")
    (result,) = run_all(agent, load_tasks(EXAMPLES / "tasks" / "fix-roman"),
                        load_setups([always]), trials=1, out_dir=tmp_path / "out",
                        sleep=lambda s: None)
    assert len(calls) == 2 and result.error is None and result.passed


def test_failing_setup_command_is_an_error_not_a_failure(tmp_path):
    task_dir = tmp_path / "t"
    (task_dir / "repo").mkdir(parents=True)
    (task_dir / "task.toml").write_text(
        'prompt = "x"\ntest = "true"\n[workspace]\nsetup = "exit 3"\n')
    (result,) = run_all(MockAgent(), load_tasks(task_dir), load_setups([tmp_path / "t"]),
                        trials=1, out_dir=tmp_path / "out")
    assert result.error and "setup command failed" in result.error


def test_classify_failure():
    from agentab.agents import classify_failure
    assert classify_failure('API Error: 529 {"error":{"type":"overloaded_error"}}')[1] is True
    assert classify_failure("Claude AI usage limit reached, resets 5pm")[1] is False
    assert classify_failure("Not logged in · Please run /login")[1] is False
    assert classify_failure("API Error: Stream idle timeout - partial response received")[1]
    # Test output must never look like an outage (it would be retried and dropped).
    for text in ["I fixed the bug in parser.py", "FAILED test_returns_500", "Ran 1500 tests",
                 "test_connection_pool failed", "timeout in test_x",
                 "fix authentication middleware: 2 tests failed", "HTTP 503 handler added"]:
        assert classify_failure(text) is None, text
