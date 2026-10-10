"""analysis_runs: automation_analysis type, partial status

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-08

Automation Intelligence v1's forensic audit (Task 1/11) confirmed
`AnalysisRun` already has everything a coordinating run needs structurally
(repository_id/revision_id, analyzer identity, status/timestamps, a
freeform `config` JSONB for analyzer-specific parameters) — no new table is
needed. Two additive, backward-compatible CHECK-constraint widenings:

1. `analysis_type` gains `'automation_analysis'` — the one new run kind
   this phase introduces (revision-delta computation, impact reuse, and
   cross-capability invalidation decisions).

2. `status` gains `'partial'` — Task 11's explicit requirement that an
   automation run which could not update all required deterministic
   intelligence must never be reported as `'completed'`. No existing
   analyzer (parsing, graph_build, etc.) is required to ever use this
   value; it is additive, not a redefinition of what any existing status
   means for them.

Automation-specific fields (parent_revision_id, changed/impacted counts,
limitations) are stored in `AnalysisRun.config` (JSONB) — exactly what that
column is documented for ("analyzer-specific parameters that nothing needs
to filter or join on"), so no new columns are needed either.

Not run against a live PostgreSQL instance when authored — same caveat as
every prior migration's own header.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_OLD_ANALYSIS_TYPES = (
    "ingestion",
    "parsing",
    "graph_build",
    "impact_analysis",
    "rag_indexing",
    "security_scan",
    "test_generation",
    "documentation_generation",
)
_NEW_ANALYSIS_TYPES = (*_OLD_ANALYSIS_TYPES, "automation_analysis")

_OLD_STATUSES = ("pending", "running", "completed", "failed", "cancelled")
_NEW_STATUSES = ("pending", "running", "completed", "partial", "failed", "cancelled")


def upgrade() -> None:
    op.drop_constraint("analysis_type_allowed", "analysis_runs", type_="check")
    op.create_check_constraint(
        "analysis_type_allowed", "analysis_runs", f"analysis_type IN {_NEW_ANALYSIS_TYPES}"
    )

    op.drop_constraint("status_allowed", "analysis_runs", type_="check")
    op.create_check_constraint("status_allowed", "analysis_runs", f"status IN {_NEW_STATUSES}")


def downgrade() -> None:
    op.drop_constraint("status_allowed", "analysis_runs", type_="check")
    op.create_check_constraint("status_allowed", "analysis_runs", f"status IN {_OLD_STATUSES}")

    op.drop_constraint("analysis_type_allowed", "analysis_runs", type_="check")
    op.create_check_constraint(
        "analysis_type_allowed", "analysis_runs", f"analysis_type IN {_OLD_ANALYSIS_TYPES}"
    )
