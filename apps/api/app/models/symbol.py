import uuid
from datetime import datetime

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
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base
from app.models._validation import validate_same_owner
from app.models.analysis import AnalysisRun

SYMBOL_KINDS = (
    "function",
    "method",
    "class",
    "interface",
    "type_alias",
    "module",
    "variable",
    "constant",
    "enum",
    "property",
    "namespace",
    "struct",
    "trait",
)


class Symbol(Base):
    """Derived analysis: a function/class/module/etc. extracted from a parse.

    Always revision-scoped and immutable — see domain-model.md sections 4 and 13.
    Not every language/analyzer produces every field (signature, columns);
    nullability reflects that, not sloppiness.
    """

    __tablename__ = "symbols"
    __table_args__ = (
        ForeignKeyConstraint(
            ["repository_id", "revision_id"],
            ["repository_revisions.repository_id", "repository_revisions.id"],
            ondelete="CASCADE",
            name="fk_symbols_repository_revision",
        ),
        ForeignKeyConstraint(
            ["revision_id", "file_id"],
            ["source_files.revision_id", "source_files.id"],
            ondelete="CASCADE",
            name="fk_symbols_revision_file",
        ),
        # Self-referential composite FK, safe with RESTRICT (unlike SET NULL):
        # RESTRICT never writes to a row, so it doesn't hit the "would null a
        # NOT NULL column" problem, and because both the parent and child
        # symbol rows for a revision are always deleted together in the same
        # cascade sweep (via the repository_revisions -> symbols CASCADE
        # above), RESTRICT never actually fires on a legitimate deletion —
        # only if something tried to delete one symbol while a child in the
        # same revision still pointed at it, which is exactly what should be
        # rejected. See database-design.md "Revision-scoped reference
        # enforcement".
        ForeignKeyConstraint(
            ["revision_id", "parent_symbol_id"],
            ["symbols.revision_id", "symbols.id"],
            ondelete="RESTRICT",
            name="fk_symbols_revision_parent_symbol",
        ),
        # Supports composite FKs from GraphEdge/CodeChunk/GeneratedTest/self.
        UniqueConstraint("revision_id", "id", name="uq_symbols_revision_id_id"),
        Index("ix_symbols_file_id", "file_id"),
        Index("ix_symbols_repository_id_qualified_name", "repository_id", "qualified_name"),
        Index("ix_symbols_parent_symbol_id", "parent_symbol_id"),
        CheckConstraint(f"kind IN {SYMBOL_KINDS}", name="kind_allowed"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    # Provenance pointer only — deliberately SET NULL on delete, not part of
    # the composite revision-consistency chain (see database-design.md).
    # `_validate_analysis_run_id` below is the application-level backstop.
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analysis_runs.id", ondelete="SET NULL")
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    qualified_name: Mapped[str] = mapped_column(Text, nullable=False)
    signature: Mapped[str | None] = mapped_column(Text)

    start_line: Mapped[int] = mapped_column(Integer, nullable=False)
    end_line: Mapped[int] = mapped_column(Integer, nullable=False)
    start_column: Mapped[int | None] = mapped_column(Integer)
    end_column: Mapped[int | None] = mapped_column(Integer)

    # DB-enforced same-revision via the composite FK in __table_args__ above.
    parent_symbol_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))

    visibility: Mapped[str | None] = mapped_column(Text)
    analyzer: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    @validates("analysis_run_id")
    def _validate_analysis_run_id(self, key: str, value: uuid.UUID | None) -> uuid.UUID | None:
        validate_same_owner(self, key, AnalysisRun, value, self.revision_id, "revision_id")
        return value
