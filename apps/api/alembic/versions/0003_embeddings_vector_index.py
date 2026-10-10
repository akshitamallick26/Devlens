"""embeddings: HNSW vector index for similarity search

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07

The only schema change RAG/Codebase Q&A v1 needs. `domain-model.md`
section 14 already anticipated this exact index ("vector index
(ivfflat/hnsw) on vector, pre-filtered by revision_id — ... a real tuning
concern to resolve at implementation time, not here") — this is that
resolution. See docs/architecture/rag-codebase-qa.md section 6 for the
full reasoning, summarized here:

- HNSW over ivfflat: no "lists" parameter to tune against an unknown
  future corpus size, and it builds incrementally rather than needing
  existing data to cluster against — appropriate for a schema that may
  start empty.
- Cosine distance (`vector_cosine_ops`): matches `app/rag/vector.py`'s
  query operator (`<=>`) — the two must agree, or the index is unused.
- `embeddings` has no `revision_id` of its own (only its parent
  `CodeChunk` does — see `app/models/rag.py`), so this index is
  necessarily global, not revision-scoped. Revision isolation is enforced
  by the query (joining to `CodeChunk` and filtering there), with an
  over-fetch-then-filter strategy to compensate for the index returning
  its nearest neighbors before that filter is applied — see
  `app/rag/vector.py`'s own docstring for the precise mitigation.

Not run against a live PostgreSQL instance when authored — same caveat as
`0001`/`0002`'s own headers.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_embeddings_vector_hnsw ON embeddings USING hnsw (vector vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_embeddings_vector_hnsw")
