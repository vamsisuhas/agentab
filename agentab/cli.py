"""Command line: `agentab run`, `agentab mine` and `agentab report`."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, procs
from .agents import make_agent
from .gitsource import GitError
from .mine import MineError, MineOptions, mine
from .report import print_summary, render_html
from .runner import RunResult, WorkspaceError, load_results, run_all
from .tasks import load_setups, load_tasks


def _progress(result: RunResult, done: int, total: int) -> None:
    color = sys.stderr.isatty()
    if result.error:
        mark = "\033[33m!\033[0m" if color else "ERROR"
    elif result.passed:
        mark = "\033[32m✔\033[0m" if color else "PASS"
    else:
        mark = "\033[31m✘\033[0m" if color else "FAIL"
    detail = f"error: {result.error[:80]}" if result.error else (
        f"{result.duration_s:.0f}s" +
        (f", ${result.cost_usd:.3f}" if result.cost_usd is not None else ""))
    width = len(str(total))
    print(f"  [{done:>{width}}/{total}] {mark} {result.setup} · {result.task} · "
          f"trial {result.trial} ({detail})", file=sys.stderr)


def cmd_run(args: argparse.Namespace) -> int:
    if args.trials < 1 or args.parallel < 1:
        raise ValueError("--trials and --parallel must be at least 1")
    tasks = load_tasks(Path(args.tasks))
    setups = load_setups([Path(p) for p in args.setups])
    agent = make_agent(args.agent, model=args.model, max_budget_usd=args.max_budget_per_run,
                       seed=args.seed, allow_test_runs=not args.no_agent_test_runs)
    agent_label = agent.name + (f" ({args.model})" if args.model else "")

    if args.resume:
        out_dir = Path(args.resume)
        meta = json.loads((out_dir / "run.json").read_text()) if \
            (out_dir / "run.json").exists() else None
        if meta is None:
            raise ValueError(f"{out_dir} is not an agentab results folder (no run.json)")
        if meta.get("setups") != [s.name for s in setups]:
            raise ValueError(f"--resume: setups differ from the original run "
                             f"({', '.join(meta.get('setups', []))})")
        # Mixing runs made with a different agent or settings into one comparison
        # would make the results meaningless.
        for key, value in _run_settings(args).items():
            if key in meta.get("settings", {}) and meta["settings"][key] != value:
                raise ValueError(f"--resume: {key} was {meta['settings'][key]!r} in the "
                                 f"original run, not {value!r}")
        stamp = meta.get("started", "")
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = Path(args.out) / stamp
        meta = None

    total = len(tasks) * len(setups) * args.trials
    print(f"agentab: {len(setups)} setups × {len(tasks)} tasks × {args.trials} trials "
          f"= {total} runs, agent: {agent_label}", file=sys.stderr)
    if args.agent != "mock" and not args.yes and total > 20 and sys.stdin.isatty():
        reply = input(f"This makes {total} real agent runs. Continue? [y/N] ")
        if reply.strip().lower() not in ("y", "yes"):
            print("Cancelled.", file=sys.stderr)
            return 1

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run.json").write_text(json.dumps({
        **(meta or {}),
        "agent": agent_label, "trials": args.trials, "seed": args.seed,
        "settings": _run_settings(args),
        "tasks": sorted({*(meta or {}).get("tasks", []), *(t.name for t in tasks)}),
        "setups": [s.name for s in setups], "started": stamp,
    }, indent=2))

    procs.keep_awake()
    if any(t.source_git for t in tasks):
        print("Preparing repositories...", file=sys.stderr)
    try:
        results = run_all(agent, tasks, setups, args.trials, out_dir,
                          parallel=args.parallel, seed=args.seed,
                          keep_workspaces=args.keep_workspaces,
                          max_consecutive_errors=args.max_consecutive_errors,
                          on_result=_progress)
    except KeyboardInterrupt:
        print(f"\nInterrupted. Finished runs are saved; continue with:\n  "
              f"{_resume_command(args, out_dir)}", file=sys.stderr)
        return 130
    _write_reports(results, out_dir)
    return 0


def _run_settings(args: argparse.Namespace) -> dict:
    return {"agent": args.agent, "model": args.model,
            "max_budget_per_run": args.max_budget_per_run,
            "agent_test_runs": not args.no_agent_test_runs}


def _resume_command(args: argparse.Namespace, out_dir: Path) -> str:
    """The original command line, pointed at the same results folder."""
    original, skip = [], False
    for arg in args.argv:
        if skip:
            skip = False
            continue
        if arg in ("--out", "--resume"):
            skip = True
            continue
        if arg.startswith(("--out=", "--resume=")):
            continue
        original.append(arg)
    return shlex.join(["agentab", *original, "--resume", str(out_dir)])


def _write_reports(results: list[RunResult], out_dir: Path) -> None:
    meta_file = out_dir / "run.json"
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
    # Keep the order from --setups, so the first setup stays the baseline.
    order = {name: i for i, name in enumerate(meta.get("setups", []))}
    results = sorted(results, key=lambda r: (order.get(r.setup, len(order)), r.task, r.trial))
    if not results:
        print("No runs to report.", file=sys.stderr)
        return
    tasks = sorted({r.task for r in results})
    trials = max((r.trial for r in results), default=0)
    title = "agentab report"
    subtitle = (f"Agent: {meta.get('agent', '?')} · {len(tasks)} tasks · "
                f"{trials} trials each · {len(results)} runs · {meta.get('started', '')}")

    setups = list(dict.fromkeys([*meta.get("setups", []), *(r.setup for r in results)]))
    print_summary(results, f"{title} | {subtitle}", setups=setups)
    sys.stdout.flush()
    report_file = out_dir / "report.html"
    report_file.write_text(render_html(results, title, subtitle, setups=setups))
    print(f"\nFull report: {report_file}", file=sys.stderr)


def cmd_report(args: argparse.Namespace) -> int:
    out_dir = Path(args.run_dir)
    _write_reports(load_results(out_dir), out_dir)
    return 0


def _parse_env(pairs: list[str]) -> dict[str, str]:
    env = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise ValueError(f"--env expects KEY=VALUE, got '{pair}'")
        env[key] = value
    return env


def cmd_mine(args: argparse.Namespace) -> int:
    if args.limit < 1:
        raise ValueError("--limit must be at least 1")
    name = Path(args.repo.rstrip("/")).name.removesuffix(".git") or "repo"
    opts = MineOptions(
        out=Path(args.out or f"agentab-tasks/{name}"), limit=args.limit,
        max_commits=args.max_commits, rev=args.rev, since=args.since, paths=args.path,
        ecosystem=args.ecosystem, test_command=args.test_command,
        agent_commands=args.agent_command, test_patterns=args.test_pattern,
        style=args.style, min_words=args.min_words, github=args.github, setup=args.setup,
        link=args.link, auto_link=not args.no_auto_link,
        python=str(Path(args.python).expanduser().absolute()) if args.python else None,
        env=_parse_env(args.env),
        max_lines=args.max_lines, max_files=args.max_files, jobs=args.jobs,
        test_timeout=args.test_timeout, stability_runs=args.stability_runs,
        agent_timeout=args.agent_timeout, snapshot=args.snapshot, force=args.force,
    )
    procs.keep_awake()
    try:
        outcomes = mine(args.repo, opts, log=lambda msg: print(msg, file=sys.stderr))
    except KeyboardInterrupt:
        procs.kill_all()
        print("\nInterrupted.", file=sys.stderr)
        return 130
    if not any(o.accepted for o in outcomes) and not list(opts.out.glob("*/task.toml")):
        return 1
    print(f"\nNext: agentab run --tasks {opts.out} --setups <setup folders>", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agentab",
        description="A/B test your coding agent's setup (CLAUDE.md, skills, tools, model).")
    parser.add_argument("--version", action="version", version=f"agentab {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run tasks against one or more setups")
    run.add_argument("--tasks", required=True,
                     help="a task folder, or a folder containing task folders")
    run.add_argument("--setups", required=True, nargs="+",
                     help="setup folders to compare; the first one is the baseline")
    run.add_argument("--agent", default="claude-code",
                     help="claude-code, mock, or cmd:'<command with {prompt}>'")
    run.add_argument("--model", help="model to use, e.g. haiku or sonnet (claude-code only)")
    run.add_argument("--trials", "-n", type=int, default=3, help="runs per task per setup")
    run.add_argument("--parallel", "-j", type=int, default=4, help="runs at the same time")
    run.add_argument("--max-budget-per-run", type=float, default=1.0,
                     help="stop a single run after this many dollars (claude-code only)")
    run.add_argument("--out", default="agentab-results", help="where to save results")
    run.add_argument("--resume", metavar="RUN_DIR",
                     help="continue an earlier run: skips runs already done, retries errors")
    run.add_argument("--no-agent-test-runs", action="store_true",
                     help="don't let the agent run the tests itself")
    run.add_argument("--max-consecutive-errors", type=int, default=5,
                     help="stop after this many errors in a row, e.g. a usage limit (0 = never)")
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--keep-workspaces", action="store_true",
                     help="don't delete the temporary folders (for debugging)")
    run.add_argument("--yes", "-y", action="store_true", help="don't ask before big runs")
    run.set_defaults(func=cmd_run)

    m = sub.add_parser("mine", help="create tasks from a repository's git history",
                       description="Find past commits that fixed something and changed tests, "
                       "verify each one (tests fail before, pass after), and turn them "
                       "into tasks.")
    m.add_argument("repo", nargs="?", default=".", help="local repository path or git URL")
    m.add_argument("--out", help="where to write tasks (default: agentab-tasks/<repo>)")
    m.add_argument("--limit", type=int, default=20, help="how many tasks to create")
    m.add_argument("--max-commits", type=int, default=300, help="how far back to look")
    m.add_argument("--rev", default="HEAD", help="branch, tag or range, e.g. v2.0..main")
    m.add_argument("--since", help="only commits after this date, e.g. 2025-01-01")
    m.add_argument("--path", action="append", default=[],
                   help="only commits touching this path (repeatable)")
    m.add_argument("--ecosystem", default="auto",
                   help="auto, python, jest, vitest, mocha, npm, go, cargo, rspec, minitest, "
                   "maven or gradle")
    m.add_argument("--test-command", help="test command template, e.g. 'pytest -q {files}'")
    m.add_argument("--agent-command", action="append", default=[],
                   help="command prefix the agent may run, e.g. 'pytest' (repeatable)")
    m.add_argument("--test-pattern", action="append", default=[],
                   help="glob for test files, e.g. 'tests/*' (repeatable; replaces detection)")
    m.add_argument("--style", choices=["tests", "message"], default="tests",
                   help="tests: the agent sees the failing tests (default). message: tests "
                   "stay hidden and the commit message describes the task")
    m.add_argument("--min-words", type=int, default=15,
                   help="--style message: skip commits with shorter messages")
    m.add_argument("--github", action="store_true",
                   help="add the text of linked GitHub issues to prompts (uses `gh`)")
    m.add_argument("--setup", help="command to prepare each workspace, e.g. 'npm ci'")
    m.add_argument("--link", action="append", default=[],
                   help="folder to link into workspaces instead of copying, e.g. node_modules")
    m.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                   help="environment variable for setup, agent and tests (repeatable)")
    m.add_argument("--no-auto-link", action="store_true",
                   help="don't link node_modules folders automatically")
    m.add_argument("--python", help="Python interpreter with the project's dependencies "
                   "(default: the repo's .venv if it has one)")
    m.add_argument("--max-lines", type=int, default=300,
                   help="skip commits changing more lines of code than this")
    m.add_argument("--max-files", type=int, default=8,
                   help="skip commits changing more code files than this")
    m.add_argument("--jobs", "-j", type=int, default=4, help="commits checked at the same time")
    m.add_argument("--test-timeout", type=int, default=300, help="seconds per test run")
    m.add_argument("--stability-runs", type=int, default=2,
                   help="times the fixed version must pass (catches flaky tests)")
    m.add_argument("--agent-timeout", type=int, default=900,
                   help="seconds the agent gets per task")
    m.add_argument("--snapshot", action="store_true",
                   help="copy the starting code into each task (self-contained, shareable)")
    m.add_argument("--force", action="store_true", help="redo commits that already have tasks")
    m.set_defaults(func=cmd_mine)

    report = sub.add_parser("report", help="rebuild the report for a previous run")
    report.add_argument("run_dir", help="e.g. agentab-results/20261017-153000")
    report.set_defaults(func=cmd_report)

    argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(argv)
    args.argv = argv
    try:
        return args.func(args)
    except (ValueError, GitError, MineError, WorkspaceError) as e:
        print(f"agentab: {e}", file=sys.stderr)
        return 2
