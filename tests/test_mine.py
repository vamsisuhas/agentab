import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from agentab import gitsource, procs
from agentab.agents import MockAgent
from agentab.ecosystems import ECOSYSTEMS, detect, render_test_command
from agentab.mine import MineOptions, clean_body, mine, toml_string
from agentab.report import summarize
from agentab.runner import load_results, run_all
from agentab.tasks import load_setups, load_tasks

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull}


def sh(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True, env={**os.environ, **GIT_ENV}).stdout


def commit(repo: Path, message: str, files: dict[str, str | None]) -> str:
    for path, content in files.items():
        target = repo / path
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", message)
    return sh(repo, "rev-parse", "HEAD").strip()


CALC_V1 = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - abs(b)\n"
CALC_V2 = CALC_V1.replace("a - abs(b)", "a - b")
TEST_V1 = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
TEST_V2 = TEST_V1.replace("from calc import add", "from calc import add, sub") + \
    "\n\ndef test_sub_negative():\n    assert sub(1, -2) == 3\n"


@pytest.fixture
def history(tmp_path):
    """A repo whose history has one good task and one example of each thing to skip."""
    repo = tmp_path / "proj"
    repo.mkdir()
    sh(repo, "init", "-q", "-b", "main")
    shas = {}
    shas["root"] = commit(repo, "Initial commit", {
        "calc.py": CALC_V1, "tests/test_calc.py": TEST_V1, "tests/test_old.py": TEST_V1,
        "pyproject.toml": "[project]\nname = 'proj'\n",
        ".gitattributes": "/tests export-ignore\n",  # must not hide tests from tasks
    })
    shas["fix"] = commit(repo, "Fix sub() with negative numbers\n\nsub(1, -2) returned -1.\n\n"
                         "Signed-off-by: Someone <s@example.com>",
                         {"calc.py": CALC_V2, "tests/test_calc.py": TEST_V2})
    shas["docs"] = commit(repo, "Update docs", {"README.md": "# proj\n"})
    shas["refactor"] = commit(repo, "Refactor add", {
        "calc.py": CALC_V2.replace("return a + b", "result = a + b\n    return result"),
        "tests/test_calc.py": TEST_V2 + "\n\ndef test_add_zero():\n    assert add(0, 0) == 0\n",
    })
    shas["tests_only"] = commit(repo, "More tests", {
        "tests/test_more.py": "def test_true():\n    assert True\n"})
    shas["broken"] = commit(repo, "Add mul (incomplete)", {
        "calc.py": (repo / "calc.py").read_text() + "\n\ndef mul(a, b):\n    return a + b\n",
        "tests/test_mul.py": "from calc import mul\n\n\ndef test_mul():\n    assert mul(2, 3) == 6\n",
    })
    shas["delete_test"] = commit(repo, "Drop old test", {
        "tests/test_old.py": None, "calc.py": (repo / "calc.py").read_text() + "\n# tidy\n"})
    sh(repo, "checkout", "-q", "-b", "side")
    commit(repo, "Side branch docs", {"docs/x.md": "x\n"})
    sh(repo, "checkout", "-q", "main")
    sh(repo, "merge", "-q", "--no-ff", "side", "-m", "Merge side")
    # Uncommitted change in the working copy: must never leak into tasks.
    (repo / "calc.py").write_text("BROKEN = True\n")
    return repo, shas


def options(out: Path, **kw) -> MineOptions:
    return MineOptions(out=out, jobs=2, stability_runs=1, test_timeout=60,
                       python=sys.executable, **kw)


def test_mine_finds_only_the_real_fix(history, tmp_path):
    repo, shas = history
    out = tmp_path / "tasks"
    outcomes = mine(str(repo), options(out), log=lambda *_: None)

    reasons = {o.candidate.commit.sha: o.reason for o in outcomes}
    assert reasons[shas["refactor"]] == "tests already pass before the fix"
    assert reasons[shas["broken"]] == "tests fail even with the real fix"
    assert [o.candidate.commit.sha for o in outcomes if o.accepted] == [shas["fix"]]

    report = (out / "mine-report.json").read_text()
    for reason in ("doesn't change any tests", "only changes tests",
                   "deletes or renames test files"):
        assert reason in report

    (task,) = load_tasks(out)
    assert task.source_rev == shas["root"]          # starts just before the fix
    assert task.show_checks is True
    assert "Fix sub() with negative numbers" in task.prompt
    assert "Signed-off-by" not in task.prompt
    assert "tests/test_calc.py" in task.prompt
    assert (task.checks / "tests" / "test_calc.py").read_text() == TEST_V2
    assert (task.solution / "calc.py").read_text() == CALC_V2
    assert task.agent_commands == ["python -m pytest"]


def test_mined_task_runs_and_grades_correctly(history, tmp_path):
    repo, _ = history
    out = tmp_path / "tasks"
    mine(str(repo), options(out), log=lambda *_: None)
    tasks = load_tasks(out)
    setups = []
    for name, rate in (("never", 0.0), ("always", 1.0)):
        folder = tmp_path / name
        folder.mkdir()
        (folder / ".agentab.toml").write_text(f"[mock]\nsuccess_rate = {rate}\n")
        setups.append(folder)
    results = run_all(MockAgent(), tasks, load_setups(setups), trials=2,
                      out_dir=tmp_path / "results", parallel=2)
    by_setup = {s.name: s for s in summarize(results)}
    assert by_setup["never"].passed == 0      # unsolved: the hidden tests fail
    assert by_setup["always"].passed == 2     # the real fix passes
    assert all(r.error is None for r in results)


def test_mine_is_idempotent_and_snapshot_is_self_contained(history, tmp_path):
    repo, shas = history
    out = tmp_path / "tasks"
    mine(str(repo), options(out, snapshot=True), log=lambda *_: None)
    (task,) = load_tasks(out)
    assert task.source_git is None and (task.repo / "calc.py").read_text() == CALC_V1
    assert (task.repo / "tests" / "test_calc.py").exists()   # export-ignore didn't drop it
    assert not (task.repo / ".git").exists()
    assert "BROKEN" not in (task.repo / "calc.py").read_text()

    # Running again finds the existing task and doesn't re-check that commit.
    outcomes = mine(str(repo), options(out, snapshot=True), log=lambda *_: None)
    assert shas["fix"] not in [o.candidate.commit.sha for o in outcomes]
    assert len(load_tasks(out)) == 1


def test_message_style_hides_tests_and_needs_a_real_description(history, tmp_path):
    repo, _ = history
    out = tmp_path / "tasks"
    mine(str(repo), options(out, style="message", min_words=3), log=lambda *_: None)
    (task,) = load_tasks(out)
    assert task.show_checks is False
    assert "tests/test_calc.py" not in task.prompt

    out2 = tmp_path / "strict"
    outcomes = mine(str(repo), options(out2, style="message", min_words=50),
                    log=lambda *_: None)
    assert not any(o.accepted for o in outcomes)
    assert "commit message too short" in (out2 / "mine-report.json").read_text()


def test_mine_from_url_clone(history, tmp_path, monkeypatch):
    repo, shas = history
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    out = tmp_path / "tasks"
    mine(f"file://{repo}", options(out), log=lambda *_: None)
    (task,) = load_tasks(out)
    assert task.source_git == f"file://{repo}"
    results = run_all(MockAgent(), [task], load_setups([tmp_path]), trials=1,
                      out_dir=tmp_path / "results")
    assert results[0].error is None


def test_snapshot_ignores_dirty_working_copy_and_keeps_modes(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    sh(repo, "init", "-q")
    (repo / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (repo / "run.sh").chmod(0o755)
    (repo / "link").symlink_to("run.sh")
    commit(repo, "init", {"a.txt": "committed\n"})
    (repo / "a.txt").write_text("dirty\n")
    dest = tmp_path / "out"
    gitsource.snapshot(repo, "HEAD", dest)
    assert (dest / "a.txt").read_text() == "committed\n"
    assert os.access(dest / "run.sh", os.X_OK)
    assert (dest / "link").is_symlink()
    assert sh(repo, "status", "--porcelain").strip() == "M a.txt"  # user's repo untouched


def test_classification():
    py, jest, go = ECOSYSTEMS["python"], ECOSYSTEMS["jest"], ECOSYSTEMS["go"]
    assert py.is_runnable("tests/test_x.py") and py.is_runnable("pkg/x_test.py")
    assert py.is_test("tests/fixtures/data.json") and py.is_test("conftest.py")
    assert not py.is_test("django/test/client.py")      # nested test/ can be real code
    assert not py.is_test("src/testing_utils.py")
    assert jest.is_runnable("src/a.test.ts") and jest.is_runnable("src/__tests__/a.js")
    assert jest.is_test("test/fixtures/x.json") and not jest.is_runnable("test/fixtures/x.json")
    assert go.is_runnable("pkg/a_test.go") and go.is_test("pkg/testdata/in.txt")
    assert ECOSYSTEMS["maven"].is_runnable("core/src/test/java/a/FooTest.java")
    assert ECOSYSTEMS["cargo"].is_runnable("tests/parse.rs")
    assert not ECOSYSTEMS["cargo"].is_runnable("src/lib.rs")


def test_render_test_command():
    files = ["pkg/a/x_test.go", "pkg/a/y_test.go", "main_test.go"]
    assert render_test_command("go test {packages}", files) == "go test ./ ./pkg/a"
    assert render_test_command("pytest {files}", ["tests/it's.py"]) == "pytest 'tests/it'\"'\"'s.py'"
    assert render_test_command("mvn -Dtest={classes}", ["src/test/java/FooTest.java"]) == \
        "mvn -Dtest=FooTest"


def test_detect_ecosystem():
    read = {"package.json": '{"devDependencies": {"vitest": "^2"}}'}.get
    assert detect(["package.json", "src/a.test.ts"], read).name == "vitest"
    assert detect(["pyproject.toml", "tests/test_a.py"], read).name == "python"
    assert detect(["go.mod", "a_test.go", "tools/x.py"], read).name == "go"
    assert detect(["README.md"], read) is None


def test_toml_string_round_trips_awkward_text():
    for text in ['plain', 'quote " and backslash \\ and tab\t', "multi\nline ''' text",
                 "multi\nline", "unicode ✔ 中文", "control \x01 char"]:
        assert tomllib.loads(f"v = {toml_string(text)}")["v"] == text


def test_clean_body_strips_trailers():
    assert clean_body("Details here.\n\nSigned-off-by: A <a@a>\nCo-authored-by: B <b@b>") == \
        "Details here."


def test_workspace_env_puts_workspace_first(tmp_path):
    (tmp_path / "src").mkdir()
    env = procs.workspace_env(tmp_path, base={"PYTHONPATH": "/elsewhere", "PATH": "/bin"})
    assert env["PYTHONPATH"].split(os.pathsep) == [str(tmp_path), str(tmp_path / "src"),
                                                    "/elsewhere"]
    assert env["PATH"].startswith(str(Path(sys.executable).parent))


def test_run_timeout_kills_child_processes(tmp_path):
    marker = tmp_path / "still-running"
    result = procs.run(f"(sleep 2; touch {marker}) & sleep 30", cwd=tmp_path, timeout=0.5)
    assert result.timed_out
    import time
    time.sleep(2.5)
    assert not marker.exists()


def test_env_is_saved_in_tasks_and_used_by_tests(history, tmp_path):
    repo, _ = history
    out = tmp_path / "tasks"
    opts = options(out, env={"AGENTAB_TEST_FLAG": "on", "QUOTE": 'a "b"'})
    mine(str(repo), opts, log=lambda *_: None)
    (task,) = load_tasks(out)
    assert task.env == {"AGENTAB_TEST_FLAG": "on", "QUOTE": 'a "b"'}
    env = procs.workspace_env(tmp_path, extra=task.env)
    assert env["AGENTAB_TEST_FLAG"] == "on"


def test_parse_env():
    from agentab.cli import _parse_env
    assert _parse_env(["A=1", "B=x=y", "C="]) == {"A": "1", "B": "x=y", "C": ""}
    with pytest.raises(ValueError):
        _parse_env(["NOEQUALS"])
