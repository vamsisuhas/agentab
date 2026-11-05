"""Regression tests for bugs found in code review. Each one once produced a wrong result."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from agentab import procs
from agentab.agents import AgentResult, MockAgent
from agentab.cli import main
from agentab.report import verdicts
from agentab.runner import RunResult, run_all
from agentab.stats import compare_means
from agentab.tasks import Setup, load_setups, load_tasks
from agentab.verdict import judge

EXAMPLES = Path(__file__).parent.parent / "examples"


class ScriptedAgent:
    """An agent that runs a Python function on the workspace instead of an AI."""
    name = "scripted"

    def __init__(self, action):
        self.action = action

    def run(self, workdir, task, setup, run_id):
        self.action(Path(workdir))
        return AgentResult(True, 1.0, cost_usd=0.01, turns=1, tokens=10)


def make_task(tmp_path, files, test, checks=None, name="t"):
    task = tmp_path / name
    for rel, content in files.items():
        (task / "repo" / rel).parent.mkdir(parents=True, exist_ok=True)
        (task / "repo" / rel).write_text(content)
    for rel, content in (checks or {}).items():
        (task / "checks" / rel).parent.mkdir(parents=True, exist_ok=True)
        (task / "checks" / rel).write_text(content)
    (task / "task.toml").write_text(f'prompt = "do it"\ntest = {json.dumps(test)}\n')
    return load_tasks(task)


def empty_setup(tmp_path, name="base"):
    (tmp_path / name).mkdir(exist_ok=True)
    return load_setups([tmp_path / name])


def test_background_child_does_not_cause_a_timeout(tmp_path):
    start = time.monotonic()
    result = procs.run("(sleep 5 &); exit 0", cwd=tmp_path, timeout=3)
    assert result.returncode == 0 and time.monotonic() - start < 2


def test_ctrl_c_does_not_record_false_failures(tmp_path):
    try:
        procs.kill_all()
        with pytest.raises(procs.Stopped):
            procs.run("true", cwd=tmp_path, timeout=5)
    finally:
        procs.allow_new_runs()


def test_diff_survives_agent_commits(tmp_path):
    def commit_change(ws):
        (ws / "a.py").write_text("x = 2\n")
        subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@a", "commit", "-qam",
                        "agent"], cwd=ws, check=True)
    tasks = make_task(tmp_path, {"a.py": "x = 1\n"}, "true")
    (r,) = run_all(ScriptedAgent(commit_change), tasks, empty_setup(tmp_path), 1,
                   tmp_path / "out")
    assert r.lines_changed == 2


def test_conftest_that_skips_everything_does_not_pass(tmp_path):
    test = "import sys; sys.exit(0 if __import__('a').x == 2 else 1)"
    tasks = make_task(tmp_path, {"a.py": "x = 1\n", "tests/conftest.py": "",
                                 "tests/test_a.py": "from a import x\n\ndef test_x():\n"
                                 "    assert x == 2\n"},
                      "{python} -m pytest -q -p no:cacheprovider tests/test_a.py")

    def cheat(ws):
        (ws / "conftest.py").write_text(
            "import pytest\n\ndef pytest_collection_modifyitems(items):\n"
            "    for i in items:\n        i.add_marker(pytest.mark.skip)\n")
        (ws / "tests" / "conftest.py").write_text((ws / "conftest.py").read_text())
    (r,) = run_all(ScriptedAgent(cheat), tasks, empty_setup(tmp_path), 1, tmp_path / "out")
    assert not r.passed
    assert "restored test files" in r.test_output


def test_setup_files_replace_symlinks_instead_of_writing_through(tmp_path):
    task_dir = tmp_path / "t"
    (task_dir / "repo").mkdir(parents=True)
    (task_dir / "repo" / "AGENTS.md").write_text("original\n")
    os.symlink("AGENTS.md", task_dir / "repo" / "CLAUDE.md")
    (task_dir / "task.toml").write_text('prompt = "x"\ntest = "grep -q original AGENTS.md"\n')
    setup = tmp_path / "s"
    setup.mkdir()
    (setup / "CLAUDE.md").write_text("from the setup\n")
    seen = {}
    (r,) = run_all(ScriptedAgent(lambda ws: seen.update(
        agents=(ws / "AGENTS.md").read_text(), claude=(ws / "CLAUDE.md").read_text())),
        load_tasks(task_dir), load_setups([setup]), 1, tmp_path / "out")
    assert seen == {"agents": "original\n", "claude": "from the setup\n"}
    assert r.passed


def test_agent_breaking_the_workspace_is_a_failed_run_not_a_crash(tmp_path):
    import shutil
    tasks = make_task(tmp_path, {"a.py": "x = 1\n"}, "true")
    results = run_all(ScriptedAgent(lambda ws: shutil.rmtree(ws / ".git")), tasks,
                      empty_setup(tmp_path), 2, tmp_path / "out")
    assert len(results) == 2
    assert all(not r.passed and r.error is None for r in results)


def test_missing_runs_do_not_make_a_setup_look_cheaper():
    base = {"easy": [1.0] * 5, "hard": [10.0] * 5}
    cand = {"easy": [1.0] * 5, "hard": [10.0] * 2}  # 3 hard runs timed out (no cost)
    c = compare_means(base, cand)
    assert c.difference == pytest.approx(0.0) and c.direction == "unclear"


def test_zero_baseline_resamples_do_not_shrink_the_range():
    c = compare_means({"t": [0, 0, 0, 0, 10]}, {"t": [30, 31, 29, 30, 30]})
    assert c.low > 0


def _run(setup, task, trial, passed, cost=1.0):
    return RunResult(setup, task, trial, passed, True, 10.0, cost, 1, 100, 1, "", "")


def test_cheaper_but_maybe_failing_more_is_not_better():
    results = ([_run("base", f"t{i}", 1, i % 2 == 0, 1.0) for i in range(6)] +
               [_run("base", f"t{i}", 2, i % 2 == 0, 1.1) for i in range(6)] +
               [_run("lazy", f"t{i}", 1, i % 3 == 0, 0.5) for i in range(6)] +
               [_run("lazy", f"t{i}", 2, i % 3 == 0, 0.55) for i in range(6)])
    v = judge(results, "base", "lazy")
    assert v.label == "MIXED" and "could be as much as" in v.reason


def test_report_uses_the_real_baseline_even_without_its_runs():
    results = [_run("b", "t", 1, True), _run("c", "t", 1, False)]
    names = [v.setup for v in verdicts(results, ["a", "b", "c"])]
    assert names == ["b", "c"]  # compared against "a", not against "b"


def test_non_utf8_commit_message_does_not_crash_mining(tmp_path):
    from agentab.mine import MineOptions, mine
    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a",
           "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@a"}
    run = lambda *a, **k: subprocess.run(["git", "-C", str(repo), *a], check=True, env=env,
                                         capture_output=True, **k)
    run("init", "-q")
    (repo / "m.py").write_text("V = 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_m.py").write_text("def test_ok():\n    pass\n")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    (repo / "m.py").write_text("V = 2\n")
    (repo / "tests" / "test_m.py").write_text("from m import V\n\ndef test_v():\n    assert V == 2\n")
    run("add", "-A")
    run("-c", "i18n.commitEncoding=latin-1", "commit", "-q", "-F", "-",
        input=b"Fix caf\xe9 value\n")
    out = tmp_path / "tasks"
    outcomes = mine(str(repo), MineOptions(out=out, stability_runs=1, jobs=1),
                    log=lambda *_: None)
    assert any(o.accepted for o in outcomes)
    assert "caf" in load_tasks(out)[0].prompt


def test_resume_refuses_a_different_model(tmp_path, capsys):
    common = ["run", "--agent", "mock", "--trials", "1", "--tasks",
              str(EXAMPLES / "tasks" / "fix-roman"), "--setups",
              str(EXAMPLES / "setups" / "no-instructions")]
    assert main([*common, "--out", str(tmp_path)]) == 0
    (run_dir,) = tmp_path.iterdir()
    assert main([*common, "--model", "opus", "--resume", str(run_dir)]) == 2
    assert "model was None in the original run" in capsys.readouterr().err


def test_computer_sleep_is_detected_not_waited_out(tmp_path, monkeypatch):
    """On macOS the timeout clock stops during sleep; a slept run must end as an error."""
    import time as real_time
    real_wall_clock = real_time.time
    calls = {"n": 0}

    def wall_clock():
        calls["n"] += 1
        return real_wall_clock() + (0 if calls["n"] == 1 else 3600)  # "slept" an hour
    monkeypatch.setattr(procs.time, "time", wall_clock)
    monkeypatch.setattr(procs, "POLL", 0.1)
    start = real_time.monotonic()
    result = procs.run("sleep 30", cwd=tmp_path, timeout=900)
    assert result.slept and result.returncode is None
    assert real_time.monotonic() - start < 5
