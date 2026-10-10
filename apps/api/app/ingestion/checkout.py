"""Safe, temporary, cleanup-guaranteed Git checkout.

The checkout directory is created under the system temp directory (never
inside the application source tree) and removed in a `finally` block,
implemented as a context manager so cleanup can't be skipped by a caller
forgetting to call it — on success, on a Git failure, or on an exception
raised later by the caller while still inside the `with` block.
"""

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.ingestion.git_adapter import (
    CommitMetadata,
    clone_repository,
    read_commit_metadata,
    resolve_current_branch,
    resolve_head_commit,
)


@dataclass(frozen=True)
class Checkout:
    path: Path
    commit_sha: str
    branch: str | None
    commit_metadata: CommitMetadata


@contextmanager
def checkout_repository(clone_url: str, *, timeout: float) -> Iterator[Checkout]:
    """Clone `clone_url` into a fresh temporary directory, resolve its exact
    HEAD commit, and yield a `Checkout` describing it. The directory and
    everything in it is removed when the `with` block exits, regardless of
    how it exits."""
    tmp_dir = tempfile.mkdtemp(prefix="devlens-ingest-")
    tmp_path = Path(tmp_dir)
    try:
        clone_repository(clone_url, tmp_path, timeout=timeout)
        commit_sha = resolve_head_commit(tmp_path, timeout=timeout)
        branch = resolve_current_branch(tmp_path, timeout=timeout)
        metadata = read_commit_metadata(tmp_path, timeout=timeout)
        yield Checkout(
            path=tmp_path, commit_sha=commit_sha, branch=branch, commit_metadata=metadata
        )
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
