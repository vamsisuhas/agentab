"""Tasks (what the agent is asked to do) and setups (the agent configuration being tested).

A task is a folder with a task.toml. The starting code comes from either:

    my-task/
      task.toml   # prompt = "...", test = "..."
      repo/       # a copy of the starting code
      checks/     # tests used for grading, copied in AFTER the agent finishes
      solution/   # optional: the correct files (used by the mock agent)

or a commit in a git repository (this is what `agentab mine` creates):

    # task.toml
    [source]
    git = "/path/to/repo"        # or a URL, cloned once into ~/.cache/agentab
    rev = "3f2c1a9..."           # the commit to start from

A setup is a folder of files copied on top of the starting code before the agent
starts, for example a CLAUDE.md, an AGENTS.md, or a .claude/skills/ directory.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Files in a setup folder that are never copied into the workspace.
IGNORED_SETUP_FILES = {".gitkeep", ".DS_Store"}
SETUP_METADATA_PREFIX = ".agentab"


@dataclass
class Task:
    name: str
    path: Path
    prompt: str
    test: str
    timeout: int = 600
    # Starting code from git instead of repo/.
    source_git: str | None = None
    source_rev: str | None = None
    # Workspace preparation, run before the agent (not counted in its time).
    setup: str | None = None
    setup_timeout: int = 900
    link: list[str] = field(default_factory=list)
    link_from: str | None = None
    # Copy checks/ in before the agent too, so it can read (and run) the tests.
    show_checks: bool = False
    # Python interpreter for {python} and for `python` on the agent's PATH
    # (e.g. the project's own .venv). Defaults to the one running agentab.
    python: str | None = None
    # Extra environment variables for setup, agent and tests.
    env: dict[str, str] = field(default_factory=dict)
    # Command prefixes the agent may run, e.g. ["python -m pytest"].
    agent_commands: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    resolved_repo: Path | None = None  # filled in by the runner for git sources

    @property
    def repo(self) -> Path:
        return self.path / "repo"

    @property
    def checks(self) -> Path:
        return self.path / "checks"

    @property
    def solution(self) -> Path:
        return self.path / "solution"

    def agent_test_command(self) -> str:
        """The test command as the agent should type it (plain `python`)."""
        return self.test.replace("{python}", "python")

    def full_prompt(self, can_run_tests: bool) -> str:
        if self.show_checks and can_run_tests:
            return (f"{self.prompt}\n\nYou can run the tests with:\n\n"
                    f"    {self.agent_test_command()}")
        return self.prompt


@dataclass
class Setup:
    name: str
    path: Path

    def files(self) -> list[Path]:
        """Files to copy into the workspace, relative to the setup folder."""
        if not self.path.exists():
            return []
        return sorted(
            p.relative_to(self.path)
            for p in self.path.rglob("*")
            if p.is_file()
            and p.name not in IGNORED_SETUP_FILES
            and not p.name.startswith(SETUP_METADATA_PREFIX)
        )

    def metadata(self) -> dict:
        """Optional .agentab.toml in the setup folder (model choice, mock settings)."""
        meta_file = self.path / ".agentab.toml"
        if meta_file.exists():
            return tomllib.loads(meta_file.read_text())
        return {}


def load_task(path: Path) -> Task:
    config_file = path / "task.toml"
    try:
        config = tomllib.loads(config_file.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{config_file}: invalid TOML ({e})") from None
    for key in ("prompt", "test"):
        if not str(config.get(key, "")).strip():
            raise ValueError(f"{config_file}: missing required key '{key}'")

    source = config.get("source", {})
    workspace = config.get("workspace", {})
    has_repo = (path / "repo").is_dir()
    if not has_repo and not (source.get("git") and source.get("rev")):
        raise ValueError(f"{path}: needs a repo/ folder or a [source] with git and rev")

    link_from = workspace.get("link_from")
    if workspace.get("link") and not link_from:
        git_source = source.get("git", "")
        if not git_source or "://" in git_source or git_source.startswith("git@"):
            raise ValueError(f"{config_file}: [workspace] link needs link_from "
                             "(a local folder to link from)")
        link_from = git_source

    known = {"name", "prompt", "test", "timeout", "source", "workspace"}
    return Task(
        name=config.get("name", path.name),
        path=path,
        prompt=config["prompt"].strip(),
        test=config["test"],
        timeout=int(config.get("timeout", 600)),
        source_git=None if has_repo else source.get("git"),
        source_rev=None if has_repo else source.get("rev"),
        setup=workspace.get("setup"),
        setup_timeout=int(workspace.get("setup_timeout", 900)),
        link=list(workspace.get("link", [])),
        link_from=link_from,
        show_checks=bool(workspace.get("show_checks", False)),
        agent_commands=list(workspace.get("agent_commands", [])),
        python=workspace.get("python"),
        env={str(k): str(v) for k, v in workspace.get("env", {}).items()},
        extra={k: v for k, v in config.items() if k not in known},
    )


def load_tasks(path: Path) -> list[Task]:
    """Load one task folder, or every task folder inside a directory."""
    path = path.resolve()
    if not path.is_dir():
        raise ValueError(f"Tasks folder not found: {path}")
    if (path / "task.toml").exists():
        return [load_task(path)]
    tasks = [load_task(p) for p in sorted(path.iterdir()) if (p / "task.toml").exists()]
    if not tasks:
        raise ValueError(f"No tasks found in {path} (looking for folders with task.toml)")
    names = [t.name for t in tasks]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"Duplicate task names: {', '.join(duplicates)}")
    return tasks


def load_setups(paths: list[Path]) -> list[Setup]:
    setups = []
    for p in paths:
        p = p.resolve()
        if not p.is_dir():
            raise ValueError(f"Setup folder not found: {p}")
        setups.append(Setup(name=p.name, path=p))
    names = [s.name for s in setups]
    if len(set(names)) != len(names):
        raise ValueError(f"Setup folder names must be unique, got: {names}")
    return setups
