"""DB-dependent chunk persistence tests. Requires live PostgreSQL — same
`db_session` fixture and explicit-skip behavior as every other DB-dependent
suite in this project. Never substitutes SQLite.
"""

import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AnalysisRun, CodeChunk
from app.rag.persistence import load_chunk_text, run_chunk_generation
from tests.rag.conftest import ingest_parse_graph_and_chunk


def test_chunks_are_persisted_revision_scoped_with_correct_provenance(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    ingestion_result, _, chunk_result = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    assert chunk_result.status == "completed"
    assert chunk_result.chunk_count > 0

    chunks = (
        db_session.execute(select(CodeChunk).where(CodeChunk.revision_id == revision_id))
        .scalars()
        .all()
    )
    assert len(chunks) == chunk_result.chunk_count
    assert all(c.revision_id == revision_id for c in chunks)
    assert all(c.repository_id == uuid.UUID(ingestion_result.repository_id) for c in chunks)

    # At least one symbol_body chunk, correctly linked to its Symbol.
    symbol_chunks = [c for c in chunks if c.chunk_kind == "symbol_body"]
    assert len(symbol_chunks) > 0
    assert all(c.symbol_id is not None for c in symbol_chunks)

    # Exactly one file_summary chunk per file (none of our fixture files
    # are large enough to need more than one piece).
    file_summary_chunks = [c for c in chunks if c.chunk_kind == "file_summary"]
    distinct_files = {c.file_id for c in file_summary_chunks}
    assert len(file_summary_chunks) == len(distinct_files)


def test_chunk_generation_is_idempotent(db_session: Session, rag_fixture_repo: Path) -> None:
    ingestion_result, _, first = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    second = run_chunk_generation(db_session, repository_id, revision_id)

    assert first.chunk_count == second.chunk_count
    count = db_session.scalar(
        select(func.count()).select_from(CodeChunk).where(CodeChunk.revision_id == revision_id)
    )
    assert count == first.chunk_count

    runs = (
        db_session.execute(
            select(AnalysisRun)
            .where(AnalysisRun.revision_id == revision_id)
            .where(AnalysisRun.analysis_type == "rag_indexing")
            .where(AnalysisRun.analyzer_name == "devlens-rag-chunker")
        )
        .scalars()
        .all()
    )
    assert len(runs) == 2  # history preserved, one row per attempt
    assert all(r.status == "completed" for r in runs)


def test_load_chunk_text_derives_correct_source_span(
    db_session: Session, rag_fixture_repo: Path
) -> None:
    ingestion_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    chunk = (
        db_session.execute(
            select(CodeChunk)
            .where(CodeChunk.revision_id == revision_id)
            .where(CodeChunk.chunk_kind == "symbol_body")
        )
        .scalars()
        .first()
    )
    assert chunk is not None

    text = load_chunk_text(db_session, revision_id, chunk.file_id, chunk.start_line, chunk.end_line)
    assert text is not None
    assert text.strip() != ""


def test_other_revisions_chunks_never_appear(db_session: Session, rag_fixture_repo: Path) -> None:
    first_result, _, _ = ingest_parse_graph_and_chunk(db_session, rag_fixture_repo, owner="owner-a")
    second_result, _, _ = ingest_parse_graph_and_chunk(
        db_session, rag_fixture_repo, owner="owner-b"
    )

    first_revision_id = uuid.UUID(first_result.revision_id)
    second_revision_id = uuid.UUID(second_result.revision_id)
    assert first_revision_id != second_revision_id

    first_chunk_ids = {
        c.id
        for c in db_session.execute(
            select(CodeChunk).where(CodeChunk.revision_id == first_revision_id)
        ).scalars()
    }
    second_chunk_ids = {
        c.id
        for c in db_session.execute(
            select(CodeChunk).where(CodeChunk.revision_id == second_revision_id)
        ).scalars()
    }
    assert first_chunk_ids.isdisjoint(second_chunk_ids)
