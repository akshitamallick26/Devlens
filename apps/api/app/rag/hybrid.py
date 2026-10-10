"""Pure Reciprocal Rank Fusion — no DB. See
docs/architecture/rag-codebase-qa.md section 7.
"""

import uuid

from app.rag.models import HybridResult, RetrievedChunk

# The standard constant from the original RRF paper (Cormack, Clarke &
# Buettcher, 2009) — an externally-justified default, not tuned for this
# dataset.
RRF_K = 60


def combine_hybrid(
    lexical: list[RetrievedChunk], vector: list[RetrievedChunk]
) -> list[HybridResult]:
    """Combines by rank, never by raw score — lexical `ts_rank_cd` and
    vector cosine distance are on incomparable scales. A chunk found by
    only one retriever still scores (using only that retriever's term); a
    chunk found by both gets the sum, naturally rewarding agreement.
    Final order: RRF score descending, then `chunk_id` ascending as a
    stable, deterministic tie-break — never dict/set iteration order."""
    scores: dict[uuid.UUID, float] = {}
    sources: dict[uuid.UUID, set[str]] = {}
    chunk_info: dict[uuid.UUID, RetrievedChunk] = {}

    for retriever_name, results in (("lexical", lexical), ("vector", vector)):
        for chunk in results:
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (RRF_K + chunk.rank)
            sources.setdefault(chunk.chunk_id, set()).add(retriever_name)
            chunk_info.setdefault(chunk.chunk_id, chunk)

    combined = [
        HybridResult(
            chunk_id=chunk_id,
            file_id=chunk_info[chunk_id].file_id,
            file_path=chunk_info[chunk_id].file_path,
            symbol_id=chunk_info[chunk_id].symbol_id,
            qualified_name=chunk_info[chunk_id].qualified_name,
            start_line=chunk_info[chunk_id].start_line,
            end_line=chunk_info[chunk_id].end_line,
            rrf_score=score,
            sources=tuple(sorted(sources[chunk_id])),
        )
        for chunk_id, score in scores.items()
    ]
    combined.sort(key=lambda r: (-r.rrf_score, r.chunk_id))
    return combined
