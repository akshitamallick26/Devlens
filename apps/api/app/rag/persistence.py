"""DB-touching chunk generation and text derivation — the only module in
app/rag/ that persists CodeChunk rows. Mirrors the AnalysisRun lifecycle
established by app/graph/service.py::run_graph_construction and
app/impact/service.py. See docs/architecture/rag-codebase-qa.md sections
0 and 3.
"""

import logging
import platform
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import AnalysisRun, CodeChunk, RepositoryRevision, SourceBlob, SourceFile, Symbol
from app.rag.chunking import chunk_file
from app.rag.models import SymbolSpan

logger = logging.getLogger("devlens.rag")

ANALYZER_NAME = "devlens-rag-chunker"


class ChunkGenerationError(Exception):
    pass


class RevisionNotFoundError(ChunkGenerationError):
    pass


@dataclass(frozen=True)
class ChunkGenerationResult:
    revision_id: str
    analysis_run_id: str
    status: str
    chunk_count: int


def _log_event(event: str, **fields: object) -> None:
    logger.info(event, extra={"event": event, **fields})


def _analyzer_version() -> str:
    return f"rag-chunker@{platform.python_version()}"


def load_file_lines(db: Session, revision_id: uuid.UUID) -> dict[uuid.UUID, list[str]]:
    """One query for every file in a revision — the shared batched
    primitive both `load_chunk_texts` (below) and
    `app/rag/embeddings.py::_embed_chunks` import and reuse directly, so
    that loading text for several chunks is never one query per chunk (an
    N+1 pattern caught and removed during an earlier hardening pass).
    Returns no entry for storage_key-backed files — never fabricated."""
    rows = db.execute(
        select(SourceFile.id, SourceBlob.inline_content)
        .join(SourceBlob, SourceFile.content_hash == SourceBlob.content_hash)
        .where(SourceFile.revision_id == revision_id)
    ).all()
    return {file_id: content.splitlines() for file_id, content in rows if content is not None}


def load_chunk_text(
    db: Session, revision_id: uuid.UUID, file_id: uuid.UUID, start_line: int, end_line: int
) -> str | None:
    """Convenience single-item wrapper — for a bounded set of chunks (more
    than one or two), prefer `load_chunk_texts` to avoid N+1 queries."""
    return load_chunk_texts(db, revision_id, [(file_id, start_line, end_line)])[0]


def load_chunk_texts(
    db: Session, revision_id: uuid.UUID, requests: list[tuple[uuid.UUID, int, int]]
) -> list[str | None]:
    """Batched: one query for every distinct file among `requests`,
    regardless of how many chunk text spans are requested. Returns `None`,
    never fabricated text, for a span whose file's content lives externally
    (`storage_key`, no retrieval path — the exact precedent
    app/parsing/service.py and app/impact/source.py already established for
    this condition)."""
    if not requests:
        return []
    lines_by_file = load_file_lines(db, revision_id)
    results: list[str | None] = []
    for file_id, start_line, end_line in requests:
        lines = lines_by_file.get(file_id)
        results.append("\n".join(lines[start_line - 1 : end_line]) if lines is not None else None)
    return results


def run_chunk_generation(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> ChunkGenerationResult:
    revision = db.get(RepositoryRevision, revision_id)
    if revision is None or revision.repository_id != repository_id:
        raise RevisionNotFoundError(f"no revision {revision_id} for repository {repository_id}")

    run = AnalysisRun(
        repository_id=repository_id,
        revision_id=revision_id,
        analysis_type="rag_indexing",
        analyzer_name=ANALYZER_NAME,
        analyzer_version=_analyzer_version(),
        status="running",
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    _log_event(
        "chunk_generation_started", repository_id=str(repository_id), revision_id=str(revision_id)
    )

    try:
        chunk_count = _generate_chunks(db, repository_id, revision_id, run.id)
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        db.commit()
        _log_event(
            "chunk_generation_completed", revision_id=str(revision_id), chunk_count=chunk_count
        )
        return ChunkGenerationResult(
            revision_id=str(revision_id),
            analysis_run_id=str(run.id),
            status="completed",
            chunk_count=chunk_count,
        )
    except Exception as exc:
        db.rollback()
        run.status = "failed"
        run.error_message = str(exc)[:2000]
        run.finished_at = datetime.now(UTC)
        db.commit()
        _log_event("chunk_generation_failed", revision_id=str(revision_id), reason=str(exc))
        raise ChunkGenerationError(str(exc)) from exc


def _generate_chunks(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, analysis_run_id: uuid.UUID
) -> int:
    # Idempotency (same strategy as graph construction's GraphEdge rebuild):
    # delete-and-rebuild, scoped to this revision.
    db.execute(
        delete(CodeChunk)
        .where(CodeChunk.repository_id == repository_id)
        .where(CodeChunk.revision_id == revision_id)
    )

    files = (
        db.execute(
            select(SourceFile)
            .where(SourceFile.repository_id == repository_id)
            .where(SourceFile.revision_id == revision_id)
            .where(SourceFile.language == "python")
            .order_by(SourceFile.path)
        )
        .scalars()
        .all()
    )
    symbols = (
        db.execute(
            select(Symbol)
            .where(Symbol.repository_id == repository_id)
            .where(Symbol.revision_id == revision_id)
        )
        .scalars()
        .all()
    )
    symbols_by_file: dict[uuid.UUID, list[Symbol]] = {}
    for symbol in symbols:
        symbols_by_file.setdefault(symbol.file_id, []).append(symbol)

    total = 0
    for source_file in files:
        blob = db.get(SourceBlob, source_file.content_hash)
        if blob is None or blob.inline_content is None:
            continue  # storage_key-backed: no chunking input available, skipped, not fabricated

        file_symbols = [
            SymbolSpan(
                symbol_id=s.id,
                qualified_name=s.qualified_name,
                kind=s.kind,
                start_line=s.start_line,
                end_line=s.end_line,
            )
            for s in symbols_by_file.get(source_file.id, [])
        ]
        candidates = chunk_file(source_file.id, source_file.path, blob.inline_content, file_symbols)
        for candidate in candidates:
            db.add(
                CodeChunk(
                    repository_id=repository_id,
                    revision_id=revision_id,
                    file_id=candidate.file_id,
                    symbol_id=candidate.symbol_id,
                    analysis_run_id=analysis_run_id,
                    chunk_kind=candidate.chunk_kind,
                    start_line=candidate.start_line,
                    end_line=candidate.end_line,
                    cached_content_hash=source_file.content_hash,
                )
            )
            total += 1

    return total
