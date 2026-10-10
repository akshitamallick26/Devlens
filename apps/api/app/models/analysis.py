import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, object_session, validates

from app.db.base import Base
from app.models.repository import RepositoryRevision

ANALYSIS_TYPES = (
    "ingestion",
    "parsing",
    "graph_build",
    "impact_analysis",
    "rag_indexing",
    "security_scan",
    "test_generation",
    "documentation_generation",
    # Automation Intelligence v1: one run coordinating revision-delta
    # computation, impact reuse, and invalidation decisions across the
    # other analyzers. See app/automation/service.py.
    "automation_analysis",
)
# "partial" added for Automation Intelligence v1 (Task 11): a run that
# completed some but not all of its required deterministic stages must
# never be reported as "completed" — see app/automation/service.py.
ANALYSIS_STATUSES = ("pending", "running", "completed", "partial", "failed", "cancelled")
TRIGGERED_BY_VALUES = ("manual", "automation", "scheduled")


class AnalysisRun(Base):
    """The connective tissue between source truth and derived analysis: a
    record of one analyzer's execution against one revision.

    One generic table for every analyzer type — see domain-model.md section 6.
    `analyzer_name`/`analyzer_version` are real, indexed columns (not buried
    in `config`) because "which runs used analyzer X version Y" is a query
    this system genuinely needs; `config` stays reserved for analyzer-specific
    parameters that nothing needs to filter or join on. See item 10 of the
    design-audit discussion in database-design.md.
    """

    __tablename__ = "analysis_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repository_id", "revision_id"],
            ["repository_revisions.repository_id", "repository_revisions.id"],
            ondelete="CASCADE",
            name="fk_analysis_runs_repository_revision",
        ),
        Index(
            "ix_analysis_runs_repository_revision_type_status",
            "repository_id",
            "revision_id",
            "analysis_type",
            "status",
        ),
        Index("ix_analysis_runs_status", "status"),
        Index(
            "ix_analysis_runs_type_analyzer_name_version",
            "analysis_type",
            "analyzer_name",
            "analyzer_version",
        ),
        CheckConstraint(f"analysis_type IN {ANALYSIS_TYPES}", name="analysis_type_allowed"),
        CheckConstraint(f"status IN {ANALYSIS_STATUSES}", name="status_allowed"),
        CheckConstraint(f"triggered_by IN {TRIGGERED_BY_VALUES}", name="triggered_by_allowed"),
        # Automation Intelligence v1's own idempotency contract (distinct
        # from every other analyzer type, which stays append-only history):
        # repeated automation requests for the same logical
        # (repository, revision, parent_revision, analysis_type) must
        # resolve to exactly one row, upserted in place — never an
        # uncontrolled duplicate. Split into two partial unique indexes
        # (rather than one constraint over the nullable `parent_revision_id`)
        # for the identical reason `graph_edges`' own dedup indexes are
        # split in app/models/graph.py: a plain UNIQUE constraint treats
        # every NULL as distinct from every other NULL, which would let
        # duplicate root-revision (no-parent) automation runs through
        # unnoticed.
        Index(
            "uq_analysis_runs_automation_with_parent",
            "repository_id",
            "revision_id",
            "parent_revision_id",
            "analysis_type",
            unique=True,
            postgresql_where=text(
                "analysis_type = 'automation_analysis' AND parent_revision_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_analysis_runs_automation_without_parent",
            "repository_id",
            "revision_id",
            "analysis_type",
            unique=True,
            postgresql_where=text(
                "analysis_type = 'automation_analysis' AND parent_revision_id IS NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    # Automation Intelligence v1 only (NULL for every other analyzer type).
    # Deliberately a simple, not composite, FK (see migration 0007);
    # `_validate_parent_revision_id` below is the application-level
    # same-repository backstop. The parent is part of an automation run's
    # identity (the partial unique indexes key on it), so deleting the
    # parent revision deletes the run (migration 0011) rather than nulling
    # it into a different, possibly already-taken, no-parent identity.
    parent_revision_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("repository_revisions.id", ondelete="CASCADE"),
        nullable=True,
    )

    analysis_type: Mapped[str] = mapped_column(Text, nullable=False)

    # Analyzer identity/version convention: always real columns, never config keys.
    analyzer_name: Mapped[str] = mapped_column(Text, nullable=False)
    analyzer_version: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    error_message: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Analyzer-specific parameters only — not identity, not anything queried.
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    triggered_by: Mapped[str] = mapped_column(Text, nullable=False, server_default="manual")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @validates("parent_revision_id")
    def _validate_parent_revision_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        if value is None:
            return value
        session = object_session(self)
        if session is not None:
            revision = session.get(RepositoryRevision, value)
            if revision is not None and revision.repository_id != self.repository_id:
                raise ValueError(f"{key} references a RepositoryRevision of a different repository")
        return value
