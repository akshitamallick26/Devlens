"""generated_tests/test_executions: provenance, validation, execution detail

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07

Testing / Generated Tests v1's forensic audit (Task 1) found that
`app/models/testing.py`'s original design — written during Phase 0, before
this capability existed — predates the actual v1 spec in the same way
`app/models/rag.py::Embedding` originally predated RAG v1's provider/model/
model_version split. Three corrections, all additive:

1. `generated_tests.status` allowed values change from the original
   `generated|accepted|rejected|superseded` (a human-review workflow that
   was never built) to `generated|validation_failed|ready|execution_failed|
   passed|failed` (the actual generate -> validate -> execute pipeline).
   This is a value-set change on an empty-in-production column (this table
   has never been written to — Testing v1 is what first writes to it), so
   no data backfill is needed; the CHECK is simply dropped and recreated.

2. `generated_tests` gains explicit `provider`/`model_name`/`model_version`
   columns (same convention as `embeddings`, migration `0001`) plus
   `validation_status`/`validation_errors`/`rationale`/`cited_evidence_ids`
   — provenance and pre-execution-validation metadata the original design
   never anticipated. `server_default=''` on the three identity columns
   mirrors `embeddings.model_version`'s own reasoning: a real default is
   needed for the ALTER on an existing table, not a design claim about what
   "no model" should mean.

3. `test_executions` gains `exit_code`/`stdout`/`stderr`/`timed_out`/
   `validation_status`. The original `output` (combined stdout+stderr)
   column is kept, not dropped — this phase's instructions prohibit
   destructive migrations — but is no longer written to by any v1 code;
   `stdout`/`stderr` are now split explicitly so a failure's actual error
   output isn't interleaved with routine progress output.

Not run against a live PostgreSQL instance when authored — same caveat as
`0001`/`0002`/`0003`'s own headers.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_OLD_GENERATED_TEST_STATUSES = ("generated", "accepted", "rejected", "superseded")
_NEW_GENERATED_TEST_STATUSES = (
    "generated",
    "validation_failed",
    "ready",
    "execution_failed",
    "passed",
    "failed",
)
_VALIDATION_STATUSES = ("pending", "passed", "failed")


def upgrade() -> None:
    op.drop_constraint("status_allowed", "generated_tests", type_="check")
    op.create_check_constraint(
        "status_allowed", "generated_tests", f"status IN {_NEW_GENERATED_TEST_STATUSES}"
    )

    op.add_column(
        "generated_tests",
        sa.Column("provider", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "generated_tests",
        sa.Column("model_name", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "generated_tests",
        sa.Column("model_version", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "generated_tests",
        sa.Column("validation_status", sa.Text(), nullable=False, server_default="pending"),
    )
    op.create_check_constraint(
        "validation_status_allowed",
        "generated_tests",
        f"validation_status IN {_VALIDATION_STATUSES}",
    )
    op.add_column("generated_tests", sa.Column("validation_errors", JSONB, nullable=True))
    op.add_column("generated_tests", sa.Column("rationale", sa.Text(), nullable=True))
    op.add_column("generated_tests", sa.Column("cited_evidence_ids", JSONB, nullable=True))

    op.add_column("test_executions", sa.Column("exit_code", sa.Integer(), nullable=True))
    op.add_column("test_executions", sa.Column("stdout", sa.Text(), nullable=True))
    op.add_column("test_executions", sa.Column("stderr", sa.Text(), nullable=True))
    op.add_column(
        "test_executions",
        sa.Column("timed_out", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "test_executions",
        sa.Column("validation_status", sa.Text(), nullable=False, server_default="pending"),
    )
    op.create_check_constraint(
        "validation_status_allowed",
        "test_executions",
        f"validation_status IN {_VALIDATION_STATUSES}",
    )


def downgrade() -> None:
    op.drop_constraint("validation_status_allowed", "test_executions", type_="check")
    op.drop_column("test_executions", "validation_status")
    op.drop_column("test_executions", "timed_out")
    op.drop_column("test_executions", "stderr")
    op.drop_column("test_executions", "stdout")
    op.drop_column("test_executions", "exit_code")

    op.drop_column("generated_tests", "cited_evidence_ids")
    op.drop_column("generated_tests", "rationale")
    op.drop_column("generated_tests", "validation_errors")
    op.drop_constraint("validation_status_allowed", "generated_tests", type_="check")
    op.drop_column("generated_tests", "validation_status")
    op.drop_column("generated_tests", "model_version")
    op.drop_column("generated_tests", "model_name")
    op.drop_column("generated_tests", "provider")

    op.drop_constraint("status_allowed", "generated_tests", type_="check")
    op.create_check_constraint(
        "status_allowed", "generated_tests", f"status IN {_OLD_GENERATED_TEST_STATUSES}"
    )
