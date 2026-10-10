"""generated_artifacts: documentation fields, provenance, idempotency

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-08

Documentation Intelligence v1 (Task 24). Additive only — no existing
column, table, or historical migration touched; `architecture_summary`/
`ai_report` kind rows (currently none exist anywhere in this codebase) are
untouched since every new column stays NULL/empty-default for them.

Adds:
  - artifact_type, generation_method (nullable text) — which of the four
    documentation sub-types this is, and whether its current content was
    LLM-authored or deterministic-fallback-authored. Required (via CHECK)
    exactly when kind='documentation'.
  - provider/model_name/model_version (text, server_default='') —
    provenance identity, same convention as generated_tests' own columns.
  - evidence_ids/cited_evidence_ids (nullable JSONB) — ids only, never
    evidence content (see generated_tests.cited_evidence_ids precedent).
  - context_truncated (boolean, NOT NULL, server_default false) — Task 6:
    never silently exceed a context budget.
  - One CHECK constraint tying artifact_type/generation_method
    requiredness to kind='documentation'.
  - Three partial unique indexes for idempotent upsert-in-place generation
    (Task 12), split by target shape (none/file/symbol) — the identical
    pattern analysis_runs' own automation-idempotency indexes (migration
    0007) already established for a two-way split.

Not run against a live PostgreSQL instance when authored — same caveat as
every prior migration's own header.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_DOCUMENTATION_ARTIFACT_TYPES = (
    "repository_overview",
    "architecture_overview",
    "module_documentation",
    "symbol_documentation",
)
_GENERATION_METHODS = ("llm_generated", "deterministic_fallback")


def upgrade() -> None:
    op.add_column("generated_artifacts", sa.Column("artifact_type", sa.Text(), nullable=True))
    op.add_column("generated_artifacts", sa.Column("generation_method", sa.Text(), nullable=True))
    op.add_column(
        "generated_artifacts",
        sa.Column("provider", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "generated_artifacts",
        sa.Column("model_name", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "generated_artifacts",
        sa.Column("model_version", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column("generated_artifacts", sa.Column("evidence_ids", JSONB, nullable=True))
    op.add_column("generated_artifacts", sa.Column("cited_evidence_ids", JSONB, nullable=True))
    op.add_column(
        "generated_artifacts",
        sa.Column(
            "context_truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )

    artifact_types_sql = ", ".join(f"'{t}'" for t in _DOCUMENTATION_ARTIFACT_TYPES)
    generation_methods_sql = ", ".join(f"'{m}'" for m in _GENERATION_METHODS)
    op.create_check_constraint(
        "documentation_fields_consistent",
        "generated_artifacts",
        "(kind <> 'documentation' AND artifact_type IS NULL AND generation_method IS NULL) "
        f"OR (kind = 'documentation' AND artifact_type IN ({artifact_types_sql}) "
        f"AND generation_method IN ({generation_methods_sql}))",
    )

    op.create_index(
        "uq_generated_artifacts_doc_no_target",
        "generated_artifacts",
        ["repository_id", "revision_id", "artifact_type"],
        unique=True,
        postgresql_where=sa.text(
            "artifact_type IN ('repository_overview', 'architecture_overview')"
        ),
    )
    op.create_index(
        "uq_generated_artifacts_doc_file_target",
        "generated_artifacts",
        ["repository_id", "revision_id", "artifact_type", "related_file_id"],
        unique=True,
        postgresql_where=sa.text("artifact_type = 'module_documentation'"),
    )
    op.create_index(
        "uq_generated_artifacts_doc_symbol_target",
        "generated_artifacts",
        ["repository_id", "revision_id", "artifact_type", "related_symbol_id"],
        unique=True,
        postgresql_where=sa.text("artifact_type = 'symbol_documentation'"),
    )


def downgrade() -> None:
    op.drop_index("uq_generated_artifacts_doc_symbol_target", table_name="generated_artifacts")
    op.drop_index("uq_generated_artifacts_doc_file_target", table_name="generated_artifacts")
    op.drop_index("uq_generated_artifacts_doc_no_target", table_name="generated_artifacts")
    op.drop_constraint("documentation_fields_consistent", "generated_artifacts", type_="check")
    op.drop_column("generated_artifacts", "context_truncated")
    op.drop_column("generated_artifacts", "cited_evidence_ids")
    op.drop_column("generated_artifacts", "evidence_ids")
    op.drop_column("generated_artifacts", "model_version")
    op.drop_column("generated_artifacts", "model_name")
    op.drop_column("generated_artifacts", "provider")
    op.drop_column("generated_artifacts", "generation_method")
    op.drop_column("generated_artifacts", "artifact_type")
