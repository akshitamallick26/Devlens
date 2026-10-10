"""Bounded source-text loading for symbol-delta comparison. Deliberately
NOT a reuse of app/rag/persistence.py::load_file_lines, which loads every
file in an entire revision — here we only ever need the handful of files
that `revision_delta.py` already identified as added/deleted/modified,
so this is scoped to an explicit `file_ids` set (Task 3's "never load
entire repositories into memory unnecessarily").
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SourceBlob, SourceFile


def load_lines_for_files(
    db: Session, revision_id: uuid.UUID, file_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, list[str]]:
    file_id_list = list(file_ids)
    if not file_id_list:
        return {}
    rows = db.execute(
        select(SourceFile.id, SourceBlob.inline_content)
        .join(SourceBlob, SourceFile.content_hash == SourceBlob.content_hash)
        .where(SourceFile.revision_id == revision_id)
        .where(SourceFile.id.in_(file_id_list))
    ).all()
    return {file_id: content.splitlines() for file_id, content in rows if content is not None}
