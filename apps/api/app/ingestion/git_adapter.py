"""A narrow Git subprocess adapter — exactly the operations repository
ingestion needs, nothing more. Not a generic command-execution framework.

Safety properties, all deliberate (see docs/architecture/repository-ingestion.md
section 3 and 6):
- `git` is always invoked as an argv list, never `shell=True`, and no user
  input is ever interpolated into a shell string.
- A minimal, deterministic, credential-free environment is used for every
  call — no ambient `~/.gitconfig`, no credential helpers, no interactive
  prompts, no LFS smudge downloads.
- Every call has an explicit timeout.
- Output is captured and length-bounded before being attached to an
  exception or returned, so a misbehaving process can't blow up memory.
"""

import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_MAX_CAPTURED_OUTPUT = 64_000
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")


class GitCommandError(Exception):
    """A `git` invocation failed (non-zero exit)."""

    def __init__(self, command: list[str], returncode: int, stderr: str) -> None:
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"git {' '.join(command)} exited {returncode}: {stderr[:500]}")


class GitTimeoutError(GitCommandError):
    """A `git` invocation exceeded its timeout and was killed."""

    def __init__(self, command: list[str], timeout: float) -> None:
        self.command = command
        self.timeout = timeout
        Exception.__init__(self, f"git {' '.join(command)} timed out after {timeout}s")


class InvalidCommitShaError(Exception):
    """`git rev-parse HEAD` returned something that isn't a valid commit SHA."""


class PathEscapeError(Exception):
    """A repository-relative path would resolve outside the checkout
    directory — a path-traversal or symlink-escape attempt."""


class FileTooLargeError(Exception):
    def __init__(self, relative_path: str, max_bytes: int) -> None:
        self.relative_path = relative_path
        self.max_bytes = max_bytes
        super().__init__(f"{relative_path!r} exceeds the {max_bytes}-byte read limit")


@dataclass(frozen=True)
class CommitMetadata:
    author_name: str
    author_email: str
    committed_at: datetime
    parent_sha: str | None


def _git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/echo",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "LC_ALL": "C",
    }


def _truncate(text: str) -> str:
    if len(text) <= _MAX_CAPTURED_OUTPUT:
        return text
    return text[:_MAX_CAPTURED_OUTPUT] + "...<truncated>"


def _run(args: list[str], *, cwd: Path | None, timeout: float) -> subprocess.CompletedProcess[str]:
    full_args = ["git", *args]
    try:
        result = subprocess.run(
            full_args,
            cwd=cwd,
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitTimeoutError(full_args, timeout) from exc

    if result.returncode != 0:
        raise GitCommandError(full_args, result.returncode, _truncate(result.stderr))
    return result


def clone_repository(clone_url: str, destination: Path, *, timeout: float) -> None:
    """Shallow, single-branch, submodule-free, LFS-free clone. `clone_url`
    must already be a normalized `RepositoryIdentity.clone_url` in
    production — this function does not itself validate it, which is
    deliberate: it's also used directly against local filesystem paths in
    tests (see tests/ingestion/conftest.py), so it can't hard-code an
    HTTPS-only or file-disallowed restriction here without breaking that.
    Production callers always go through `app.ingestion.service.ingest_repository`,
    which validates the URL via `url_policy.normalize_repository_url` first.

    No `protocol.file.allow=never` hardening is needed here specifically
    because `--no-recurse-submodules` means `.gitmodules` is never even read
    during this clone — there is no secondary protocol-resolution surface
    within this single invocation to restrict."""
    _run(
        [
            "clone",
            "--depth",
            "1",
            "--no-tags",
            "--single-branch",
            "--no-recurse-submodules",
            clone_url,
            str(destination),
        ],
        cwd=None,
        timeout=timeout,
    )


def resolve_head_commit(repo_dir: Path, *, timeout: float) -> str:
    """Resolve and validate the exact commit SHA HEAD points at. Raises
    `InvalidCommitShaError` if the output isn't a well-formed commit SHA —
    branch names are never accepted here."""
    result = _run(["rev-parse", "HEAD"], cwd=repo_dir, timeout=timeout)
    sha = result.stdout.strip()
    if not _COMMIT_SHA_RE.match(sha):
        raise InvalidCommitShaError(f"not a valid commit SHA: {sha!r}")
    return sha


def resolve_current_branch(repo_dir: Path, *, timeout: float) -> str | None:
    """The branch name actually checked out — informational only, never
    treated as revision identity. Returns None for a detached HEAD."""
    result = _run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir, timeout=timeout)
    branch = result.stdout.strip()
    return None if branch == "HEAD" else branch


def read_commit_metadata(repo_dir: Path, *, timeout: float) -> CommitMetadata:
    result = _run(
        ["log", "-1", "--format=%an%x00%ae%x00%cI", "HEAD"], cwd=repo_dir, timeout=timeout
    )
    author_name, author_email, committed_at_raw = result.stdout.rstrip("\n").split("\x00")

    parent_sha: str | None
    try:
        parent_result = _run(["rev-parse", "HEAD^"], cwd=repo_dir, timeout=timeout)
        parent_sha = parent_result.stdout.strip()
    except GitCommandError:
        parent_sha = None  # root commit — no parent

    return CommitMetadata(
        author_name=author_name,
        author_email=author_email,
        committed_at=datetime.fromisoformat(committed_at_raw),
        parent_sha=parent_sha,
    )


def list_tracked_files(repo_dir: Path, *, timeout: float) -> list[str]:
    """Git's own tracked-file view (NUL-separated, so filenames containing
    spaces/newlines are split correctly), sorted for determinism."""
    result = _run(["ls-files", "-z"], cwd=repo_dir, timeout=timeout)
    paths = [p for p in result.stdout.split("\x00") if p]
    return sorted(paths)


def safe_join(repo_dir: Path, relative_path: str) -> Path:
    """Joins without resolving. Deliberately does NOT call `.resolve()` here
    — resolving follows symlinks, which would defeat `is_symlink()` checks
    done on the result (a resolved path to a symlink's target is never
    itself "a symlink"). Defensive even though `git ls-files` shouldn't ever
    produce an unsafe path — this is explicitly an untrusted-input boundary
    (see docs/architecture/repository-ingestion.md section 3)."""
    if relative_path.startswith("/") or ".." in Path(relative_path).parts:
        raise PathEscapeError(f"unsafe relative path: {relative_path!r}")
    return repo_dir / relative_path


def _verify_contained(repo_dir: Path, path: Path, relative_path: str) -> None:
    """Resolves and checks containment — called only after confirming `path`
    isn't a symlink, so this resolve step can't be tricked by one."""
    if not path.resolve().is_relative_to(repo_dir.resolve()):
        raise PathEscapeError(f"path escapes checkout directory: {relative_path!r}")


def is_symlink(repo_dir: Path, relative_path: str) -> bool:
    return safe_join(repo_dir, relative_path).is_symlink()


def file_size(repo_dir: Path, relative_path: str) -> int:
    # lstat, not stat: never follow a symlink to measure something else's size.
    return safe_join(repo_dir, relative_path).lstat().st_size


def read_file_at_revision(repo_dir: Path, relative_path: str, *, max_bytes: int) -> bytes:
    """Reads a tracked file's content directly from the checkout's working
    tree. This is safe because the checkout is an immutable, freshly-cloned
    snapshot of exactly one commit that nothing else writes to — reading
    from disk is equivalent to `git show <sha>:<path>` here, without a
    subprocess call per file. Refuses to follow a symlink, and refuses to
    read past `max_bytes` (raises rather than silently truncating)."""
    path = safe_join(repo_dir, relative_path)
    if path.is_symlink():
        raise PathEscapeError(f"refusing to read a symlink: {relative_path!r}")
    _verify_contained(repo_dir, path, relative_path)
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise FileTooLargeError(relative_path, max_bytes)
    return data
