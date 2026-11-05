"""Per-language knowledge: which files are tests, and how to run them.

Each ecosystem decides two things about a changed file:
  is_test:     belongs to the tests (test files, fixtures, helpers). These become
               the task's checks/ and are never part of the expected solution.
  is_runnable: a test file the test runner can be pointed at directly.

Test command templates can use these placeholders:
  {files}      the runnable test files changed by the commit
  {packages}   Go packages of those files, e.g. ./internal/parser
  {classes}    Java test class names, comma separated
  {gradle_tests}  --tests <Class> for each Java test class
  {python}     the Python interpreter
"""

from __future__ import annotations

import fnmatch
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Callable

_JS_EXT = r"\.[cm]?[jt]sx?$"


def _dirs(path: str) -> list[str]:
    return path.split("/")[:-1]


def _py_test(path: str) -> bool:
    name, dirs = path.rsplit("/", 1)[-1], _dirs(path)
    return (_py_runnable(path) or name == "conftest.py"
            or (dirs[:1] in (["tests"], ["test"]))  # top-level test folders
            or "tests" in dirs)                      # nested tests/ folders


def _py_runnable(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _js_test(path: str) -> bool:
    dirs = _dirs(path)
    return (_js_named(path)
            or any(d in ("test", "tests", "__tests__", "__mocks__", "__fixtures__")
                   for d in dirs))


def _js_named(path: str) -> bool:
    return bool(re.search(r"\.(test|spec)" + _JS_EXT, path)) or \
        ("__tests__" in _dirs(path) and bool(re.search(_JS_EXT, path)))


def _js_in_test_dir(path: str) -> bool:
    dirs = _dirs(path)
    helpers = {"fixtures", "__fixtures__", "helpers", "__mocks__", "utils", "support"}
    return (bool(re.search(_JS_EXT, path)) and any(d in ("test", "tests") for d in dirs)
            and not helpers & set(dirs))


def _go_test(path: str) -> bool:
    return path.endswith("_test.go") or "testdata" in _dirs(path)


def _rust_test(path: str) -> bool:
    return "tests" in _dirs(path)


def _rust_runnable(path: str) -> bool:
    return path.endswith(".rs") and _dirs(path)[-1:] == ["tests"]


def _ruby_test(path: str) -> bool:
    return (_ruby_runnable(path) or _dirs(path)[:1] in (["spec"], ["test"])
            or path.endswith("_helper.rb") and ("spec" in _dirs(path) or "test" in _dirs(path)))


def _ruby_runnable(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith("_spec.rb") or name.endswith("_test.rb") or \
        (name.startswith("test_") and name.endswith(".rb"))


def _java_test(path: str) -> bool:
    return "/src/test/" in f"/{path}"


def _java_runnable(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return _java_test(path) and bool(re.search(r"(Test|Tests|TestCase|IT)\.(java|kt)$", name))


@dataclass
class Ecosystem:
    name: str
    test_command: str
    agent_commands: list[str]
    is_test: Callable[[str], bool]
    is_runnable: Callable[[str], bool]
    needs: list[str] = field(default_factory=list)  # executables that must be installed

    def with_patterns(self, patterns: list[str]) -> "Ecosystem":
        """User-provided globs decide what counts as a test instead."""
        def matches(path: str) -> bool:
            return any(fnmatch.fnmatch(path, p) for p in patterns)
        return Ecosystem(self.name, self.test_command, self.agent_commands, matches,
                         matches, self.needs)


ECOSYSTEMS = {
    "python": Ecosystem("python", "{python} -m pytest -q -p no:cacheprovider {files}",
                        ["python -m pytest"], _py_test, _py_runnable),
    "jest": Ecosystem("jest", "npx --no-install jest --ci {files}", ["npx jest"],
                      _js_test, _js_named, ["npx"]),
    "vitest": Ecosystem("vitest", "npx --no-install vitest run {files}", ["npx vitest"],
                        _js_test, _js_named, ["npx"]),
    "mocha": Ecosystem("mocha", "npx --no-install mocha {files}", ["npx mocha"],
                       _js_test, lambda p: _js_named(p) or _js_in_test_dir(p), ["npx"]),
    "npm": Ecosystem("npm", "npm test -- {files}", ["npm test"],
                     _js_test, lambda p: _js_named(p) or _js_in_test_dir(p), ["npm"]),
    "go": Ecosystem("go", "go test {packages}", ["go test"], _go_test,
                    lambda p: p.endswith("_test.go"), ["go"]),
    "cargo": Ecosystem("cargo", "cargo test --quiet", ["cargo test"], _rust_test,
                       _rust_runnable, ["cargo"]),
    "rspec": Ecosystem("rspec", "bundle exec rspec {files}", ["bundle exec rspec"],
                       _ruby_test, lambda p: p.endswith("_spec.rb"), ["bundle"]),
    "minitest": Ecosystem(
        "minitest",
        "bundle exec ruby -Itest -Ilib -e 'ARGV.each { |f| require File.expand_path(f) }' {files}",
        ["bundle exec ruby", "bundle exec rake test"], _ruby_test,
        lambda p: _ruby_runnable(p) and not p.endswith("_spec.rb"), ["bundle"]),
    "maven": Ecosystem("maven", "mvn -q -DfailIfNoTests=false "
                       "-Dsurefire.failIfNoSpecifiedTests=false test -Dtest={classes}",
                       ["mvn"], _java_test, _java_runnable, ["mvn"]),
    "gradle": Ecosystem("gradle", "./gradlew test --console=plain {gradle_tests}",
                        ["./gradlew"], _java_test, _java_runnable),
}


def render_test_command(template: str, runnable: list[str]) -> str:
    """Fill in the placeholders for one commit's test files."""
    packages = sorted({"./" + str(PurePosixPath(f).parent) if "/" in f else "./"
                       for f in runnable})
    classes = sorted({PurePosixPath(f).stem for f in runnable})
    return (template
            .replace("{files}", " ".join(shlex.quote(f) for f in runnable))
            .replace("{packages}", " ".join(shlex.quote(p) for p in packages))
            .replace("{classes}", ",".join(classes))
            .replace("{gradle_tests}", " ".join(f"--tests {shlex.quote(c)}" for c in classes)))


def detect(files: list[str], read: Callable[[str], str | None]) -> Ecosystem | None:
    """Pick the ecosystem whose test files are most common in the repository.

    `files` lists every file in the repo; `read(path)` returns a file's content.
    """
    present = set(files)
    candidates: list[str] = []
    if present & {"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini"} or \
            any(f.endswith(".py") for f in files):
        candidates.append("python")
    if "package.json" in present:
        candidates.append(_js_runner(read("package.json") or "{}"))
    if "go.mod" in present:
        candidates.append("go")
    if "Cargo.toml" in present:
        candidates.append("cargo")
    if "Gemfile" in present:
        candidates.append("rspec" if ".rspec" in present or any(
            f.startswith("spec/") for f in files) else "minitest")
    if "pom.xml" in present:
        candidates.append("maven")
    elif present & {"build.gradle", "build.gradle.kts"} and "gradlew" in present:
        candidates.append("gradle")
    if not candidates:
        return None
    counts = {c: sum(ECOSYSTEMS[c].is_runnable(f) for f in files) for c in candidates}
    best = max(candidates, key=lambda c: counts[c])  # ties: the earlier, more common one
    return ECOSYSTEMS[best] if counts[best] > 0 else None


def _js_runner(package_json: str) -> str:
    try:
        data = json.loads(package_json)
    except json.JSONDecodeError:
        return "npm"
    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    script = str(data.get("scripts", {}).get("test", ""))
    for runner in ("vitest", "jest", "mocha"):
        if runner in deps or re.search(rf"\b{runner}\b", script):
            return runner
    return "npm"
