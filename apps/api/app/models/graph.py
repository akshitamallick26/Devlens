import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base
from app.models._validation import validate_same_owner
from app.models.analysis import AnalysisRun

# "contains" is deliberately excluded: containment of a symbol in a file or a
# method in a class is already modeled via Symbol.file_id / parent_symbol_id.
# See docs/architecture/database-design.md section on GraphEdge.
RELATIONSHIP_TYPES = ("imports", "calls", "inherits", "implements", "references")
# "syntactic" (tree-sitter/pattern-match, not yet implemented) and "semantic"
# (type-checker-backed, not yet implemented — see domain-model.md's original
# gloss) are reserved for future analyzers. "ast_resolution" is what the v1
# Python graph builder actually does: deterministic AST-based scope/import/
# binding resolution, no type inference. See graph-construction.md section 7.
RESOLUTION_METHODS = ("syntactic", "semantic", "ast_resolution")


class GraphEdge(Base):
    """Derived analysis: a dependency relationship between two Symbols.

    Canonical invariant: source = dependent (caller/importer/subtype),
    target = dependency (callee/imported/supertype). Impact analysis is
    backward traversal from target to source. See domain-model.md section 5.

    No generic graph-node abstraction: both ends of every edge are Symbol
    rows, nothing else.
    """

    __tablename__ = "graph_edges"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repository_id", "revision_id"],
            ["repository_revisions.repository_id", "repository_revisions.id"],
            ondelete="CASCADE",
            name="fk_graph_edges_repository_revision",
        ),
        ForeignKeyConstraint(
            ["revision_id", "source_symbol_id"],
            ["symbols.revision_id", "symbols.id"],
            ondelete="CASCADE",
            name="fk_graph_edges_revision_source_symbol",
        ),
        ForeignKeyConstraint(
            ["revision_id", "target_symbol_id"],
            ["symbols.revision_id", "symbols.id"],
            ondelete="CASCADE",
            name="fk_graph_edges_revision_target_symbol",
        ),
        ForeignKeyConstraint(
            ["revision_id", "evidence_file_id"],
            ["source_files.revision_id", "source_files.id"],
            ondelete="CASCADE",
            name="fk_graph_edges_revision_evidence_file",
        ),
        CheckConstraint(
            "(target_symbol_id IS NOT NULL AND target_external_ref IS NULL) "
            "OR (target_symbol_id IS NULL AND target_external_ref IS NOT NULL)",
            name="exactly_one_target",
        ),
        CheckConstraint(
            f"relationship_type IN {RELATIONSHIP_TYPES}", name="relationship_type_allowed"
        ),
        CheckConstraint(
            f"resolution_method IN {RESOLUTION_METHODS}", name="resolution_method_allowed"
        ),
        CheckConstraint("confidence >= 0.0 AND confidence <= 1.0", name="confidence_range"),
        # Impact analysis (backward) and dependency listing (forward) are
        # equally common, structurally different query patterns — each needs
        # its own index; one shared index cannot efficiently serve both.
        Index("ix_graph_edges_target_symbol_id_revision_id", "target_symbol_id", "revision_id"),
        Index("ix_graph_edges_source_symbol_id_revision_id", "source_symbol_id", "revision_id"),
        Index("ix_graph_edges_revision_id_relationship_type", "revision_id", "relationship_type"),
        # Dedup: identical evidence must not create duplicate logical edges.
        # Split into two partial unique indexes (rather than one constraint
        # spanning both nullable target columns) because a standard multi-
        # column UNIQUE constraint never flags a duplicate when any column in
        # it is NULL — which target_symbol_id always is on the external-ref
        # branch. See database-design.md.
        Index(
            "uq_graph_edges_dedup_internal",
            "revision_id",
            "relationship_type",
            "source_symbol_id",
            "target_symbol_id",
            "evidence_file_id",
            "evidence_start_line",
            unique=True,
            postgresql_where=text("target_symbol_id IS NOT NULL"),
        ),
        Index(
            "uq_graph_edges_dedup_external",
            "revision_id",
            "relationship_type",
            "source_symbol_id",
            "target_external_ref",
            "evidence_file_id",
            "evidence_start_line",
            unique=True,
            postgresql_where=text("target_symbol_id IS NULL"),
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

    relationship_type: Mapped[str] = mapped_column(Text, nullable=False)

    source_symbol_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    target_symbol_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    target_external_ref: Mapped[str | None] = mapped_column(Text)

    # Nullable: v1 analyzers have no calibrated confidence model. NULL means
    # "not calibrated" — never replaced with an arbitrary numeric stand-in.
    # See graph-construction.md section 7.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    resolution_method: Mapped[str] = mapped_column(Text, nullable=False)

    evidence_file_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    evidence_start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_end_line: Mapped[int | None] = mapped_column(Integer)
    evidence_start_column: Mapped[int | None] = mapped_column(Integer)
    evidence_end_column: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @validates("analysis_run_id")
    def _validate_analysis_run_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, AnalysisRun, value, self.revision_id, "revision_id")
        return value
