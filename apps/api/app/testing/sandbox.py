"""Sandboxed execution of a generated test via subprocess. See
docs/architecture for the full design — the critical framing (Task 10) is
repeated here because it must never be lost in refactoring:

THIS IS NOT AN OS-LEVEL SECURITY SANDBOX. No container, no VM, no chroot,
no seccomp profile, no network namespace. What this module actually
provides:

  - a fresh, disposable temporary directory, deleted in a `finally` block
    regardless of how execution ends — same guaranteed-cleanup idiom as
    app/ingestion/checkout.py::checkout_repository.
  - subprocess.run with an explicit argv list, `shell=False` always (the
    default; never overridden) — same discipline as
    app/ingestion/git_adapter.py.
  - an explicit, bounded wall-clock timeout.
  - a minimal, credential-free environment (no DATABASE_URL, no API keys,
    no ambient secrets, `HOME` pointed at the disposable directory so a
    test can't read the real user's `~/.netrc`/`~/.aws`/etc.) — same
    pattern as git_adapter.py's `_git_env()`.
  - bounded, truncated captured stdout/stderr, so a misbehaving process
    can't exhaust memory via its own output.

What it does NOT do: prevent a test process from making a network call
(no firewall or network namespace is applied — if the host can reach the
network, so can the subprocess), prevent it from reading other files the
OS file permissions allow, or bound its CPU/memory short of the timeout.
Real enforcement of those requires OS/container-level isolation (e.g. a
gVisor/Firecracker/network-isolated container runner) — explicitly out of
scope for this phase, and tracked as a required later hardening phase
rather than silently assumed away.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from app.testing.models import ExecutionResult

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_OUTPUT_BYTES = 64_000
MAX_MATERIALIZED_FILES = 50


class UnsafeMaterializationError(Exception):
    """A file path supplied for materialization would escape the sandbox
    directory — refused before any write happens."""


@dataclass(frozen=True)
class MaterializedFile:
    relative_path: str
    content: str


def _truncate(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "...<truncated>"


def _safe_write(root: Path, relative_path: str, content: str) -> Path:
    if relative_path.startswith("/") or ".." in Path(relative_path).parts:
        raise UnsafeMaterializationError(f"unsafe relative path: {relative_path!r}")
    destination = root / relative_path
    if not destination.resolve().is_relative_to(root.resolve()):
        raise UnsafeMaterializationError(f"path escapes sandbox directory: {relative_path!r}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    return destination


def _ensure_package_inits(root: Path, written_paths: list[Path]) -> None:
    """So `from pkg.module import thing`-style imports in a materialized
    test can resolve: every ancestor directory (up to the sandbox root) of
    every written file gets an `__init__.py` if one doesn't already exist
    among the files actually written."""
    for path in written_paths:
        directory = path.parent
        while directory != root and directory.is_relative_to(root):
            init_file = directory / "__init__.py"
            if not init_file.exists():
                init_file.write_text("", encoding="utf-8")
            directory = directory.parent


def _sandbox_env(sandbox_root: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(sandbox_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(sandbox_root),
        "LC_ALL": "C",
    }


def run_generated_test(
    context_files: list[MaterializedFile],
    test_file: MaterializedFile,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> ExecutionResult:
    """Materializes `context_files` plus `test_file` into a fresh temporary
    directory and runs exactly that one test file under pytest — never an
    LLM-supplied command, never a caller-supplied argv. Deterministic
    command construction: always `[sys.executable, "-m", "pytest",
    test_file.relative_path, "-q", "--no-header"]`, nothing else."""
    all_files = [*context_files[:MAX_MATERIALIZED_FILES], test_file]

    tmp_dir = tempfile.mkdtemp(prefix="devlens-testexec-")
    tmp_path = Path(tmp_dir)
    try:
        written: list[Path] = []
        for file in all_files:
            written.append(_safe_write(tmp_path, file.relative_path, file.content))
        _ensure_package_inits(tmp_path, written)

        command = [sys.executable, "-m", "pytest", test_file.relative_path, "-q", "--no-header"]
        started = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, shell=False, see module docstring
                command,
                cwd=tmp_path,
                env=_sandbox_env(tmp_path),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            return ExecutionResult(
                status="timeout",
                exit_code=None,
                stdout=_truncate(stdout, max_output_bytes),
                stderr=_truncate(stderr, max_output_bytes),
                timed_out=True,
                duration_ms=duration_ms,
            )
        except OSError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            return ExecutionResult(
                status="error",
                exit_code=None,
                stdout="",
                stderr=_truncate(str(exc), max_output_bytes),
                timed_out=False,
                duration_ms=duration_ms,
            )

        duration_ms = int((time.monotonic() - started) * 1000)
        status = "passed" if completed.returncode == 0 else "failed"
        return ExecutionResult(
            status=status,
            exit_code=completed.returncode,
            stdout=_truncate(completed.stdout, max_output_bytes),
            stderr=_truncate(completed.stderr, max_output_bytes),
            timed_out=False,
            duration_ms=duration_ms,
        )
    except UnsafeMaterializationError as exc:
        return ExecutionResult(
            status="error",
            exit_code=None,
            stdout="",
            stderr=str(exc),
            timed_out=False,
            duration_ms=0,
        )
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
