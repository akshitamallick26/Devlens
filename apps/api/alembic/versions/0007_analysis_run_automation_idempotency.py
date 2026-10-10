"""analysis_runs: parent_revision_id + automation idempotency indexes

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-08

Automation Intelligence v1 hardening, Blocker 1: the original
implementation intentionally created a new `AnalysisRun` row on every
automation call, matching every other analyzer type's append-only-history
convention — but automation has its own, different idempotency contract
("repeated calls for the same logical (repository, revision, parent
revision) must not create uncontrolled duplicates"), which that append-only
behavior does not satisfy.

Two additive changes, both automation-specific — no other analyzer type's
behavior changes:

1. `parent_revision_id` (nullable, simple FK — see app/models/analysis.py's
   docstring for why not composite) — NULL for every row except automation
   runs, which record the exact parent revision (or NULL for a root-commit
   run) they were computed against.

2. Two partial unique indexes, scoped by `WHERE analysis_type =
   'automation_analysis'` so no other analyzer type is ever affected, and
   split into a "has a parent" and "root commit" pair (rather than one
   constraint over the nullable `parent_revision_id`) for the identical
   reason `graph_edges`' own dedup indexes are split in migration `0001`:
   a plain UNIQUE constraint treats every NULL as distinct from every other
   NULL, which would let duplicate root-revision (no-parent) automation
   runs through unnoticed.

Not run against a live PostgreSQL instance when authored — same caveat as
every prior migration's own header.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("analysis_runs", sa.Column("parent_revision_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_analysis_runs_parent_revision_id_repository_revisions",
        "analysis_runs",
        "repository_revisions",
        ["parent_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_index(
        "uq_analysis_runs_automation_with_parent",
        "analysis_runs",
        ["repository_id", "revision_id", "parent_revision_id", "analysis_type"],
        unique=True,
        postgresql_where=sa.text(
            "analysis_type = 'automation_analysis' AND parent_revision_id IS NOT NULL"
        ),
    )
    op.create_index(
        "uq_analysis_runs_automation_without_parent",
        "analysis_runs",
        ["repository_id", "revision_id", "analysis_type"],
        unique=True,
        postgresql_where=sa.text(
            "analysis_type = 'automation_analysis' AND parent_revision_id IS NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_analysis_runs_automation_without_parent", table_name="analysis_runs")
    op.drop_index("uq_analysis_runs_automation_with_parent", table_name="analysis_runs")
    op.drop_constraint(
        "fk_analysis_runs_parent_revision_id_repository_revisions",
        "analysis_runs",
        type_="foreignkey",
    )
    op.drop_column("analysis_runs", "parent_revision_id")
