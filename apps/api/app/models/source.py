import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

FILE_PARSING_STATUSES = ("pending", "parsed", "skipped", "failed")


class SourceBlob(Base):
    """Source truth: content-addressed file content, shared across revisions
    and repositories. Never revision-scoped — see domain-model.md section 3.
    """

    __tablename__ = "source_blobs"
    __table_args__ = (
        CheckConstraint(
            "(inline_content IS NOT NULL AND storage_key IS NULL) "
            "OR (inline_content IS NULL AND storage_key IS NOT NULL)",
            name="exactly_one_storage_mode",
        ),
    )

    # Content IS identity; no surrogate key.
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Exactly one of these is populated (enforced above). The size threshold
    # for choosing inline vs. external storage is an application/config
    # decision, deliberately not made here — see domain-model.md section 16.
    inline_content: Mapped[str | None] = mapped_column(Text)
    storage_key: Mapped[str | None] = mapped_column(Text)

    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceFile(Base):
    """Source truth: a revision-scoped snapshot of a file's identity.

    Immutable once written — see domain-model.md sections 3 and 13.
    """

    __tablename__ = "source_files"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repository_id", "revision_id"],
            ["repository_revisions.repository_id", "repository_revisions.id"],
            ondelete="CASCADE",
            name="fk_source_files_repository_revision",
        ),
        UniqueConstraint("revision_id", "path"),
        # Supports composite FKs from Symbol/CodeChunk enforcing file/revision consistency.
        UniqueConstraint("revision_id", "id", name="uq_source_files_revision_id_id"),
        Index("ix_source_files_repository_id_content_hash", "repository_id", "content_hash"),
        CheckConstraint(
            f"parsing_status IN {FILE_PARSING_STATUSES}", name="parsing_status_allowed"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(
        String(64), ForeignKey("source_blobs.content_hash", ondelete="RESTRICT"), nullable=False
    )
    language: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    line_count: Mapped[int | None] = mapped_column(Integer)
    is_binary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # Derived cache, not source truth — see domain-model.md section 3.
    parsing_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
