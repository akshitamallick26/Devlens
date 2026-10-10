"""Deterministic file-level revision delta (Task 3). DB-touching: reads
only `SourceFile` identity columns (path, content_hash) for the two
revisions being compared — never loads file content here, and never loads
more than the two revisions' own rows (bounded by `repository_id` +
`revision_id`, never a repository-wide scan).

Rename detection: `SourceFile` carries no git rename signal (ingestion
reads `git ls-files` + per-file content, not `git diff --find-renames`),
so there is no deterministic evidence to detect a rename from. A renamed-
without-modification file therefore shows up honestly as one delete +
one add, with `rename_detection="unsupported"` always set — never guessed
from path/content similarity.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.models import FileDelta, FileEntry
from app.models import SourceFile


def _load_file_entries(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> dict[str, FileEntry]:
    rows = db.execute(
        select(SourceFile.id, SourceFile.path, SourceFile.content_hash)
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == revision_id)
    ).all()
    return {
        path: FileEntry(path=path, file_id=file_id, content_hash=content_hash)
        for file_id, path, content_hash in rows
    }


def compute_file_delta(
    db: Session,
    repository_id: uuid.UUID,
    current_revision_id: uuid.UUID,
    parent_revision_id: uuid.UUID | None,
) -> FileDelta:
    current_by_path = _load_file_entries(db, repository_id, current_revision_id)
    parent_by_path = (
        _load_file_entries(db, repository_id, parent_revision_id)
        if parent_revision_id is not None
        else {}
    )

    added_paths = sorted(current_by_path.keys() - parent_by_path.keys())
    deleted_paths = sorted(parent_by_path.keys() - current_by_path.keys())
    common_paths = sorted(current_by_path.keys() & parent_by_path.keys())

    modified: list[tuple[FileEntry, FileEntry]] = []
    unchanged: list[FileEntry] = []
    for path in common_paths:
        current_entry = current_by_path[path]
        parent_entry = parent_by_path[path]
        if current_entry.content_hash == parent_entry.content_hash:
            unchanged.append(current_entry)
        else:
            modified.append((parent_entry, current_entry))

    return FileDelta(
        added=tuple(current_by_path[p] for p in added_paths),
        deleted=tuple(parent_by_path[p] for p in deleted_paths),
        modified=tuple(modified),
        unchanged=tuple(unchanged),
        rename_detection="unsupported",
    )
