"""Runs every (setup, task, trial) combination and grades each run.

One run, step by step:
  1. Build a fresh workspace: the task's starting code (from repo/ or a git
     commit), linked folders like node_modules, the setup's files (CLAUDE.md...),
     and the visible tests if the task shows them. Run the task's setup command.
  2. Commit everything to a throwaway git repo, so we can see exactly what the
     agent changed afterwards.
  3. Let the agent work on the task prompt. If the agent hits an API error,
     retry in a fresh workspace; if it keeps failing, record an error.
  4. Save the agent's changes as a patch.
  5. Undo the agent's changes to test files and test config, copy in the task's
     checks/, then run the task's test command. Exit code 0 means the run passed.
"""

from __future__ import annotations

import fnmatch
import json
import os
import random
import shlex
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Callable

from . import gitsource, procs
from .agents import AgentResult, classify_failure
from .tasks import Setup, Task

# Ignore the user's global git hooks, templates and ignore rules in workspaces.
GIT = ["git", "-c", "user.name=agentab", "-c", "user.email=agentab@localhost",
       "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={os.devnull}",
       "-c", f"core.excludesFile={os.devnull}", "-c", "core.autocrlf=false"]
RETRY_DELAYS = (15, 60)  # seconds to wait before retrying a temporary API error


@dataclass
class RunResult:
    setup: str
    task: str
    trial: int
    passed: bool
    agent_finished: bool
    duration_s: float
    cost_usd: float | None
    turns: int | None
    tokens: int | None
    lines_changed: int
    agent_message: str
    test_output: str
    error: str | None = None  # the run said nothing about the setup; left out of stats

    @classmethod
    def from_dict(cls, data: dict) -> "RunResult":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class WorkspaceError(RuntimeError):
    pass


class ComputerSlept(RuntimeError):
    pass


def _git(workdir: Path, *args: str) -> str:
    proc = procs.run([*GIT, *args], cwd=workdir, timeout=600)
    if proc.returncode != 0:
        raise WorkspaceError(f"git {args[0]} failed in workspace: {proc.output[-500:]}")
    return proc.stdout


def prepare_sources(tasks: list[Task]) -> None:
    """Find (or clone) every git repository the tasks start from, once, up front."""
    resolved: dict[str, Path] = {}
    for task in tasks:
        if not task.source_git:
            continue
        if task.source_git not in resolved:
            resolved[task.source_git] = gitsource.resolve_repo(task.source_git)
        task.resolved_repo = resolved[task.source_git]
        gitsource.ensure_revision(task.resolved_repo, task.source_rev, task.source_git)


def _copy_into(src: Path, dest: Path) -> None:
    """Copy src's files over dest, replacing (never writing through) symlinks.

    The agent controls the workspace, so a file we overwrite might have been
    turned into a symlink pointing anywhere, even outside the workspace.
    """
    for item in sorted(src.rglob("*")):
        if item.is_dir() and not item.is_symlink():
            continue
        target = dest / item.relative_to(src)
        _make_real_dirs(dest, target.parent)
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        if item.is_symlink():
            os.symlink(os.readlink(item), target)
        else:
            shutil.copy2(item, target)


def _make_real_dirs(root: Path, folder: Path) -> None:
    """mkdir -p, replacing any symlink or file in the way."""
    current = root
    for part in folder.relative_to(root).parts:
        current = current / part
        if current.is_symlink() or current.is_file():
            current.unlink()
        if not current.exists():
            current.mkdir()


def prepare_workspace(workdir: Path, task: Task, setup: Setup) -> str:
    """Build the workspace and return the baseline commit the agent starts from."""
    # 1. Starting code.
    if task.source_git:
        if task.resolved_repo is None:
            prepare_sources([task])
        gitsource.snapshot(task.resolved_repo, task.source_rev, workdir)
    else:
        shutil.copytree(task.repo, workdir, dirs_exist_ok=True, symlinks=True)

    # 2. Shared folders (node_modules, .venv...) linked instead of copied.
    linked = []
    for rel in task.link:
        target = Path(task.link_from).expanduser() / rel
        here = workdir / rel
        if not target.exists() or here.exists() or here.is_symlink():
            continue
        here.parent.mkdir(parents=True, exist_ok=True)
        here.symlink_to(target.resolve(), target_is_directory=target.is_dir())
        linked.append(rel)

    # 3. Visible tests, then the setup's own files (they win on conflicts).
    #    Repos often symlink CLAUDE.md -> AGENTS.md; replace the link, don't write through it.
    if task.show_checks and task.checks.is_dir():
        _copy_into(task.checks, workdir)
    for rel in setup.files():
        dest = workdir / rel
        _make_real_dirs(workdir, dest.parent)
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        shutil.copy2(setup.path / rel, dest)

    # 4. The task's setup command (e.g. `npm ci`), before the baseline commit so
    #    whatever it creates doesn't count as the agent's change.
    if task.setup:
        result = procs.run(task.setup, cwd=workdir, timeout=task.setup_timeout,
                           env=procs.workspace_env(workdir, python=task.python, extra=task.env))
        if result.slept:
            raise WorkspaceError("the computer went to sleep during the setup command")
        if result.returncode != 0:
            reason = "timed out" if result.timed_out else f"exit code {result.returncode}"
            raise WorkspaceError(f"setup command failed ({reason}): {result.output[-800:]}")

    # 5. Baseline commit. Linked folders are excluded so they're never diffed.
    _git(workdir, "init", "-q", "--template=")
    exclude = workdir / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("".join(f"/{rel}\n" for rel in linked))
    _git(workdir, "add", "-A")
    _git(workdir, "commit", "-q", "--allow-empty", "-m", "start")
    return _git(workdir, "rev-parse", "HEAD").strip()


def _changes(workdir: Path, base: str) -> list[tuple[str, str]]:
    """(status, path) of everything that differs from the baseline commit.

    Compared against the baseline commit, not HEAD, so it still works if the
    agent made commits of its own.
    """
    _git(workdir, "add", "-A")
    tokens = [t for t in _git(workdir, "diff", "--cached", "--no-renames", "--name-status",
                              "-z", base).split("\0") if t]
    return [(tokens[i][0], tokens[i + 1]) for i in range(0, len(tokens) - 1, 2)]


def changed_lines(workdir: Path, base: str) -> tuple[int, str]:
    """How many lines the agent added or removed, and the full patch."""
    _git(workdir, "add", "-A")
    numstat = _git(workdir, "diff", "--cached", "--numstat", base)
    total = 0
    for line in numstat.splitlines():
        added, removed, *_ = line.split("\t")
        if added.isdigit() and removed.isdigit():  # "-" means binary file
            total += int(added) + int(removed)
    return total, _git(workdir, "diff", "--cached", "--binary", base)


# Files that control how tests run. An agent could make every test "pass" by
# editing them (e.g. a conftest.py that skips everything), so they're put back
# before grading. New test files the agent wrote are kept; they aren't graded.
TEST_DIRS = {"test", "tests", "spec", "__tests__", "testdata"}
TEST_CONFIG_NAMES = {"conftest.py", "pytest.ini", "tox.ini", ".rspec", "spec_helper.rb",
                     "rails_helper.rb", "test_helper.rb"}
TEST_CONFIG_GLOBS = (".mocharc*", "jest.config.*", "jest.setup.*", "vitest.config.*",
                     "vitest.workspace.*", "karma.conf.*", "setupTests.*")


def _is_test_config(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in TEST_CONFIG_NAMES or any(fnmatch.fnmatch(name, g) for g in TEST_CONFIG_GLOBS)


def restore_tests(workdir: Path, base: str) -> list[str]:
    """Undo the agent's changes to test files and test config. Returns what was restored."""
    restored = []
    for status, path in _changes(workdir, base):
        in_test_dir = bool(TEST_DIRS & set(path.split("/")[:-1]))
        if not (_is_test_config(path) or in_test_dir):
            continue
        if status == "A":
            if _is_test_config(path):  # e.g. a new conftest.py
                (workdir / path).unlink(missing_ok=True)
                restored.append(path)
            continue
        _git(workdir, "checkout", base, "--", path)
        restored.append(path)
    return restored


def test_command(task: Task) -> str:
    return task.test.replace("{python}", shlex.quote(task.python or sys.executable))


def grade(workdir: Path, task: Task, base: str) -> tuple[bool, str]:
    restored = restore_tests(workdir, base)
    if task.checks.is_dir():
        _copy_into(task.checks, workdir)
    result = procs.run(test_command(task), cwd=workdir, timeout=task.timeout,
                       env=procs.workspace_env(workdir, python=task.python, extra=task.env))
    if result.slept:
        raise ComputerSlept("the computer went to sleep while the tests ran")
    note = f"(restored test files the agent changed: {', '.join(restored)})\n" \
        if restored else ""
    if result.timed_out:
        return False, note + "tests timed out\n" + result.output[-2500:]
    return result.returncode == 0, note + result.output[-3000:]


def run_one(agent, task: Task, setup: Setup, trial: int, patches_dir: Path | None,
            keep_workspace: bool, sleep: Callable[[float], None] = time.sleep) -> RunResult:
    run_id = f"{setup.name}/{task.name}/{trial}"

    def failed(error: str | None, message: str = "", result: AgentResult | None = None,
               test_output: str = "") -> RunResult:
        return RunResult(setup.name, task.name, trial, passed=False,
                         agent_finished=bool(result and result.finished),
                         duration_s=round(result.duration_s, 1) if result else 0.0,
                         cost_usd=result.cost_usd if result else None,
                         turns=result.turns if result else None,
                         tokens=result.tokens if result else None, lines_changed=0,
                         agent_message=message, test_output=test_output, error=error)

    result: AgentResult | None = None
    for attempt in range(len(RETRY_DELAYS) + 1):
        if attempt:
            sleep(RETRY_DELAYS[attempt - 1] * random.uniform(0.8, 1.2))
        # A fresh workspace per attempt: a retry must not see a half-finished attempt.
        workdir = Path(tempfile.mkdtemp(prefix="agentab-"))
        try:
            try:
                base = prepare_workspace(workdir, task, setup)
            except (WorkspaceError, gitsource.GitError, OSError) as e:
                return failed(f"could not prepare workspace: {e}")

            result = agent.run(workdir, task, setup, run_id)
            if result.error:
                if result.retryable and attempt < len(RETRY_DELAYS):
                    continue
                return failed(result.error, result.message, result)

            try:
                lines, patch = changed_lines(workdir, base)
                if patches_dir is not None:
                    patch_file = patches_dir / setup.name / f"{task.name}-{trial}.patch"
                    patch_file.parent.mkdir(parents=True, exist_ok=True)
                    patch_file.write_text(patch, errors="replace")
                passed, test_output = grade(workdir, task, base)
            except ComputerSlept as e:
                return failed(str(e), result.message, result)
            except (WorkspaceError, OSError) as e:
                # The agent left the workspace unusable (deleted .git, broke files...).
                # That's the agent's doing, so it's a failed run, not an error.
                return failed(None, result.message, result,
                              f"could not grade, the agent broke the workspace: {e}")
            return RunResult(
                setup=setup.name, task=task.name, trial=trial,
                passed=passed, agent_finished=result.finished,
                duration_s=round(result.duration_s, 1), cost_usd=result.cost_usd,
                turns=result.turns, tokens=result.tokens, lines_changed=lines,
                agent_message=result.message, test_output=test_output,
            )
        finally:
            if keep_workspace:
                print(f"  kept workspace for {run_id}: {workdir}", file=sys.stderr)
            else:
                shutil.rmtree(workdir, ignore_errors=True)
    raise AssertionError("unreachable")


def run_all(agent, tasks: list[Task], setups: list[Setup], trials: int,
            out_dir: Path, parallel: int = 1, seed: int = 0,
            keep_workspaces: bool = False, max_consecutive_errors: int = 5,
            on_result: Callable[[RunResult, int, int], None] | None = None,
            sleep: Callable[[float], None] = time.sleep) -> list[RunResult]:
    """Run everything not already in out_dir/results.jsonl (so interrupted runs resume)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results_file = out_dir / "results.jsonl"
    previous = load_results(out_dir) if results_file.exists() else []
    done = {(r.setup, r.task, r.trial) for r in previous if r.error is None}
    # Keep earlier successful runs; earlier errors get another chance.
    kept = [r for r in previous if r.error is None]
    # Rewrite the file: drops old errors and any half-written last line.
    results_file.write_text("".join(json.dumps(asdict(r)) + "\n" for r in kept))

    prepare_sources(tasks)
    jobs = [(task, setup, trial)
            for setup in setups for task in tasks for trial in range(1, trials + 1)
            if (setup.name, task.name, trial) not in done]
    # Shuffle so no setup always runs first or last (rate limits, time of day...).
    random.Random(seed).shuffle(jobs)

    lock = threading.Lock()
    results: list[RunResult] = list(kept)
    consecutive_errors = 0
    stop = threading.Event()
    total = len(kept) + len(jobs)

    def record(result: RunResult) -> None:
        nonlocal consecutive_errors
        if procs.stopping():
            return  # killed by Ctrl-C: whatever it says isn't a real result
        with lock:
            results.append(result)
            with results_file.open("a") as f:
                f.write(json.dumps(asdict(result)) + "\n")
            consecutive_errors = consecutive_errors + 1 if result.error else 0
            if max_consecutive_errors and consecutive_errors >= max_consecutive_errors:
                stop.set()
            if on_result:
                on_result(result, len(results), total)

    def job(task: Task, setup: Setup, trial: int) -> None:
        if stop.is_set():
            return
        # Recorded here, in the worker, so the stop flag is set before it takes another job.
        try:
            result = run_one(agent, task, setup, trial, out_dir / "patches",
                             keep_workspaces, sleep)
        except procs.Stopped:
            return
        except Exception as e:  # a bug in agentab must not take the whole experiment down
            result = RunResult(setup.name, task.name, trial, False, False, 0.0, None, None,
                               None, 0, "", "", error=f"internal error: {e!r}")
        record(result)

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = [pool.submit(job, *j) for j in jobs]
        try:
            for future in as_completed(futures):
                future.result()  # re-raise any unexpected crash
        except KeyboardInterrupt:
            stop.set()
            for future in futures:
                future.cancel()
            procs.kill_all()
            raise

    if stop.is_set():
        print(f"\nStopped early: {max_consecutive_errors} runs in a row failed with "
              f"errors (last: {results[-1].error}). Fix the problem, then continue with "
              f"`agentab run ... --resume {out_dir}`.", file=sys.stderr)

    order = {s.name: i for i, s in enumerate(setups)}
    return sorted(results, key=lambda r: (order.get(r.setup, len(order)), r.task, r.trial))


def load_results(out_dir: Path) -> list[RunResult]:
    results_file = out_dir / "results.jsonl"
    if not results_file.exists():
        raise ValueError(f"No results found in {out_dir}")
    results = []
    for number, line in enumerate(results_file.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            result = RunResult.from_dict(json.loads(line))
        except (json.JSONDecodeError, TypeError):
            # A run killed mid-write leaves a broken last line; skip it.
            print(f"warning: skipping unreadable line {number} in {results_file}",
                  file=sys.stderr)
            continue
        # Re-check unfinished runs with today's error detection, so a better
        # detector also fixes results recorded before it existed.
        if result.error is None and not result.agent_finished and len(result.agent_message) < 600:
            failure = classify_failure(result.agent_message)
            if failure:
                result.error = failure[0]
        results.append(result)
    return results
