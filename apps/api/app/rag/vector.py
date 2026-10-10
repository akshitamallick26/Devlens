"""DB-touching vector retrieval over CodeChunk embeddings — revision-
scoped, bounded top-k. See docs/architecture/rag-codebase-qa.md section 6
for the pgvector + revision-filter interaction this module's over-fetch
strategy exists to mitigate.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CodeChunk, Embedding, SourceFile, Symbol
from app.rag.models import RetrievedChunk

DEFAULT_VECTOR_TOP_K = 20

# How many candidates to pull from the (necessarily global, not revision-
# scoped — see section 6) HNSW index before filtering by revision and
# truncating to top_k. Fixed, documented, not unbounded.
VECTOR_OVERFETCH_MULTIPLIER = 5


def search_vector(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    query_vector: list[float],
    provider: str,
    model_name: str,
    model_version: str,
    top_k: int = DEFAULT_VECTOR_TOP_K,
) -> list[RetrievedChunk]:
    """Missing embeddings are simply absent from the index — never
    fabricated, never a zero-vector default. Cosine distance (`<=>`),
    matching migration 0003's `vector_cosine_ops` index exactly — the two
    must agree or the index goes unused.

    One round-trip, structured as a subquery so the overfetch mitigation
    (section 6) actually works: the inner query lets the HNSW index return
    its global nearest neighbors first (no revision filter inside it,
    which is exactly what lets the index be used efficiently), and only
    the *outer* query — over that already-fetched, bounded candidate set —
    filters by revision and truncates to `top_k`. A single combined query
    with the revision filter applied directly alongside the ORDER BY/LIMIT
    risks the planner satisfying the LIMIT from the index before the
    filter narrows it, under-returning for a revision whose chunks aren't
    among the global top matches; this two-step shape avoids that.

    Both ORDER BYs break ties on `chunk_id` — cosine distance alone is not
    unique (identical or near-identical vectors are routine, e.g. two
    chunks with the same text), and an untied `ORDER BY ... LIMIT` has
    unspecified row order across ties in Postgres. Without the inner
    tie-break, which rows survive the overfetch cutoff when many share the
    boundary distance would itself be nondeterministic, not just their
    final position."""
    overfetch_limit = top_k * VECTOR_OVERFETCH_MULTIPLIER
    distance = Embedding.vector.cosine_distance(query_vector).label("distance")

    candidates = (
        select(Embedding.chunk_id, distance)
        .where(Embedding.provider == provider)
        .where(Embedding.model_name == model_name)
        .where(Embedding.model_version == model_version)
        .order_by(distance.asc(), Embedding.chunk_id.asc())
        .limit(overfetch_limit)
        .subquery()
    )

    rows = db.execute(
        select(
            CodeChunk.id,
            CodeChunk.file_id,
            SourceFile.path,
            CodeChunk.symbol_id,
            Symbol.qualified_name,
            CodeChunk.start_line,
            CodeChunk.end_line,
            candidates.c.distance,
        )
        .select_from(candidates)
        .join(CodeChunk, candidates.c.chunk_id == CodeChunk.id)
        .join(SourceFile, CodeChunk.file_id == SourceFile.id)
        .outerjoin(Symbol, CodeChunk.symbol_id == Symbol.id)
        .where(CodeChunk.repository_id == repository_id)
        .where(CodeChunk.revision_id == revision_id)
        .order_by(candidates.c.distance.asc(), CodeChunk.id.asc())
        .limit(top_k)
    ).all()

    return [
        RetrievedChunk(
            chunk_id=row[0],
            file_id=row[1],
            file_path=row[2],
            symbol_id=row[3],
            qualified_name=row[4],
            start_line=row[5],
            end_line=row[6],
            rank=index,
        )
        for index, row in enumerate(rows, start=1)
    ]
