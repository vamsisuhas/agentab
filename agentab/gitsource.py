"""Read files out of git repositories without touching the user's checkout.

Snapshots come straight from git's object database, so uncommitted changes in
the user's working copy never leak in, `export-ignore` rules don't drop files,
and the snapshot never contains a .git folder (which would hold the answer).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import threading
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: Path | str, *args: str, env: dict | None = None, check: bool = True) -> str:
    # surrogateescape: non-UTF-8 paths survive the round trip back to the filesystem.
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          encoding="utf-8", errors="surrogateescape",
                          env={**os.environ, **(env or {})})
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def is_url(source: str) -> bool:
    return "://" in source or source.startswith("git@")


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "agentab" / "repos"


_clone_lock = threading.Lock()


def resolve_repo(source: str) -> Path:
    """Local repo path for a task source: a local path, or a URL cloned into the cache."""
    if not is_url(source):
        path = Path(source).expanduser().resolve()
        if not path.exists():
            raise GitError(f"repository not found: {path}")
        git(path, "rev-parse", "--git-dir")  # raises if it's not a git repo
        return path
    with _clone_lock:
        dest = cache_dir() / hashlib.sha1(source.encode()).hexdigest()[:16]
        if not (dest / "HEAD").exists() and not (dest / ".git").exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(["git", "clone", "--quiet", "--bare", source, str(dest)],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                raise GitError(f"could not clone {source}: {proc.stderr.strip()}")
        return dest


def ensure_revision(repo: Path, rev: str, source: str) -> None:
    """Make sure `rev` exists locally, fetching it once for cached clones."""
    if _has_commit(repo, rev):
        return
    if is_url(source):
        # Fetch into a ref of our own so `git gc` can never prune the commit.
        git(repo, "fetch", "--quiet", "origin", f"{rev}:refs/agentab/{rev}", check=False)
        if not _has_commit(repo, rev):
            git(repo, "fetch", "--quiet", "origin", "+refs/heads/*:refs/heads/*", check=False)
    if not _has_commit(repo, rev):
        hint = " (is it a shallow clone? try `git fetch --unshallow`)" if \
            git(repo, "rev-parse", "--is-shallow-repository", check=False).strip() == "true" \
            else ""
        raise GitError(f"commit {rev} not found in {source}{hint}")


def _has_commit(repo: Path, rev: str) -> bool:
    proc = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{rev}^{{commit}}"],
                          capture_output=True)
    return proc.returncode == 0


def snapshot(repo: Path, rev: str, dest: Path) -> None:
    """Write every file of commit `rev` into `dest` (no .git folder)."""
    dest.mkdir(parents=True, exist_ok=True)
    fd, index = tempfile.mkstemp(prefix="agentab-index-")
    os.close(fd)
    os.unlink(index)  # git wants to create the index file itself
    env = {"GIT_INDEX_FILE": index}
    try:
        git(repo, "read-tree", rev, env=env)
        # core.symlinks etc. come from the repo config; work-tree is our empty folder.
        git(repo, f"--work-tree={dest}", "checkout-index", "--all", "--force", env=env)
    finally:
        if os.path.exists(index):
            os.unlink(index)


def read_file(repo: Path, rev: str, path: str) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"],
                          capture_output=True)
    if proc.returncode != 0:
        raise GitError(f"could not read {path} at {rev}")
    return proc.stdout
