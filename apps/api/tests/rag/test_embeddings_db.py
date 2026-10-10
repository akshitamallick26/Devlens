"""DB-dependent embedding-generation tests focused on batching behavior.
Requires live PostgreSQL — same `db_session` fixture and explicit-skip
behavior as every other DB-dependent suite. Never substitutes SQLite.
"""

import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.rag.embeddings import EMBEDDING_BATCH_SIZE, run_embedding_generation
from tests.ai.fakes import FakeEmbeddingProvider
from tests.rag.conftest import ingest_parse_graph_and_chunk


@dataclass
class _CallCountingEmbeddingProvider:
    """Wraps FakeEmbeddingProvider and records the size of every `embed()`
    call, so batching can be verified directly rather than inferred."""

    dimension: int = 1536
    call_sizes: list[int] = field(default_factory=list)
    _inner: FakeEmbeddingProvider = field(init=False)

    def __post_init__(self) -> None:
        self._inner = FakeEmbeddingProvider(dimension=self.dimension)

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.call_sizes.append(len(texts))
        return self._inner.embed(texts)


def test_embedding_generation_batches_instead_of_one_call_per_chunk(
    db_session: Session, rag_many_chunks_repo: Path
) -> None:
    ingestion_result, _, chunk_result = ingest_parse_graph_and_chunk(
        db_session, rag_many_chunks_repo
    )
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    assert chunk_result.chunk_count > EMBEDDING_BATCH_SIZE, (
        "fixture must produce more chunks than one batch to prove batching; "
        f"got {chunk_result.chunk_count}"
    )

    provider = _CallCountingEmbeddingProvider()
    result = run_embedding_generation(
        db_session, provider, "fake", "fake-model", "", 1536, repository_id, revision_id
    )

    assert result.status == "completed"
    assert result.embedded_count == chunk_result.chunk_count

    # Not one call per chunk: a bounded number of calls, each <= batch size.
    assert len(provider.call_sizes) > 1
    assert len(provider.call_sizes) < chunk_result.chunk_count
    assert all(size <= EMBEDDING_BATCH_SIZE for size in provider.call_sizes)
    assert sum(provider.call_sizes) == result.embedded_count
    # Every batch but possibly the last is a full batch — proves chunks are
    # grouped up to the limit, not flushed early for no reason.
    assert all(size == EMBEDDING_BATCH_SIZE for size in provider.call_sizes[:-1])
