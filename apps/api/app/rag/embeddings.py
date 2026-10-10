"""DB + provider-touching embedding generation. Mirrors
app/rag/persistence.py's AnalysisRun lifecycle. See
docs/architecture/rag-codebase-qa.md sections 0 and 4.

Idempotency differs deliberately from chunk generation: embeddings are
expensive to (re)compute, so this upserts by the existing
`(chunk_id, provider, model_name, model_version)` unique key rather than
delete-and-rebuild — re-running with the same model updates existing rows
in place; a different model/version always gets its own new rows, never
overwriting a different model's vector.
"""

import logging
import platform
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.ai.embedding_provider import DEFAULT_EMBEDDING_MODEL_VERSION, get_embedding_provider
from app.ai.models import EmbeddingProvider
from app.core.config import Settings
from app.models import AnalysisRun, CodeChunk, Embedding, RepositoryRevision
from app.models.rag import EMBEDDING_DIMENSION
from app.rag.persistence import load_file_lines

logger = logging.getLogger("devlens.rag")

ANALYZER_NAME = "devlens-rag-embedder"

# A fixed v1 policy constant — bounded batches, never one chunk per request
# and never one unbounded request for an entire revision (section 4).
EMBEDDING_BATCH_SIZE = 64


class EmbeddingGenerationError(Exception):
    pass


class RevisionNotFoundError(EmbeddingGenerationError):
    pass


@dataclass(frozen=True)
class EmbeddingGenerationResult:
    revision_id: str
    analysis_run_id: str
    status: str
    embedded_count: int
    skipped_count: int  # chunks with no retrievable text (storage_key-backed)


def _log_event(event: str, **fields: object) -> None:
    logger.info(event, extra={"event": event, **fields})


def _analyzer_version(provider_model: str) -> str:
    return f"{provider_model}@python{platform.python_version()}"


def run_embedding_generation_from_settings(
    db: Session, settings: Settings, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> EmbeddingGenerationResult | None:
    """Convenience wrapper wiring `get_embedding_provider()` and the
    settings-derived provider/model/version/dimension consistently — the
    same values `app/rag/qa.py`'s query-time retrieval must also use
    (mismatched values here would mean vector search never finds anything,
    since every retrieval WHERE clause filters on all three exactly).
    Returns `None` — not an error — when no embedding provider is
    configured, the same "not configured is normal" convention as
    `get_provider()`/`get_embedding_provider()` themselves."""
    provider = get_embedding_provider(settings, expected_dimension=EMBEDDING_DIMENSION)
    if provider is None:
        return None
    return run_embedding_generation(
        db,
        provider,
        settings.embedding_provider,
        settings.embedding_model,
        DEFAULT_EMBEDDING_MODEL_VERSION,
        EMBEDDING_DIMENSION,
        repository_id,
        revision_id,
    )


def run_embedding_generation(
    db: Session,
    provider: EmbeddingProvider,
    provider_name: str,
    model_name: str,
    model_version: str,
    dimension: int,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> EmbeddingGenerationResult:
    """`provider` must not be `None` — callers check `get_embedding_provider()`
    themselves first (same pattern as app/impact/explanation.py checking
    `get_provider()`), since "no provider configured" and "provider call
    failed" are different situations worth distinguishing at the caller."""
    revision = db.get(RepositoryRevision, revision_id)
    if revision is None or revision.repository_id != repository_id:
        raise RevisionNotFoundError(f"no revision {revision_id} for repository {repository_id}")

    run = AnalysisRun(
        repository_id=repository_id,
        revision_id=revision_id,
        analysis_type="rag_indexing",
        analyzer_name=ANALYZER_NAME,
        analyzer_version=_analyzer_version(model_name),
        status="running",
        started_at=datetime.now(UTC),
        config={
            "provider": provider_name,
            "model_name": model_name,
            "model_version": model_version,
        },
    )
    db.add(run)
    db.commit()
    _log_event(
        "embedding_generation_started",
        repository_id=str(repository_id),
        revision_id=str(revision_id),
    )

    try:
        embedded, skipped = _embed_chunks(
            db,
            provider,
            provider_name,
            model_name,
            model_version,
            dimension,
            repository_id,
            revision_id,
        )
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        db.commit()
        _log_event(
            "embedding_generation_completed",
            revision_id=str(revision_id),
            embedded_count=embedded,
            skipped_count=skipped,
        )
        return EmbeddingGenerationResult(
            revision_id=str(revision_id),
            analysis_run_id=str(run.id),
            status="completed",
            embedded_count=embedded,
            skipped_count=skipped,
        )
    except Exception as exc:
        # Any failure (provider or otherwise) must close the run as failed;
        # an exception escaping here would leave it "running" forever.
        db.rollback()
        run.status = "failed"
        run.error_message = str(exc)[:2000]
        run.finished_at = datetime.now(UTC)
        db.commit()
        _log_event("embedding_generation_failed", revision_id=str(revision_id), reason=str(exc))
        raise EmbeddingGenerationError(str(exc)) from exc


def _embed_chunks(
    db: Session,
    provider: EmbeddingProvider,
    provider_name: str,
    model_name: str,
    model_version: str,
    dimension: int,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
) -> tuple[int, int]:
    chunk_ids = db.execute(
        select(CodeChunk.id, CodeChunk.file_id, CodeChunk.start_line, CodeChunk.end_line)
        .where(CodeChunk.repository_id == repository_id)
        .where(CodeChunk.revision_id == revision_id)
        .order_by(CodeChunk.file_id, CodeChunk.start_line)
    ).all()
    lines_by_file = load_file_lines(db, revision_id)

    embedded = 0
    skipped = 0
    batch: list[tuple[uuid.UUID, str]] = []

    def _flush(pending: list[tuple[uuid.UUID, str]]) -> None:
        if not pending:
            return
        vectors = provider.embed([text for _, text in pending])
        for (chunk_id, _text), vector in zip(pending, vectors, strict=True):
            stmt = (
                pg_insert(Embedding)
                .values(
                    chunk_id=chunk_id,
                    provider=provider_name,
                    model_name=model_name,
                    model_version=model_version,
                    dimension=dimension,
                    vector=vector,
                )
                .on_conflict_do_update(
                    index_elements=["chunk_id", "provider", "model_name", "model_version"],
                    set_={"vector": vector, "dimension": dimension},
                )
            )
            db.execute(stmt)

    for chunk_id, file_id, start_line, end_line in chunk_ids:
        lines = lines_by_file.get(file_id)
        text = "\n".join(lines[start_line - 1 : end_line]) if lines is not None else None
        if text is None or not text.strip():
            skipped += 1
            continue
        batch.append((chunk_id, text))
        embedded += 1
        if len(batch) >= EMBEDDING_BATCH_SIZE:
            _flush(batch)
            batch = []
    _flush(batch)

    return embedded, skipped
