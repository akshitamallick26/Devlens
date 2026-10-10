import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, object_session, validates

from app.db.base import Base
from app.models.repository import RepositoryRevision

SESSION_STATUSES = ("active", "archived")
MESSAGE_ROLES = ("user", "assistant", "system")
REF_TYPES = (
    "selected_file",
    "selected_symbol",
    "graph_focus",
    "analysis_reference",
    "finding_reference",
)


class InvestigationSession(Base):
    """Investigation state (not source truth, not derived analysis): a
    repository-scoped, ChatGPT-like conversation thread. See
    domain-model.md section 10.

    `current_revision_id`/`started_at_revision_id` are deliberately plain FKs
    (not composite against repository_id) — see "Revision-scoped reference
    enforcement" in database-design.md: SET NULL cannot safely apply to a
    composite that also includes the NOT NULL repository_id. The `@validates`
    methods below are the application-level backstop for repository-matching.
    """

    __tablename__ = "investigation_sessions"
    __table_args__ = (
        Index(
            "ix_investigation_sessions_repository_id_last_active_at",
            "repository_id",
            "last_active_at",
        ),
        CheckConstraint(f"status IN {SESSION_STATUSES}", name="status_allowed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )
    started_at_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("repository_revisions.id", ondelete="SET NULL")
    )
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("repository_revisions.id", ondelete="SET NULL")
    )

    title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def _validate_revision_belongs_to_repository(
        self, key: str, value: uuid.UUID | None
    ) -> uuid.UUID | None:
        if value is None:
            return value
        session = object_session(self)
        if session is not None:
            revision = session.get(RepositoryRevision, value)
            if revision is not None and revision.repository_id != self.repository_id:
                raise ValueError(f"{key} references a RepositoryRevision of a different repository")
        return value

    @validates("started_at_revision_id")
    def _validate_started_at_revision_id(
        self, key: str, value: uuid.UUID | None
    ) -> uuid.UUID | None:
        return self._validate_revision_belongs_to_repository(key, value)

    @validates("current_revision_id")
    def _validate_current_revision_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        return self._validate_revision_belongs_to_repository(key, value)


class SessionMessage(Base):
    """Investigation state: one append-only turn in a session's conversation."""

    __tablename__ = "session_messages"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence_number"),
        CheckConstraint(f"role IN {MESSAGE_ROLES}", name="role_allowed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("investigation_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SessionContextRef(Base):
    """Investigation state: a breadcrumb of what was selected/focused on at
    a point in a session. Polymorphic by `ref_type`, with a CHECK constraint
    ensuring exactly the column matching `ref_type` is populated and no
    other reference column contradicts it. See domain-model.md section 10.

    `file_id`/`symbol_id`/`analysis_run_id`/`finding_id` are intentionally
    **not** validated against any notion of "the session's current revision"
    — this table has no revision_id of its own, and a breadcrumb is supposed
    to preserve what was true *at the time*, which is routinely from an
    older revision than whatever the session points at now. See
    "Revision-scoped reference enforcement" in database-design.md.

    Investigation Sessions v1 (Task 14): four partial unique indexes below
    prevent the same (session, referenced entity) pair from being recorded
    twice for the three reference-typed ref_types plus `finding_reference`
    — one index per populated column, each scoped by `postgresql_where` to
    only the rows of its own `ref_type`, for the identical reason
    `graph_edges`/`analysis_runs` already split their own dedup indexes this
    way (a plain UNIQUE constraint over a nullable column treats every NULL
    as distinct from every other NULL, so one shared constraint across all
    four nullable columns would never actually fire). `graph_focus` is
    deliberately excluded — repeated camera-state snapshots at different
    points in a session are not duplicates, they are distinct breadcrumbs.
    See docs/architecture/investigation-sessions.md section 7.
    """

    __tablename__ = "session_context_refs"
    __table_args__ = (
        CheckConstraint(
            "(ref_type = 'selected_file' AND file_id IS NOT NULL "
            " AND symbol_id IS NULL AND analysis_run_id IS NULL AND finding_id IS NULL) "
            "OR (ref_type = 'selected_symbol' AND symbol_id IS NOT NULL "
            " AND file_id IS NULL AND analysis_run_id IS NULL AND finding_id IS NULL) "
            "OR (ref_type = 'analysis_reference' AND analysis_run_id IS NOT NULL "
            " AND file_id IS NULL AND symbol_id IS NULL AND finding_id IS NULL) "
            "OR (ref_type = 'finding_reference' AND finding_id IS NOT NULL "
            " AND file_id IS NULL AND symbol_id IS NULL AND analysis_run_id IS NULL) "
            "OR (ref_type = 'graph_focus' AND graph_camera_state IS NOT NULL "
            " AND file_id IS NULL AND symbol_id IS NULL AND analysis_run_id IS NULL "
            " AND finding_id IS NULL)",
            name="ref_type_matches_populated_column",
        ),
        Index(
            "uq_session_context_refs_session_id_file_id",
            "session_id",
            "file_id",
            unique=True,
            postgresql_where=text("ref_type = 'selected_file'"),
        ),
        Index(
            "uq_session_context_refs_session_id_symbol_id",
            "session_id",
            "symbol_id",
            unique=True,
            postgresql_where=text("ref_type = 'selected_symbol'"),
        ),
        Index(
            "uq_session_context_refs_session_id_analysis_run_id",
            "session_id",
            "analysis_run_id",
            unique=True,
            postgresql_where=text("ref_type = 'analysis_reference'"),
        ),
        Index(
            "uq_session_context_refs_session_id_finding_id",
            "session_id",
            "finding_id",
            unique=True,
            postgresql_where=text("ref_type = 'finding_reference'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("investigation_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("session_messages.id", ondelete="SET NULL")
    )

    ref_type: Mapped[str] = mapped_column(Text, nullable=False)

    file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_files.id", ondelete="SET NULL")
    )
    symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("symbols.id", ondelete="SET NULL")
    )
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )
    # Added beyond the original domain document, which listed
    # 'finding_reference' as a ref_type but had no column to back it — see
    # database-design.md for this gap-fill.
    finding_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("security_findings.id", ondelete="SET NULL")
    )

    graph_camera_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
