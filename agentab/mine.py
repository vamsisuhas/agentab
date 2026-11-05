"""`agentab mine`: turn a repository's history into verified tasks.

A past commit becomes a task when it fixed something and added or changed tests
for it. For every such commit we check, for real:

  before: the code from just before the commit + the commit's tests  -> must FAIL
  after:  the same, plus the commit's changes to the code            -> must PASS

Only commits that pass both checks are kept, so every task is a real bug fix (or
feature) with tests proving it, and we know the task can be solved by editing
code alone. The commit's own changes become the task's reference solution.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

from . import gitsource, procs
from .ecosystems import ECOSYSTEMS, Ecosystem, detect, render_test_command
from .gitsource import git

# Changes to these files don't have to be repeated by the agent (docs, lockfiles,
# dependency lists...). They are left out of both the solution and the checks.
OTHER_FILES = re.compile(
    r"(^|/)(docs?|\.github|\.circleci|changelog\.d|news)/"
    r"|\.(md|rst|txt|adoc)$"
    r"|(^|/)(CHANGELOG|CHANGES|HISTORY|NEWS|AUTHORS|CONTRIBUTORS|LICENSE|COPYING)[^/]*$"
    r"|(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|uv\.lock|"
    r"Pipfile\.lock|Cargo\.lock|go\.sum|Gemfile\.lock|composer\.lock)$",
    re.IGNORECASE)
TRAILER = re.compile(r"^[A-Za-z][A-Za-z-]*: \S")
ISSUE_REF = re.compile(r"(?:^|[\s(])#(\d+)\b")


@dataclass
class Commit:
    index: int
    sha: str
    parent: str
    date: str
    subject: str
    body: str


@dataclass
class Candidate:
    commit: Commit
    test_files: list[str]      # tests added or changed (become checks/)
    runnable: list[str]        # the ones the test runner is pointed at
    source_files: list[str]    # code added or changed (the solution)
    deleted_sources: list[str]
    lines_changed: int
    test_command: str


@dataclass
class MineOptions:
    out: Path
    limit: int = 20
    max_commits: int = 300
    rev: str = "HEAD"
    since: str | None = None
    paths: list[str] = field(default_factory=list)
    ecosystem: str = "auto"
    test_command: str | None = None
    agent_commands: list[str] = field(default_factory=list)
    test_patterns: list[str] = field(default_factory=list)
    style: str = "tests"          # "tests": agent sees the tests; "message": hidden tests
    min_words: int = 15
    github: bool = False
    setup: str | None = None
    link: list[str] = field(default_factory=list)
    auto_link: bool = True
    python: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    max_lines: int = 300
    max_files: int = 8
    jobs: int = 4
    test_timeout: int = 300
    stability_runs: int = 2
    agent_timeout: int = 900
    snapshot: bool = False
    force: bool = False


@dataclass
class Outcome:
    candidate: Candidate
    accepted: bool
    reason: str = ""
    output: str = ""


class MineError(RuntimeError):
    pass


# ---------------------------------------------------------------- history


def list_commits(repo: Path, opts: MineOptions) -> list[Commit]:
    args = ["log", "--no-merges", f"--max-count={opts.max_commits}",
            "--format=%H%x1f%P%x1f%aI%x1f%B%x1e"]
    if opts.since:
        args.append(f"--since={opts.since}")
    args.append(opts.rev)
    if opts.paths:
        args += ["--", *opts.paths]
    commits = []
    for record in git(repo, *args).split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha, parents, date, message = record.split("\x1f", 3)
        parents = parents.split()
        if len(parents) != 1:  # the very first commit has nothing to start from
            continue
        # Commit messages in old repos aren't always UTF-8; keep them printable.
        message = message.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        subject, _, body = message.strip().partition("\n")
        commits.append(Commit(len(commits), sha, parents[0], date, subject.strip(),
                              clean_body(body)))
    return commits


def clean_body(body: str) -> str:
    """Commit body without trailers like Signed-off-by: and Co-authored-by:."""
    lines = body.strip().splitlines()
    while lines and (TRAILER.match(lines[-1]) or not lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip()


def changed_files(repo: Path, parent: str, sha: str) -> list[tuple[str, str, int]]:
    """(status, path, lines changed) for every file the commit touched."""
    status_out = git(repo, "diff", "--no-renames", "--name-status", "-z", parent, sha)
    numstat_out = git(repo, "diff", "--no-renames", "--numstat", "-z", parent, sha)
    lines: dict[str, int] = {}
    for entry in numstat_out.split("\0"):
        parts = entry.split("\t")
        if len(parts) == 3:
            added, removed, path = parts
            lines[path] = (int(added) if added.isdigit() else 0) + \
                (int(removed) if removed.isdigit() else 0)
    tokens = [t for t in status_out.split("\0") if t]
    return [(tokens[i][0], tokens[i + 1], lines.get(tokens[i + 1], 0))
            for i in range(0, len(tokens) - 1, 2)]


def classify(repo: Path, commit: Commit, eco: Ecosystem,
             opts: MineOptions) -> Candidate | str:
    """A Candidate, or the reason this commit can't become a task."""
    files = changed_files(repo, commit.parent, commit.sha)
    tests, sources, deleted_sources, deleted_tests = [], [], [], []
    source_lines = 0
    for status, path, n in files:
        if eco.is_test(path):
            (deleted_tests if status == "D" else tests).append(path)
        elif OTHER_FILES.search(path):
            continue
        elif status == "D":
            deleted_sources.append(path)
            source_lines += n
        else:
            sources.append(path)
            source_lines += n

    if not tests and not deleted_tests:
        return "doesn't change any tests"
    if not sources and not deleted_sources:
        return "only changes tests"
    if deleted_tests:
        return "deletes or renames test files"
    runnable = [t for t in tests if eco.is_runnable(t)]
    if not runnable:
        return "no runnable test files changed (only helpers or fixtures)"
    if len(sources) + len(deleted_sources) > opts.max_files:
        return f"changes more than {opts.max_files} code files"
    if source_lines > opts.max_lines:
        return f"changes more than {opts.max_lines} lines of code"
    if opts.style == "message":
        words = len(f"{commit.subject} {commit.body}".split())
        if words < opts.min_words and not (opts.github and ISSUE_REF.search(
                f"{commit.subject} {commit.body}")):
            return f"commit message too short to describe the task (under {opts.min_words} words)"

    template = opts.test_command or eco.test_command
    return Candidate(commit, sorted(tests), sorted(runnable), sorted(sources),
                     sorted(deleted_sources), source_lines,
                     render_test_command(template, sorted(runnable)))


# ---------------------------------------------------------------- validation


def _write_files(repo: Path, rev: str, paths: list[str], dest: Path) -> None:
    for path in paths:
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.is_file():
            target.unlink()
        target.write_bytes(gitsource.read_file(repo, rev, path))
        mode = git(repo, "ls-tree", rev, "--", path).split(" ", 1)[0]
        if mode == "100755":
            target.chmod(0o755)


def _link(dest: Path, link_from: Path | None, links: list[str]) -> None:
    for rel in links:
        if link_from is None:
            continue
        target, here = link_from / rel, dest / rel
        if target.exists() and not here.exists():
            here.parent.mkdir(parents=True, exist_ok=True)
            here.symlink_to(target.resolve(), target_is_directory=target.is_dir())


def _run_tests(workdir: Path, command: str, opts: MineOptions) -> procs.Completed:
    python = opts.python or sys.executable
    return procs.run(command.replace("{python}", shlex.quote(python)), cwd=workdir,
                     timeout=opts.test_timeout,
                     env=procs.workspace_env(workdir, python=python, extra=opts.env))


# Exit codes meaning "the tests didn't really run", which must not count as failing:
# 126/127 = the shell couldn't run the command; pytest 4 = usage error, 5 = no tests found.
NOT_A_TEST_RESULT = {126: "test command could not run", 127: "test command not found"}
PYTEST_NOT_A_RESULT = {4: "pytest usage error", 5: "no tests were collected"}


def _invalid_result(result: procs.Completed, command: str) -> str | None:
    code = result.returncode
    if code in NOT_A_TEST_RESULT:
        return NOT_A_TEST_RESULT[code]
    if "pytest" in command and code in PYTEST_NOT_A_RESULT:
        return PYTEST_NOT_A_RESULT[code]
    return None


def validate(repo: Path, cand: Candidate, opts: MineOptions,
             link_from: Path | None, links: list[str]) -> Outcome:
    c = cand.commit
    tmp = Path(tempfile.mkdtemp(prefix="agentab-mine-"))
    try:
        base = tmp / "base"
        gitsource.snapshot(repo, c.parent, base)
        _link(base, link_from, links)
        if opts.setup:
            result = procs.run(opts.setup, cwd=base, timeout=900,
                               env=procs.workspace_env(base, python=opts.python, extra=opts.env))
            if result.returncode != 0:
                return Outcome(cand, False, "setup command failed", result.output[-1500:])
        _write_files(repo, c.sha, cand.test_files, base)

        # The "after" workspace: the same, plus the commit's code changes.
        after = tmp / "after"
        shutil.copytree(base, after, symlinks=True)
        _write_files(repo, c.sha, cand.source_files, after)
        for path in cand.deleted_sources:
            (after / path).unlink(missing_ok=True)

        before_run = _run_tests(base, cand.test_command, opts)
        if before_run.slept:
            return Outcome(cand, False, "computer went to sleep (run mine again to retry)")
        if before_run.timed_out:
            return Outcome(cand, False, "tests time out before the fix")
        if before_run.returncode == 0:
            return Outcome(cand, False, "tests already pass before the fix",
                           before_run.output[-1500:])
        invalid = _invalid_result(before_run, cand.test_command)
        if invalid:
            return Outcome(cand, False, invalid, before_run.output[-1500:])

        for _ in range(max(1, opts.stability_runs)):
            after_run = _run_tests(after, cand.test_command, opts)
            if after_run.slept:
                return Outcome(cand, False, "computer went to sleep (run mine again to retry)")
            if after_run.timed_out:
                return Outcome(cand, False, "tests time out with the fix")
            if after_run.returncode != 0:
                return Outcome(cand, False, "tests fail even with the real fix",
                               after_run.output[-1500:])
        return Outcome(cand, True)
    except (gitsource.GitError, OSError) as e:
        return Outcome(cand, False, f"could not build workspace: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- prompts


def github_repo(repo: Path) -> str | None:
    url = git(repo, "config", "--get", "remote.origin.url", check=False).strip()
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return match.group(1) if match else None


def fetch_issue(slug: str, number: str) -> str | None:
    if shutil.which("gh") is None:
        return None
    proc = subprocess.run(["gh", "api", f"repos/{slug}/issues/{number}",
                           "--jq", "{title: .title, body: .body}"],
                          capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        return None
    data = json.loads(proc.stdout or "{}")
    body = (data.get("body") or "").strip()
    return f"Issue #{number}: {data.get('title', '')}\n\n{body[:3000]}".strip()


def build_prompt(cand: Candidate, opts: MineOptions, slug: str | None) -> str:
    c = cand.commit
    parts = [c.subject]
    if c.body:
        parts.append(c.body[:2000])
    if opts.github and slug:
        for number in dict.fromkeys(ISSUE_REF.findall(f"{c.subject} {c.body}")):
            issue = fetch_issue(slug, number)
            if issue:
                parts.append(issue)
    if opts.style == "tests":
        listing = "\n".join(f"- {t}" for t in cand.runnable)
        parts.append(
            "The tests in these files describe the expected behaviour, and some of them "
            f"currently fail:\n\n{listing}\n\nChange the code so that these tests pass. "
            "Don't edit the test files: they are restored to their original versions "
            "before grading.")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- writing tasks


def slugify(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "change"


def toml_string(value: str) -> str:
    # Multi-line literal strings keep prompts readable. They can't contain ''' or
    # control characters, and TOML drops the newline right after the opening '''.
    readable = "\n" in value and "'''" not in value and not value.endswith("'") and \
        not re.search(r"[\x00-\x08\x0b-\x1f\x7f]", value.replace("\n", "").replace("\t", ""))
    if readable:
        return f"'''\n{value}'''"
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
               .replace("\r", "\\r").replace("\t", "\\t"))
    escaped = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", lambda m: f"\\u{ord(m.group()):04x}", escaped)
    return f'"{escaped}"'


def toml_list(values: list[str]) -> str:
    return "[" + ", ".join(toml_string(v) for v in values) + "]"


def write_task(repo: Path, source: str, cand: Candidate, opts: MineOptions, prompt: str,
               links: list[str], link_from: Path | None, agent_commands: list[str]) -> Path:
    c = cand.commit
    name = f"{c.sha[:8]}-{slugify(c.subject)}"
    task_dir = opts.out / name
    if task_dir.exists():
        shutil.rmtree(task_dir)
    task_dir.mkdir(parents=True)
    _write_files(repo, c.sha, cand.test_files, task_dir / "checks")
    _write_files(repo, c.sha, cand.source_files, task_dir / "solution")
    if opts.snapshot:
        gitsource.snapshot(repo, c.parent, task_dir / "repo")

    lines = [
        f"name = {toml_string(name)}",
        f"prompt = {toml_string(prompt)}",
        f"test = {toml_string(cand.test_command)}",
        f"timeout = {opts.agent_timeout}",
        "",
    ]
    if not opts.snapshot:
        lines += ["[source]", f"git = {toml_string(source)}",
                  f"rev = {toml_string(c.parent)}", ""]
    workspace = [f"show_checks = {'true' if opts.style == 'tests' else 'false'}",
                 f"agent_commands = {toml_list(agent_commands)}"]
    if opts.setup:
        workspace.append(f"setup = {toml_string(opts.setup)}")
    if links and link_from:
        workspace += [f"link = {toml_list(links)}",
                      f"link_from = {toml_string(str(link_from))}"]
    if opts.python:
        workspace.append(f"python = {toml_string(opts.python)}")
    if opts.env:
        pairs = ", ".join(f"{toml_string(k)} = {toml_string(v)}" for k, v in opts.env.items())
        workspace.append(f"env = {{ {pairs} }}")
    lines += ["[workspace]", *workspace, ""]
    lines += ["[mined]", f"commit = {toml_string(c.sha)}", f"date = {toml_string(c.date)}",
              f"subject = {toml_string(c.subject)}",
              f"test_files = {toml_list(cand.test_files)}",
              f"source_files = {toml_list(cand.source_files)}",
              f"deleted_files = {toml_list(cand.deleted_sources)}",
              f"lines_changed = {cand.lines_changed}", ""]
    (task_dir / "task.toml").write_text("\n".join(lines))
    return task_dir


# ---------------------------------------------------------------- main


def _find_links(repo_dir: Path) -> list[str]:
    """Dependency folders worth linking into workspaces (they're never committed)."""
    found = []
    for pattern in ("node_modules", "*/node_modules", "*/*/node_modules"):
        for path in sorted(repo_dir.glob(pattern)):
            rel = path.relative_to(repo_dir).as_posix()
            if path.is_dir() and rel.count("node_modules") == 1:
                found.append(rel)
    return found


def _detect_python(repo_dir: Path | None) -> str | None:
    if repo_dir is None:
        return None
    for venv in (".venv", "venv", "env"):
        for exe in ("bin/python", "Scripts/python.exe"):
            candidate = repo_dir / venv / exe
            if candidate.exists():
                return str(candidate)
    return None


def _check_tools(eco: Ecosystem, test_command: str, python: str) -> None:
    for tool in eco.needs:
        if shutil.which(tool) is None:
            raise MineError(f"`{tool}` is needed to run {eco.name} tests but isn't installed.")
    if "{python}" in test_command and "pytest" in test_command:
        proc = subprocess.run([python, "-c", "import pytest"], capture_output=True)
        if proc.returncode != 0:
            raise MineError(
                f"pytest isn't installed for {python}. Install the project's test "
                "dependencies there, or point --python at the project's virtualenv.")


def mine(source: str, opts: MineOptions, log=print) -> list[Outcome]:
    start = time.monotonic()
    repo = gitsource.resolve_repo(source)
    is_local = not gitsource.is_url(source)
    working_dir = Path(git(repo, "rev-parse", "--show-toplevel", check=False).strip() or repo) \
        if is_local else None
    source_ref = str(working_dir or repo) if is_local else source

    tip = re.split(r"\.\.\.?", opts.rev)[-1] or "HEAD"  # for ranges like v1.0..main
    if git(repo, "rev-parse", "--is-shallow-repository", check=False).strip() == "true":
        log("Warning: this is a shallow clone, so older history is missing. "
            "Run `git fetch --unshallow` to mine further back.")
    files = git(repo, "ls-tree", "-r", "--name-only", "-z", tip).split("\0")
    files = [f for f in files if f]
    if opts.ecosystem == "auto":
        def read(path: str) -> str | None:
            try:
                return gitsource.read_file(repo, tip, path).decode("utf-8", "replace")
            except gitsource.GitError:
                return None
        eco = detect(files, read)
        if eco is None:
            raise MineError("Couldn't tell how to run this project's tests. Pass "
                            "--ecosystem (python, jest, vitest, mocha, npm, go, cargo, rspec, "
                            "minitest, maven, gradle) or --test-command.")
    else:
        if opts.ecosystem not in ECOSYSTEMS:
            raise MineError(f"unknown ecosystem '{opts.ecosystem}'")
        eco = ECOSYSTEMS[opts.ecosystem]
    if opts.test_patterns:
        eco = eco.with_patterns(opts.test_patterns)

    opts.python = opts.python or _detect_python(working_dir)
    # Like Python's venv and Node's node_modules, Ruby should use the project's
    # current, installed gems; an old commit's Gemfile usually doesn't match them.
    if eco.name in ("rspec", "minitest") and working_dir and \
            (working_dir / "Gemfile").exists() and "BUNDLE_GEMFILE" not in opts.env:
        opts.env["BUNDLE_GEMFILE"] = str(working_dir / "Gemfile")
    links = list(opts.link)
    # A setup command like `npm ci` would run inside a link to the user's real
    # node_modules and wipe it, so automatic links are off when there's a setup.
    if opts.auto_link and working_dir and not opts.setup:
        links += [p for p in _find_links(working_dir) if p not in links]
    agent_commands = opts.agent_commands or (
        [] if opts.test_command else eco.agent_commands)
    _check_tools(eco, opts.test_command or eco.test_command, opts.python or sys.executable)

    log(f"Repository: {source_ref}")
    log(f"Ecosystem:  {eco.name}  (tests: {opts.test_command or eco.test_command})")
    if opts.python:
        log(f"Python:     {opts.python}")
    if links:
        log(f"Linking:    {', '.join(links)}")
    for key, value in opts.env.items():
        log(f"Env:        {key}={value}")
    if eco.name in ("jest", "vitest", "mocha", "npm") and "node_modules" not in links \
            and not opts.setup:
        log("Warning: no node_modules found. Run `npm install` in the repo first, or pass "
            "--setup 'npm ci'.")

    commits = list_commits(repo, opts)
    log(f"Scanning {len(commits)} commits...")

    opts.out.mkdir(parents=True, exist_ok=True)
    existing = _existing_tasks(opts.out)
    rejected: dict[str, int] = {}
    candidates: list[Candidate] = []
    already = 0
    for commit in commits:
        if commit.sha in existing and not opts.force:
            already += 1
            continue
        try:
            result = classify(repo, commit, eco, opts)
        except gitsource.GitError:
            result = "history not available (shallow clone?)"
        if isinstance(result, str):
            rejected[result] = rejected.get(result, 0) + 1
        else:
            candidates.append(result)
    wanted = max(0, opts.limit - already)
    log(f"{len(candidates)} commits change both code and tests; checking them "
        f"(need {wanted} more)..." if already else
        f"{len(candidates)} commits change both code and tests; checking them...")

    outcomes: list[Outcome] = []
    accepted: list[Outcome] = []
    slug = github_repo(repo) if opts.github else None
    link_from = working_dir if links else None
    with ThreadPoolExecutor(max_workers=max(1, opts.jobs)) as pool:
        pending = {}
        queue = iter(candidates)

        def fill() -> None:
            while len(pending) < opts.jobs and len(accepted) + len(pending) < wanted:
                cand = next(queue, None)
                if cand is None:
                    return
                pending[pool.submit(validate, repo, cand, opts, link_from, links)] = cand

        fill()
        try:
            _drain(pending, fill, outcomes, accepted, rejected, log)
        except KeyboardInterrupt:
            procs.kill_all()  # otherwise we'd wait for every running check to finish
            for future in pending:
                future.cancel()
            raise

    accepted.sort(key=lambda o: o.candidate.commit.index)
    for outcome in accepted[:wanted]:
        prompt = build_prompt(outcome.candidate, opts, slug)
        write_task(repo, source_ref, outcome.candidate, opts, prompt, links, link_from,
                   agent_commands)

    _write_report(opts.out, source_ref, eco.name, outcomes, rejected, len(commits))
    elapsed = time.monotonic() - start
    total = already + len(accepted[:wanted])
    log(f"\nCreated {len(accepted[:wanted])} new task(s) in {opts.out} "
        f"({total} in total, {elapsed:.0f}s).")
    if rejected:
        log("Skipped commits:")
        for reason, n in sorted(rejected.items(), key=lambda kv: -kv[1]):
            log(f"  {n:>4}  {reason}")
    for hint in _hints(outcomes, candidates, eco):
        log(f"\nHint: {hint}")
    return outcomes


def _drain(pending: dict, fill, outcomes: list, accepted: list, rejected: dict, log) -> None:
    while pending:
        done, _ = wait(pending, return_when=FIRST_COMPLETED)
        for future in done:
            pending.pop(future)
            try:
                outcome = future.result()
            except procs.Stopped:
                continue
            outcomes.append(outcome)
            c = outcome.candidate.commit
            if outcome.accepted:
                accepted.append(outcome)
                log(f"  ✔ {c.sha[:8]} {c.subject[:70]}")
            else:
                rejected[outcome.reason] = rejected.get(outcome.reason, 0) + 1
                log(f"  ✘ {c.sha[:8]} {c.subject[:60]}  ({outcome.reason})")
        fill()


def _existing_tasks(out: Path) -> set[str]:
    import tomllib
    shas = set()
    for toml_file in out.glob("*/task.toml"):
        try:
            sha = tomllib.loads(toml_file.read_text()).get("mined", {}).get("commit")
        except (tomllib.TOMLDecodeError, OSError):
            continue
        if sha:
            shas.add(sha)
    return shas


def _write_report(out: Path, source: str, eco: str, outcomes: list[Outcome],
                  rejected: dict[str, int], scanned: int) -> None:
    report = {
        "repository": source, "ecosystem": eco, "commits_scanned": scanned,
        "skipped": rejected,
        "checked": [{"commit": o.candidate.commit.sha, "subject": o.candidate.commit.subject,
                     "accepted": o.accepted, "reason": o.reason,
                     "test_command": o.candidate.test_command, "output": o.output}
                    for o in outcomes],
    }
    (out / "mine-report.json").write_text(json.dumps(report, indent=2))


def _hints(outcomes: list[Outcome], candidates: list[Candidate], eco: Ecosystem) -> list[str]:
    hints = []
    checked = len(outcomes)
    fail_after = [o for o in outcomes if o.reason == "tests fail even with the real fix"]
    pass_before = [o for o in outcomes if o.reason == "tests already pass before the fix"]
    if not candidates:
        hints.append("No commits changed both code and tests. If this project keeps its "
                     "tests somewhere unusual, say where with --test-pattern "
                     "'path/to/tests/*'. You can also scan further back with --max-commits.")
    if checked >= 3 and len(fail_after) >= checked * 0.7:
        sample = fail_after[0].output.strip().splitlines()[-5:]
        hints.append("Most tests fail even with the real fix applied. That usually means the "
                     "test dependencies aren't available: run agentab from inside the project's "
                     "environment, use --python, --setup or --link. Last lines of one run "
                     "(see mine-report.json for all):\n    " + "\n    ".join(sample))
    if checked >= 3 and len(pass_before) >= checked * 0.7:
        hints.append("Most tests already pass before the fix. The tests may be importing an "
                     "installed copy of the project instead of the checked-out code, or the "
                     "test command isn't running the right tests (check --test-command).")
    return hints
