import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base
from app.models._validation import validate_same_owner
from app.models.analysis import AnalysisRun
from app.models.source import SourceFile
from app.models.symbol import Symbol

SEVERITIES = ("critical", "high", "medium", "low", "info")
FINDING_STATUSES = ("open", "resolved", "ignored", "false_positive")
RESOLUTION_STATUSES = ("unresolved", "exact_symbol", "exact_file", "ambiguous")


class SecurityFinding(Base):
    """Derived analysis, with a deliberate exception to immutability: the
    deduplicated identity of a vulnerability/finding, persisting across
    revisions via `fingerprint`. `status` is the one piece of mutable
    workflow state kept — no remediation/assignment system.
    See domain-model.md section 7.
    """

    __tablename__ = "security_findings"
    __table_args__ = (
        UniqueConstraint("repository_id", "fingerprint"),
        # Supports composite FK from SecurityFindingObservation.
        UniqueConstraint("repository_id", "id", name="uq_security_findings_repository_id_id"),
        Index(
            "ix_security_findings_repository_id_severity_status",
            "repository_id",
            "severity",
            "status",
        ),
        CheckConstraint(f"severity IN {SEVERITIES}", name="severity_allowed"),
        CheckConstraint(f"status IN {FINDING_STATUSES}", name="status_allowed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False
    )

    analyzer: Mapped[str] = mapped_column(Text, nullable=False)
    rule_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Composition is analyzer-specific and deliberately not standardized here
    # — see domain-model.md section 16 on Semgrep fingerprint fragility, and
    # app/security/identity.py for the exact, documented functions.
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)

    # Nullable (Security Intelligence v1, migration 0005): a scanner/rule
    # that supplies no severity must be able to report that honestly as
    # "unknown," never coerced into a fabricated default value.
    severity: Mapped[str | None] = mapped_column(Text, nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="open")

    # Advisory-level metadata (Security Intelligence v1), never computed or
    # inferred — populated only when the scanner itself actually supplies
    # the value, left NULL otherwise. See app/security/osv.py /
    # app/security/semgrep.py for exactly which scanner supplies which field.
    cvss_score: Mapped[float | None] = mapped_column(Float)
    cvss_vector: Mapped[str | None] = mapped_column(Text)
    scanner_confidence: Mapped[str | None] = mapped_column(Text)
    # Advisory references (URLs, CWE ids, etc.) verbatim from the scanner —
    # a JSON list of strings, not reformatted or deduplicated against any
    # external source.
    references: Mapped[list[str] | None] = mapped_column(JSONB)

    first_seen_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )
    last_seen_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @validates("first_seen_run_id")
    def _validate_first_seen_run_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, AnalysisRun, value, self.repository_id, "repository_id")
        return value

    @validates("last_seen_run_id")
    def _validate_last_seen_run_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, AnalysisRun, value, self.repository_id, "repository_id")
        return value


class SecurityFindingObservation(Base):
    """Derived analysis: an immutable record of one scan observing one
    finding's fingerprint. Gives dedup (via SecurityFinding) and complete,
    reproducible history (via this table) instead of forcing a choice
    between the two. See domain-model.md section 7.

    `repository_id` here is a deliberate addition beyond the original domain
    document, needed to make the repository/revision composite-FK invariant
    (design-audit item 5) enforceable on this table — see database-design.md.
    """

    __tablename__ = "security_finding_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repository_id", "finding_id"],
            ["security_findings.repository_id", "security_findings.id"],
            ondelete="CASCADE",
            name="fk_observations_repository_finding",
        ),
        ForeignKeyConstraint(
            ["repository_id", "revision_id"],
            ["repository_revisions.repository_id", "repository_revisions.id"],
            ondelete="CASCADE",
            name="fk_observations_repository_revision",
        ),
        Index("ix_security_finding_observations_finding_id", "finding_id"),
        Index("ix_security_finding_observations_revision_id", "revision_id"),
        CheckConstraint(
            f"resolution_status IN {RESOLUTION_STATUSES}", name="resolution_status_allowed"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    finding_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_files.id", ondelete="SET NULL")
    )
    symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("symbols.id", ondelete="SET NULL")
    )

    start_line: Mapped[int | None] = mapped_column(Integer)
    end_line: Mapped[int | None] = mapped_column(Integer)
    raw_finding: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # Occurrence-specific dependency metadata (Security Intelligence v1) —
    # these vary per observation (the same advisory can be observed against
    # different manifests/lockfiles with different pinned versions), unlike
    # the advisory-level fields on SecurityFinding. NULL for non-dependency
    # (e.g. Semgrep) observations.
    affected_package: Mapped[str | None] = mapped_column(Text)
    installed_version: Mapped[str | None] = mapped_column(Text)
    affected_range: Mapped[str | None] = mapped_column(Text)
    fixed_version: Mapped[str | None] = mapped_column(Text)

    # Task 8: explicit record of whether/how this observation was mapped
    # onto the code graph — never silently absent, never a guessed symbol.
    resolution_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="unresolved"
    )

    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    @validates("analysis_run_id")
    def _validate_analysis_run_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, AnalysisRun, value, self.revision_id, "revision_id")
        return value

    @validates("file_id")
    def _validate_file_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, SourceFile, value, self.revision_id, "revision_id")
        return value

    @validates("symbol_id")
    def _validate_symbol_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, Symbol, value, self.revision_id, "revision_id")
        return value
