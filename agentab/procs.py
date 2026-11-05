"""Running commands (agents, setup steps, tests) inside a workspace."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Completed:
    returncode: int | None  # None when the command timed out (or the computer slept)
    stdout: str
    stderr: str
    slept: bool = False     # the computer went to sleep while it ran

    @property
    def timed_out(self) -> bool:
        return self.returncode is None

    @property
    def output(self) -> str:
        return (self.stdout + self.stderr).strip()


def workspace_env(workdir: Path, base: dict | None = None, python: str | None = None,
                  extra: dict | None = None) -> dict:
    """Environment for anything that runs code in a workspace.

    - `python` resolves to the interpreter running agentab (with its packages).
    - The workspace (and its src/ folder) come first on PYTHONPATH. Without this,
      an editable install (`pip install -e .`) of the original repo would be
      imported instead of the workspace copy, and we'd test the wrong code.
    - CI=true keeps test runners (jest, vitest...) out of interactive watch mode.
    - No .pyc files, so they don't show up as changes.
    """
    env = dict(os.environ if base is None else base)
    for var in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "VIRTUAL_ENV_PROMPT"):
        env.pop(var, None)
    bin_dir = str(Path(python or sys.executable).parent)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    python_path = [str(workdir)]
    if (workdir / "src").is_dir():
        python_path.append(str(workdir / "src"))
    if env.get("PYTHONPATH"):
        python_path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("CI", "true")
    env.update(extra or {})
    return env


_running: set[subprocess.Popen] = set()
_running_lock = threading.Lock()
_stopping = threading.Event()


class Stopped(RuntimeError):
    """Raised instead of returning a result once agentab is shutting down."""


def stopping() -> bool:
    return _stopping.is_set()


def keep_awake() -> None:
    """On macOS, stop the computer from idle-sleeping while agentab runs.

    `caffeinate -w <pid>` exits by itself when agentab does. (Closing the lid on
    battery still sleeps; runs caught by that are marked as errors and retried.)
    """
    if sys.platform == "darwin" and shutil.which("caffeinate"):
        subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def allow_new_runs() -> None:
    """Undo kill_all() (for tests and long-lived embedding)."""
    _stopping.clear()


def kill_all() -> None:
    """Stop every running command and refuse new ones (used on Ctrl-C)."""
    _stopping.set()
    with _running_lock:
        for proc in list(_running):
            _kill_group(proc)


def run(cmd: str | list[str], cwd: Path, timeout: float, env: dict | None = None,
        input_text: str | None = None) -> Completed:
    """Run a command; on timeout, kill it and everything it started.

    Output goes to temporary files rather than pipes: a background process the
    command leaves behind would keep a pipe open and make us wait for it, while
    with files we only wait for the command itself to exit.
    """
    if _stopping.is_set():
        raise Stopped("agentab is stopping")
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            cmd, cwd=cwd, env=env, shell=isinstance(cmd, str),
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=out, stderr=err,
            start_new_session=True,  # its own process group, so we can kill all of it
        )
        with _running_lock:
            _running.add(proc)
        try:
            if input_text is not None:
                proc.stdin.write(input_text.encode())
                proc.stdin.close()
            returncode, slept = _wait(proc, timeout)
            # Clean up anything the command left running in its process group.
            _kill_group(proc)
            proc.wait()
        except BaseException:
            _kill_group(proc)
            raise
        finally:
            with _running_lock:
                _running.discard(proc)
        if _stopping.is_set():
            raise Stopped("agentab is stopping")  # killed by us: not a real result
        return Completed(returncode, _read(out), _read(err), slept)


# If the wall clock moves this much further than the awake-time clock, the
# computer slept. A run that slept isn't a fair attempt: network connections die
# (agents hang forever waiting on them) and timeouts stop counting.
SLEEP_GAP = 60.0
POLL = 2.0


def _wait(proc: subprocess.Popen, timeout: float) -> tuple[int | None, bool]:
    """Wait for the command; returns (returncode or None on timeout, slept)."""
    awake_start, wall_start = time.monotonic(), time.time()
    while True:
        try:
            return proc.wait(timeout=POLL), False
        except subprocess.TimeoutExpired:
            pass
        awake = time.monotonic() - awake_start
        if (time.time() - wall_start) - awake > SLEEP_GAP:
            return None, True
        if awake >= timeout:
            return None, False


def _read(f) -> str:
    f.seek(0)
    # Real repos contain non-UTF-8 output; never crash on it.
    return f.read().decode("utf-8", errors="replace")


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, AttributeError):  # AttributeError: Windows
        try:
            proc.kill()
        except ProcessLookupError:
            pass
