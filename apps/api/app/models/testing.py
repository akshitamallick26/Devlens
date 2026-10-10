import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
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

# Supersedes the original domain-model.md section 8 vocabulary
# (generated|accepted|rejected|superseded), which predates Testing /
# Generated Tests v1 and described a human-review workflow that was never
# built. The pipeline this status now tracks is linear (see
# app/testing/service.py::generate_tests_for_target): generated ->
# (validation_failed | ready) -> (execution_failed | passed | failed).
# "passed"/"failed" here reflect this GeneratedTest's *most recent*
# execution outcome (denormalized convenience) — the full, append-only
# execution history lives in TestExecution; this column is never the only
# record of what happened.
GENERATED_TEST_STATUSES = (
    "generated",
    "validation_failed",
    "ready",
    "execution_failed",
    "passed",
    "failed",
)
VALIDATION_STATUSES = ("pending", "passed", "failed")
TEST_EXECUTION_STATUSES = ("passed", "failed", "error", "timeout")


class GeneratedTest(Base):
    """Derived analysis: an AI-generated test targeting a specific symbol.

    Not a CI system — just enough to trace a generated test back to what it
    targets and what produced it. See domain-model.md section 8.
    """

    __tablename__ = "generated_tests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repository_id", "revision_id"],
            ["repository_revisions.repository_id", "repository_revisions.id"],
            ondelete="CASCADE",
            name="fk_generated_tests_repository_revision",
        ),
        ForeignKeyConstraint(
            ["revision_id", "target_symbol_id"],
            ["symbols.revision_id", "symbols.id"],
            ondelete="CASCADE",
            name="fk_generated_tests_revision_target_symbol",
        ),
        CheckConstraint(f"status IN {GENERATED_TEST_STATUSES}", name="status_allowed"),
        CheckConstraint(
            f"validation_status IN {VALIDATION_STATUSES}", name="validation_status_allowed"
        ),
        CheckConstraint(
            "provider <> '' AND model_name <> '' AND model_version <> ''",
            name="provenance_not_empty",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_symbol_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )

    proposed_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(
        Text, ForeignKey("source_blobs.content_hash", ondelete="RESTRICT"), nullable=False
    )
    language: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="generated")

    # Provenance identity — same explicit-columns convention as
    # app/models/rag.py::Embedding (never buried in `config`, so "which
    # runs used provider X model Y" stays queryable). Every row is
    # provider-generated (no deterministic fallback ever persists a
    # GeneratedTest), and app/testing/service.py takes provider/model from
    # the provider object that generated it. NULL means "not known", never
    # an empty string (CHECK provenance_not_empty): model_version is NULL
    # whenever the provider reports none, and all three are NULL only on
    # rows persisted before migration 0010, whose provenance was lost.
    provider: Mapped[str | None] = mapped_column(Text)
    model_name: Mapped[str | None] = mapped_column(Text)
    model_version: Mapped[str | None] = mapped_column(Text)

    # Pre-execution static validation (Task 8) is tracked separately from
    # `status` (Task 7: "do not overload one status to mean multiple
    # things") — this is specifically "did the generated artifact pass
    # structural validation," independent of whether it was ever executed.
    validation_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    validation_errors: Mapped[list[str] | None] = mapped_column(JSONB)

    # The LLM's own stated justification and which bounded evidence ids
    # (T1, T2, ... — app/testing/citations.py) it actually cited, captured
    # post-sanitization so an unknown/invented id can never appear here.
    # Not a full copy of the context package itself — that's deterministic
    # and reproducible from (repository_id, revision_id, target_symbol_id)
    # at read time, same as RAG never persists its evidence packages.
    rationale: Mapped[str | None] = mapped_column(Text)
    cited_evidence_ids: Mapped[list[str] | None] = mapped_column(JSONB)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @validates("analysis_run_id")
    def _validate_analysis_run_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, AnalysisRun, value, self.revision_id, "revision_id")
        return value


class TestExecution(Base):
    """Derived analysis: one execution of either a GeneratedTest or an
    existing repository test. Deliberately minimal — no CI/test-management
    subsystem, just enough identity to know what ran and what happened.
    See domain-model.md section 8.
    """

    # Not a test class — this name collides with pytest's default discovery
    # pattern (Test*) purely by coincidence, since it's a legitimate domain
    # model name. This is pytest's documented way to say "skip me."
    __test__ = False

    __tablename__ = "test_executions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repository_id", "revision_id"],
            ["repository_revisions.repository_id", "repository_revisions.id"],
            ondelete="CASCADE",
            name="fk_test_executions_repository_revision",
        ),
        CheckConstraint(
            "(generated_test_id IS NOT NULL AND file_id IS NULL AND test_identifier IS NULL) "
            "OR (generated_test_id IS NULL AND file_id IS NOT NULL "
            "AND test_identifier IS NOT NULL)",
            name="exactly_one_test_identity",
        ),
        # repository_id/revision_id must be set together (or not at all) for
        # the composite FK above to mean anything — MATCH SIMPLE (Postgres's
        # default) treats a composite FK as satisfied whenever any column in
        # it is NULL, so without this CHECK a row could set repository_id
        # alone and the composite FK would silently not catch it.
        CheckConstraint(
            "(repository_id IS NULL) = (revision_id IS NULL)",
            name="repository_and_revision_set_together",
        ),
        CheckConstraint(f"status IN {TEST_EXECUTION_STATUSES}", name="status_allowed"),
        CheckConstraint(
            f"validation_status IN {VALIDATION_STATUSES}", name="validation_status_allowed"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )

    generated_test_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("generated_tests.id", ondelete="CASCADE")
    )

    # Minimum identity for executing an existing repository test (not
    # generated): which file it lives in, and an analyzer-defined identifier
    # within that file (e.g. a pytest node id). repository_id/revision_id are
    # now composite-FK'd together (above) and DB-enforced as a matching pair
    # — but file_id stays a simple FK (`_validate_file_id` below is the
    # application-level backstop): TestExecution and SourceFile are sibling
    # cascades off repository_revisions, so a composite RESTRICT between them
    # could race against the other cascade during a bulk delete and fire a
    # false-positive block. See "Revision-scoped reference enforcement" in
    # database-design.md.
    repository_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    revision_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_files.id", ondelete="SET NULL")
    )
    test_identifier: Mapped[str | None] = mapped_column(Text)

    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )

    status: Mapped[str] = mapped_column(Text, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    # Superseded by `stdout`/`stderr` below (added migration 0004) — kept
    # nullable and unwritten by new code rather than dropped, since dropping
    # a column is a destructive migration this phase's instructions
    # explicitly prohibit. Never populated going forward.
    output: Mapped[str | None] = mapped_column(Text)
    coverage_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Bounded, truncated before persistence (app/testing/sandbox.py) — never
    # the full unbounded process output. Split rather than combined so a
    # failure's actual error (stderr) isn't interleaved with a test
    # framework's routine progress output (stdout).
    exit_code: Mapped[int | None] = mapped_column(Integer)
    stdout: Mapped[str | None] = mapped_column(Text)
    stderr: Mapped[str | None] = mapped_column(Text)
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Denormalized copy of the GeneratedTest's validation_status at the
    # moment this execution ran — lets one execution row answer "was this
    # gated by a passed validation" without a join back to GeneratedTest
    # (which may not even exist for a future pre-existing-repo-test
    # execution; see `generated_test_id`'s own nullability above).
    validation_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    @validates("file_id")
    def _validate_file_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, SourceFile, value, self.revision_id, "revision_id")
        return value

    @validates("analysis_run_id")
    def _validate_analysis_run_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, AnalysisRun, value, self.revision_id, "revision_id")
        return value
