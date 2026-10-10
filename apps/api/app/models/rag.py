import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, object_session, validates

from app.db.base import Base
from app.models._validation import validate_same_owner
from app.models.analysis import AnalysisRun
from app.models.symbol import Symbol

CHUNK_KINDS = ("symbol_body", "doc_comment", "sliding_window", "file_summary")

# PROVISIONAL SCHEMA CONSTANT — not a product decision. No embedding model
# has been selected for DevLens yet (Phase 7/RAG). This exists only to make
# the initial schema executable (pgvector requires a column with vector(N)
# declared up front; it cannot be dynamically sized per row, and an ANN
# index — ivfflat/hnsw — requires a fixed N to build at all). 1536 is a
# common current embedding size, chosen with no other significance.
#
# Before Phase 7 writes real embeddings, this MUST be revisited against
# whatever model is actually selected. If that model's dimension differs,
# this column cannot be reused as-is — see the pgvector-limitation note in
# docs/architecture/database-design.md for the real options (migrate the
# column, which invalidates any existing index; add a second table/column
# for the new dimension class; or project vectors down to this width before
# storage). None of those is implemented; this is a placeholder, not a
# design for multi-dimension support.
EMBEDDING_DIMENSION = 1536


class CodeChunk(Base):
    """Derived analysis: a chunk of a file's text, for RAG retrieval.

    A chunk's authoritative text is (file_id, start_line, end_line) sliced
    from that file's content blob — not an independently stored copy.
    `cached_content_hash` is a denormalized read-optimization only, and
    could be dropped and regenerated at any time. See domain-model.md section 9.
    """

    __tablename__ = "code_chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repository_id", "revision_id"],
            ["repository_revisions.repository_id", "repository_revisions.id"],
            ondelete="CASCADE",
            name="fk_code_chunks_repository_revision",
        ),
        ForeignKeyConstraint(
            ["revision_id", "file_id"],
            ["source_files.revision_id", "source_files.id"],
            ondelete="CASCADE",
            name="fk_code_chunks_revision_file",
        ),
        Index("ix_code_chunks_revision_id_file_id", "revision_id", "file_id"),
        CheckConstraint(f"chunk_kind IN {CHUNK_KINDS}", name="chunk_kind_allowed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    # Simple (not composite) FKs, SET NULL — see "Revision-scoped reference
    # enforcement" in database-design.md on avoiding SET NULL composites
    # against NOT NULL columns. `_validate_symbol_id`/`_validate_analysis_run_id`
    # below are the application-level backstop.
    symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("symbols.id", ondelete="SET NULL")
    )
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )

    chunk_kind: Mapped[str] = mapped_column(Text, nullable=False)
    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)

    cached_content_hash: Mapped[str | None] = mapped_column(
        Text, ForeignKey("source_blobs.content_hash", ondelete="RESTRICT")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @validates("symbol_id")
    def _validate_symbol_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, Symbol, value, self.revision_id, "revision_id")
        return value

    @validates("analysis_run_id")
    def _validate_analysis_run_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, AnalysisRun, value, self.revision_id, "revision_id")
        return value


class Embedding(Base):
    """Derived analysis: one model's vector representation of one CodeChunk.

    A separate table (not a column on CodeChunk) specifically for model
    versioning — switching embedding models must not destroy history or
    require migrating a column in place. The unique constraint lets multiple
    models' embeddings coexist (e.g. during an evaluation period) while any
    one RAG query stays model-consistent by filtering on provider/model/version.
    See domain-model.md section 9.

    `provider`/`model_name`/`model_version`/`dimension` are explicit, separate
    columns — not a single opaque `model` string — so a row's embedding
    identity is queryable and auditable without parsing anything. **This
    metadata identifies which model produced a vector; it does not make the
    `vector` column's width dynamic.** `vector` is `Vector(EMBEDDING_DIMENSION)`
    — a fixed-width pgvector column — regardless of what `dimension` says for
    a given row. If a future model's actual output width differs from
    `EMBEDDING_DIMENSION`, pgvector itself will reject the insert; `dimension`
    does not and cannot override that. See `EMBEDDING_DIMENSION`'s docstring
    above and the pgvector-limitation note in database-design.md.
    """

    __tablename__ = "embeddings"
    __table_args__ = (
        UniqueConstraint("chunk_id", "provider", "model_name", "model_version"),
        # Created by migration 0003; declared here so the ORM metadata matches
        # the migrated schema (otherwise autogenerate proposes dropping it).
        Index(
            "ix_embeddings_vector_hnsw",
            "vector",
            postgresql_using="hnsw",
            postgresql_ops={"vector": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    chunk_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("code_chunks.id", ondelete="CASCADE"), nullable=False
    )

    provider: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(Text, nullable=False)
    # NOT NULL with an empty-string convention for "provider exposes no
    # explicit version" — deliberately not nullable, because a standard
    # UNIQUE constraint treats NULL as distinct from NULL, which would let
    # duplicate (chunk, provider, model_name) rows through undetected.
    model_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)

    vector: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION), nullable=False)

    # Two-hop check (Embedding has no revision_id of its own — only its
    # parent CodeChunk does): `_validate_analysis_run_id` below looks up both
    # the chunk and the run to compare their revisions.
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @validates("analysis_run_id")
    def _validate_analysis_run_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        if value is None or self.chunk_id is None:
            return value
        session = object_session(self)
        if session is None:
            return value
        chunk = session.get(CodeChunk, self.chunk_id)
        run = session.get(AnalysisRun, value)
        if chunk is not None and run is not None and chunk.revision_id != run.revision_id:
            raise ValueError(f"{key} references an AnalysisRun from a different revision")
        return value
