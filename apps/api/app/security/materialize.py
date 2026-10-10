"""Shared, safe file materialization into a disposable temporary directory
— the one place both app/security/osv.py's manifest scanning and
app/security/semgrep.py's source scanning get their input from. No
arbitrary filesystem path is ever scanned: callers build `MaterializedFile`
entries only from real `SourceFile` rows for one exact repository/revision.

Guaranteed cleanup, same pattern as app/ingestion/checkout.py and
app/testing/sandbox.py.
"""

import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MaterializedFile:
    file_id: uuid.UUID
    path: str
    content: str


@contextmanager
def materialize_files(files: list[MaterializedFile]) -> Iterator[Path]:
    """Writes exactly the given files (and nothing else) into a fresh
    temporary directory, preserving their real relative paths. Path
    traversal / absolute paths are refused defensively, even though
    `SourceFile.path` values are never supposed to contain them."""
    tmp_dir = tempfile.mkdtemp(prefix="devlens-security-scan-")
    tmp_path = Path(tmp_dir)
    try:
        for file in files:
            if file.path.startswith("/") or ".." in Path(file.path).parts:
                continue
            destination = tmp_path / file.path
            if not destination.resolve().is_relative_to(tmp_path.resolve()):
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(file.content, encoding="utf-8")
        yield tmp_path
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
