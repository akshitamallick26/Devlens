import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base
from app.models._validation import validate_same_owner
from app.models.analysis import AnalysisRun
from app.models.source import SourceFile
from app.models.symbol import Symbol

ARTIFACT_KINDS = ("documentation", "architecture_summary", "ai_report")
ARTIFACT_FORMATS = ("markdown", "html", "json")
ARTIFACT_STATUSES = ("draft", "published", "stale")

# Documentation Intelligence v1: the four artifact sub-types this phase
# generates, all under kind="documentation" (kind stays the broad
# discriminator; `architecture_summary`/`ai_report` remain unused by any
# code, untouched by this phase). Only meaningful (and required, see the
# CHECK constraint below) when kind="documentation".
DOCUMENTATION_ARTIFACT_TYPES = (
    "repository_overview",
    "architecture_overview",
    "module_documentation",
    "symbol_documentation",
)
# How THIS artifact's current content was actually produced — never
# "llm_generated" for content the provider didn't actually write (see
# docs/architecture/documentation-intelligence.md section 7).
GENERATION_METHODS = ("llm_generated", "deterministic_fallback")


class GeneratedArtifact(Base):
    """Derived analysis: a generic record for generated documentation,
    architecture summaries, and AI reports.

    One generic table with a `kind` discriminator, not a typed table per
    artifact type — these have no differing structured fields or query
    patterns between them. Generated tests are the one exception, already
    modeled separately in testing.py because execution/coverage genuinely
    are structurally different. See domain-model.md section 11.
    """

    __tablename__ = "generated_artifacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repository_id", "revision_id"],
            ["repository_revisions.repository_id", "repository_revisions.id"],
            ondelete="CASCADE",
            name="fk_generated_artifacts_repository_revision",
        ),
        CheckConstraint(f"kind IN {ARTIFACT_KINDS}", name="kind_allowed"),
        CheckConstraint(f"format IN {ARTIFACT_FORMATS}", name="format_allowed"),
        CheckConstraint(f"status IN {ARTIFACT_STATUSES}", name="status_allowed"),
        # Documentation Intelligence v1 (Task 2/9): artifact_type and
        # generation_method are required exactly when kind="documentation",
        # and NULL otherwise — the pre-existing, still-unused
        # architecture_summary/ai_report kinds never populate either column.
        CheckConstraint(
            "(kind <> 'documentation' AND artifact_type IS NULL AND generation_method IS NULL) "
            f"OR (kind = 'documentation' AND artifact_type IN {DOCUMENTATION_ARTIFACT_TYPES} "
            f"AND generation_method IN {GENERATION_METHODS})",
            name="documentation_fields_consistent",
        ),
        # Idempotency (Task 12): repeated generation for the same logical
        # identity upserts in place rather than duplicating — the same
        # upsert-by-partial-unique-index pattern app/models/analysis.py's
        # own automation indexes already established, split a third way
        # here since there are three distinct target shapes (none/file/
        # symbol) instead of automation's two (parent/no-parent).
        Index(
            "uq_generated_artifacts_doc_no_target",
            "repository_id",
            "revision_id",
            "artifact_type",
            unique=True,
            postgresql_where=text(
                "artifact_type IN ('repository_overview', 'architecture_overview')"
            ),
        ),
        Index(
            "uq_generated_artifacts_doc_file_target",
            "repository_id",
            "revision_id",
            "artifact_type",
            "related_file_id",
            unique=True,
            postgresql_where=text("artifact_type = 'module_documentation'"),
        ),
        Index(
            "uq_generated_artifacts_doc_symbol_target",
            "repository_id",
            "revision_id",
            "artifact_type",
            "related_symbol_id",
            unique=True,
            postgresql_where=text("artifact_type = 'symbol_documentation'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(
        Text, ForeignKey("source_blobs.content_hash", ondelete="RESTRICT"), nullable=False
    )

    # Soft cross-references, deliberately not DB-composited against revision
    # (SET NULL composite would null the NOT NULL revision_id too — see
    # "Revision-scoped reference enforcement" in database-design.md).
    # `_validate_related_file_id`/`_validate_related_symbol_id` below are the
    # application-level backstop.
    related_file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_files.id", ondelete="SET NULL")
    )
    related_symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("symbols.id", ondelete="SET NULL")
    )

    # `stale` matters once the revision moves forward and the artifact's
    # claims may no longer hold — see domain-model.md section 12.
    # Documentation Intelligence v1 does not use "stale": every artifact is
    # revision-scoped by construction (the unique indexes above key on
    # revision_id too), so an R1 artifact never needs to describe anything
    # but R1. "draft" is also unused — there is no human-review workflow in
    # v1; a successfully generated artifact goes straight to "published".
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")

    # Documentation Intelligence v1 (Tasks 2/9/18). NULL unless
    # kind="documentation" (see the CHECK constraint above).
    artifact_type: Mapped[str | None] = mapped_column(Text)
    generation_method: Mapped[str | None] = mapped_column(Text)
    # Empty string (not NULL) for deterministic_fallback — same "no model
    # used" convention app/models/testing.py::GeneratedTest's own
    # provider/model_name/model_version columns already use, including the
    # server_default needed because this column is added to an
    # already-existing table by an ALTER, not a design choice about what
    # "no model" means.
    provider: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    model_name: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    model_version: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    # Every evidence id supplied to the generator (deterministic,
    # reproducible from repository_id/revision_id/artifact_type/target at
    # read time — not a copy of evidence content, same convention
    # GeneratedTest.cited_evidence_ids already established) vs. which of
    # those the generator actually cited.
    evidence_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    cited_evidence_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    # Task 6: never silently exceed a context budget — this is always set
    # when any budget was hit during context assembly, surfaced through to
    # the API response.
    context_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @validates("analysis_run_id")
    def _validate_analysis_run_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, AnalysisRun, value, self.revision_id, "revision_id")
        return value

    @validates("related_file_id")
    def _validate_related_file_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, SourceFile, value, self.revision_id, "revision_id")
        return value

    @validates("related_symbol_id")
    def _validate_related_symbol_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, Symbol, value, self.revision_id, "revision_id")
        return value
