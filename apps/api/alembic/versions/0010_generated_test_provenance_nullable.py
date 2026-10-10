"""generated_tests: unknown provenance is NULL, never an empty string

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-09

Testing Intelligence v1.2 (generated-test provenance). Migration 0004 added
provider/model_name/model_version as NOT NULL with server_default '', so
"unknown" could only be written as an empty string, and a service caller
that omitted provenance silently persisted ''. Generation now takes
provider/model from the provider object itself, and model_version is
unknown (the provider reports none), which needs NULL.

  - Drops NOT NULL and the '' server default on all three columns.
  - Rewrites existing '' values to NULL: those rows' provenance was never
    recorded and cannot be recovered; NULL says so honestly.
  - Adds CHECK provenance_not_empty so '' can never be stored again.

Downgrade restores '' for NULL and the original NOT NULL/default.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_COLUMNS = ("provider", "model_name", "model_version")


def upgrade() -> None:
    for column in _COLUMNS:
        op.alter_column(
            "generated_tests", column, existing_type=sa.Text(), nullable=True, server_default=None
        )
        op.execute(f"UPDATE generated_tests SET {column} = NULL WHERE {column} = ''")
    op.create_check_constraint(
        "provenance_not_empty",
        "generated_tests",
        "provider <> '' AND model_name <> '' AND model_version <> ''",
    )


def downgrade() -> None:
    op.drop_constraint("provenance_not_empty", "generated_tests", type_="check")
    for column in _COLUMNS:
        op.execute(f"UPDATE generated_tests SET {column} = '' WHERE {column} IS NULL")
        op.alter_column(
            "generated_tests", column, existing_type=sa.Text(), nullable=False, server_default=""
        )
