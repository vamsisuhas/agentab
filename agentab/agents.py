"""Agent adapters. Each one runs a coding agent inside a workspace folder.

Adding a new agent means writing a class with a `run()` method that returns an
AgentResult. agentab never calls a model API itself: it starts the agent's own
CLI, so the agent uses whatever login or API key it is already configured with.

An important distinction: if the agent tried and got it wrong, that's a failed
run and counts against the setup. If the agent never got a fair chance (API
outage, rate limit, not logged in), that's an *error*: the run is retried or
left out of the statistics, so infrastructure problems can't make a setup look bad.
"""

from __future__ import annotations

import json
import random
import re
import shlex
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import procs
from .tasks import Setup, Task


@dataclass
class AgentResult:
    finished: bool  # False if the agent crashed or timed out
    duration_s: float
    cost_usd: float | None = None
    turns: int | None = None
    tokens: int | None = None
    message: str = ""  # the agent's final message, or what went wrong
    error: str | None = None  # set when the run says nothing about the setup
    retryable: bool = False   # True if trying again later may work


# Patterns that mean "the agent never got a fair chance". They are deliberately
# strict: an agent's output is full of test logs, and a failing test called
# test_returns_500 or test_connection_pool must never be mistaken for an outage
# (that would retry the run and drop the failure from the statistics).
_AUTH = re.compile(r"not logged in|please run /login|invalid api key|invalid x-api-key|"
                   r"authentication_error|oauth token (has )?expired", re.IGNORECASE)
_LIMIT = re.compile(r"usage limit (reached|exceeded)|limit will reset|quota exceeded|"
                    r"insufficient_quota|credit balance is too low", re.IGNORECASE)
_TRANSIENT = re.compile(r"^\s*API Error\b|stream idle timeout|overloaded_error|"
                        r"rate_limit_error|api_error|"
                        r"\bapi error\b.{0,30}\b(429|5\d\d)\b|\b(429|5\d\d)\b.{0,30}\bapi\b|"
                        r"\becon(nreset|nrefused)\b|\betimedout\b|socket hang up",
                        re.IGNORECASE)


def classify_failure(text: str) -> tuple[str, bool] | None:
    """(error, retryable) if `text` describes an infrastructure problem, else None."""
    if _AUTH.search(text):
        return "agent is not logged in or the API key is invalid", False
    if _LIMIT.search(text):
        return "usage limit reached", False
    if _TRANSIENT.search(text):
        return "temporary API error", True
    return None


def classify_status(status: int | str) -> tuple[str, bool]:
    """Error for an HTTP status the agent reported from its model API."""
    code = int(status) if str(status).isdigit() else 0
    if code in (401, 403):
        return "agent is not logged in or the API key is invalid", False
    if code == 429:
        return "rate limited by the API", True
    return f"API error {status}", True


class ClaudeCodeAgent:
    """Runs Claude Code in non-interactive mode (`claude -p`)."""

    name = "claude-code"

    def __init__(self, model: str | None = None, max_budget_usd: float | None = None,
                 allow_test_runs: bool = True):
        self.model = model
        self.max_budget_usd = max_budget_usd
        self.allow_test_runs = allow_test_runs

    def command(self, prompt: str, model: str | None = None,
                allowed_commands: list[str] = ()) -> list[str]:
        cmd = [
            "claude", "-p", prompt,
            "--output-format", "json",
            # Allow file edits without asking. Shell commands stay blocked,
            # except the test commands listed below.
            "--permission-mode", "acceptEdits",
            "--no-session-persistence",
            # Ignore your personal ~/.claude settings so every setup starts equal.
            "--setting-sources", "project,local",
        ]
        if allowed_commands:
            rules = [rule for c in allowed_commands for rule in (f"Bash({c})", f"Bash({c} *)")]
            cmd += ["--allowedTools", ",".join(rules)]
        model = model or self.model
        if model:
            cmd += ["--model", model]
        if self.max_budget_usd:
            cmd += ["--max-budget-usd", str(self.max_budget_usd)]
        return cmd

    def run(self, workdir: Path, task: Task, setup: Setup, run_id: str) -> AgentResult:
        if shutil.which("claude") is None:
            return AgentResult(False, 0.0, error="`claude` command not found. Install "
                               "Claude Code first: https://claude.com/claude-code")
        # A setup can pick its own model in .agentab.toml: [agent] model = "sonnet"
        model = setup.metadata().get("agent", {}).get("model")
        can_run = self.allow_test_runs and task.show_checks
        cmd = self.command(task.full_prompt(can_run), model,
                           task.agent_commands if can_run else [])

        start = time.monotonic()
        proc = procs.run(cmd, cwd=workdir, timeout=task.timeout,
                         env=procs.workspace_env(workdir, python=task.python, extra=task.env))
        duration = time.monotonic() - start
        if proc.slept:
            return AgentResult(False, duration, error="the computer went to sleep during the run")
        if proc.timed_out:
            return AgentResult(False, duration, message="timed out")

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            # Claude Code didn't get to run the task (crash, login problem...).
            failure = classify_failure(proc.stderr[-2000:] + proc.stdout[-500:])
            error, retryable = failure or (
                f"agent output was not JSON: {proc.output[-300:]}", True)
            return AgentResult(False, duration, message=proc.output[-1000:], error=error,
                               retryable=retryable)

        message = str(data.get("result", ""))[:2000]
        # Running out of the per-run budget or turns is the agent's own failure.
        hit_own_limit = str(data.get("subtype", "")).startswith("error_max")
        if data.get("is_error") and not hit_own_limit:
            status = data.get("api_error_status")
            if status:
                error, retryable = classify_status(status)
                return AgentResult(False, duration, message=message, error=error,
                                   retryable=retryable)
            # No status: only a short, error-shaped message (not a work log) counts.
            failure = classify_failure(message) if len(message) < 600 else None
            if failure:
                return AgentResult(False, duration, message=message, error=failure[0],
                                   retryable=failure[1])

        usage = data.get("usage") or {}
        tokens = sum(usage.get(k) or 0 for k in (
            "input_tokens", "output_tokens",
            "cache_creation_input_tokens", "cache_read_input_tokens",
        ))
        return AgentResult(
            finished=not data.get("is_error", False),
            duration_s=duration,
            cost_usd=data.get("total_cost_usd"),
            turns=data.get("num_turns"),
            tokens=tokens or None,
            message=message,
        )


class CommandAgent:
    """Runs any CLI agent from a command template, e.g. "codex exec {prompt}".

    `{prompt}` is replaced with the task prompt. Cost and token counts are not
    available this way, only pass/fail and time.
    """

    def __init__(self, template: str):
        if "{prompt}" not in template:
            raise ValueError("Command template must contain {prompt}")
        self.template = template
        self.name = shlex.split(template)[0]

    def run(self, workdir: Path, task: Task, setup: Setup, run_id: str) -> AgentResult:
        if shutil.which(self.name) is None:
            return AgentResult(False, 0.0, error=f"command not found: {self.name}")
        prompt = task.full_prompt(can_run_tests=True)
        cmd = [part.replace("{prompt}", prompt) for part in shlex.split(self.template)]
        start = time.monotonic()
        proc = procs.run(cmd, cwd=workdir, timeout=task.timeout,
                         env=procs.workspace_env(workdir, python=task.python, extra=task.env))
        duration = time.monotonic() - start
        if proc.slept:
            return AgentResult(False, duration, error="the computer went to sleep during the run")
        if proc.timed_out:
            return AgentResult(False, duration, message="timed out")
        output = proc.output[-2000:]
        if proc.returncode != 0:
            # Only the agent's own error stream, not its work log (which has test output).
            failure = classify_failure(proc.stderr[-500:])
            if failure:
                return AgentResult(False, duration, message=output, error=failure[0],
                                   retryable=failure[1])
        return AgentResult(finished=proc.returncode == 0, duration_s=duration, message=output)


class MockAgent:
    """A fake agent for demos and tests. Free and instant, no AI involved.

    It "solves" a task by copying the task's solution/ files, with a success
    rate set per setup in that setup's .agentab.toml:

        [mock]
        success_rate = 0.8
    """

    name = "mock"
    DEFAULT_SUCCESS_RATE = 0.5

    def __init__(self, seed: int = 0):
        self.seed = seed

    def run(self, workdir: Path, task: Task, setup: Setup, run_id: str) -> AgentResult:
        mock = setup.metadata().get("mock", {})
        rate = mock.get("success_rate", self.DEFAULT_SUCCESS_RATE)
        rng = random.Random(f"{self.seed}/{run_id}")

        if rng.random() < mock.get("error_rate", 0.0):
            return AgentResult(False, 1.0, error="temporary API error (simulated)",
                               retryable=mock.get("retryable", True))
        solved = rng.random() < rate and task.solution.is_dir()
        if solved:
            shutil.copytree(task.solution, workdir, dirs_exist_ok=True, symlinks=True)
        return AgentResult(
            finished=True,
            duration_s=round(rng.uniform(20, 90), 1),
            cost_usd=round(mock.get("cost_usd", 0.04) * rng.uniform(0.6, 1.4), 4),
            turns=rng.randint(3, 12),
            tokens=rng.randint(15_000, 60_000),
            message="(mock) applied solution" if solved else "(mock) gave up",
        )


def make_agent(spec: str, model: str | None = None, max_budget_usd: float | None = None,
               seed: int = 0, allow_test_runs: bool = True):
    """Turn the --agent value into an agent object."""
    if spec == "claude-code":
        return ClaudeCodeAgent(model=model, max_budget_usd=max_budget_usd,
                               allow_test_runs=allow_test_runs)
    if spec == "mock":
        return MockAgent(seed=seed)
    if spec.startswith("cmd:"):
        return CommandAgent(spec.removeprefix("cmd:"))
    raise ValueError(f"Unknown agent '{spec}'. Use claude-code, mock, or cmd:<command with {{prompt}}>")
