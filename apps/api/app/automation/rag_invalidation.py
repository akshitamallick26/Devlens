"""RAG invalidation/rebuild (Task 8). Calls the existing, unmodified
`app.rag.persistence.run_chunk_generation` directly — never a duplicate
chunking implementation. `run_chunk_generation` itself has no partial mode
(it always delete-and-rebuilds every `CodeChunk` row for the given
revision; see its own module docstring), and a brand-new revision starts
with zero `CodeChunk` rows of its own regardless of how much file content
is actually unchanged versus its parent — so there is no safe way to
"skip" this step and still have the current revision's chunks exist at
all. This is the explicit "correctness over optimization" choice Task 7/8
asks for, applied to chunking.

Embeddings are never regenerated here: `CodeChunk.id` is a fresh,
non-content-addressed UUID every time `run_chunk_generation` runs (even
for byte-identical source), so an embedding keyed to a parent revision's
chunk id can never be reused for the current revision's chunk id — there is
no schema-level content identity to reuse by. `embeddings_require_
regeneration` is therefore always `True`; actually calling the (paid,
provider-backed) `run_embedding_generation` is left to an explicit,
separate request, exactly like app/testing's "identify, don't auto-
regenerate" stance.
"""

import uuid

from sqlalchemy.orm import Session

from app.automation.models import EXECUTION_STRATEGIES, RAGInvalidationPlan
from app.rag.persistence import ChunkGenerationError, run_chunk_generation

_EXECUTION_STRATEGY = EXECUTION_STRATEGIES[0]  # "full_revision_rebuild" — the only one in v1


def rebuild_rag_index(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> RAGInvalidationPlan:
    try:
        result = run_chunk_generation(db, repository_id, revision_id)
    except ChunkGenerationError as exc:
        return RAGInvalidationPlan(
            status="failed",
            chunks_invalidated=0,
            embeddings_require_regeneration=True,
            execution_strategy=_EXECUTION_STRATEGY,
            detail=str(exc),
        )
    return RAGInvalidationPlan(
        status="rebuilt",
        chunks_invalidated=result.chunk_count,
        embeddings_require_regeneration=True,
        execution_strategy=_EXECUTION_STRATEGY,
        detail=None,
    )
