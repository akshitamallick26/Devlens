import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, object_session, validates

from app.db.base import Base

REPOSITORY_STATUSES = ("pending", "ingesting", "ready", "error", "archived")
REVISION_ANALYSIS_STATUSES = ("pending", "running", "completed", "failed")


class Repository(Base):
    """Source truth: identity of a tracked GitHub repository.

    Holds identity and a pointer to current state only — never revision-shaped
    data. See docs/architecture/domain-model.md section 1.
    """

    __tablename__ = "repositories"
    __table_args__ = (
        UniqueConstraint("owner", "name"),
        CheckConstraint(f"status IN {REPOSITORY_STATUSES}", name="status_allowed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    owner: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str] = mapped_column(String(255), nullable=False)

    # The one legitimately mutable pointer on this table. Deliberately a plain
    # (not composite) FK — see docs/architecture/database-design.md "Repository
    # <-> RepositoryRevision" for why the composite-FK pattern used everywhere
    # else cannot safely apply to this self-referencing pointer.
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("repository_revisions.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )

    # Denormalized cache of derived state — reconstructable from AnalysisRun
    # history, never an independent source of truth. See domain-model.md section 12.
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    last_successful_analysis_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @validates("current_revision_id")
    def _validate_current_revision_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        """The strongest reasonable mechanism available for this one
        deliberately-special pointer (see the comment above the column): a
        DB-level composite FK can't work here because `id` is this row's own
        primary key and can never be the target of a SET NULL action. This
        is a best-effort ORM-level check instead — see
        app/models/_validation.py's module docstring for its limits."""
        if value is None or self.id is None:
            return value
        session = object_session(self)
        if session is not None:
            revision = session.get(RepositoryRevision, value)
            if revision is not None and revision.repository_id != self.id:
                raise ValueError(f"{key} references a RepositoryRevision of a different repository")
        return value


class RepositoryRevision(Base):
    """Source truth: one specific commit DevLens has looked at.

    Immutable once ingestion for it begins — see domain-model.md section 13.
    """

    __tablename__ = "repository_revisions"
    __table_args__ = (
        UniqueConstraint("repository_id", "commit_sha"),
        # Supports composite FKs from child tables enforcing
        # repository_id/revision_id consistency (see database-design.md).
        UniqueConstraint("repository_id", "id", name="uq_repository_revisions_repository_id_id"),
        Index(
            "ix_repository_revisions_repository_id_committed_at", "repository_id", "committed_at"
        ),
        CheckConstraint(
            "latest_analysis_status IS NULL OR latest_analysis_status IN "
            f"{REVISION_ANALYSIS_STATUSES}",
            name="latest_analysis_status_allowed",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_sha: Mapped[str | None] = mapped_column(String(64))
    branch_ref: Mapped[str | None] = mapped_column(String(500))
    author_name: Mapped[str | None] = mapped_column(String(255))
    author_email: Mapped[str | None] = mapped_column(String(320))
    committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Denormalized cache — see domain-model.md section 12.
    latest_analysis_status: Mapped[str | None] = mapped_column(Text)
    file_count: Mapped[int | None] = mapped_column(Integer)
