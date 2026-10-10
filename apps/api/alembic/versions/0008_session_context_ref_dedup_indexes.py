"""session_context_refs: duplicate-reference prevention indexes

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-08

Investigation Sessions & Recents v1 (Task 14, concurrency/idempotency):
adding the same context reference (e.g. the same file) to a session twice
must not create a duplicate row, and this must be enforced by the database,
not only by an application-level check-then-act. Four partial unique
indexes, one per reference-typed `ref_type` (mirroring the exact pattern
`graph_edges`/`analysis_runs` already use for their own dedup indexes,
migrations 0001/0007): a plain UNIQUE constraint across the nullable
`file_id`/`symbol_id`/`analysis_run_id`/`finding_id` columns would never
fire, since Postgres treats every NULL as distinct from every other NULL.
`graph_focus` rows are deliberately excluded — repeated camera-state
breadcrumbs at different points in a session are not duplicates.

Purely additive — no existing column, table, or migration is touched.
Not run against a live PostgreSQL instance when authored — same caveat as
every prior migration's own header.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_session_context_refs_session_id_file_id",
        "session_context_refs",
        ["session_id", "file_id"],
        unique=True,
        postgresql_where=sa.text("ref_type = 'selected_file'"),
    )
    op.create_index(
        "uq_session_context_refs_session_id_symbol_id",
        "session_context_refs",
        ["session_id", "symbol_id"],
        unique=True,
        postgresql_where=sa.text("ref_type = 'selected_symbol'"),
    )
    op.create_index(
        "uq_session_context_refs_session_id_analysis_run_id",
        "session_context_refs",
        ["session_id", "analysis_run_id"],
        unique=True,
        postgresql_where=sa.text("ref_type = 'analysis_reference'"),
    )
    op.create_index(
        "uq_session_context_refs_session_id_finding_id",
        "session_context_refs",
        ["session_id", "finding_id"],
        unique=True,
        postgresql_where=sa.text("ref_type = 'finding_reference'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_session_context_refs_session_id_finding_id", table_name="session_context_refs"
    )
    op.drop_index(
        "uq_session_context_refs_session_id_analysis_run_id", table_name="session_context_refs"
    )
    op.drop_index("uq_session_context_refs_session_id_symbol_id", table_name="session_context_refs")
    op.drop_index("uq_session_context_refs_session_id_file_id", table_name="session_context_refs")
