"""security_findings/security_finding_observations: scanner metadata

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-07

Security Intelligence v1's forensic audit (Task 1) found
`app/models/security.py`'s original design (Phase 0, before this capability
existed) already correctly split identity (SecurityFinding) from occurrence
(SecurityFindingObservation) — no structural rework needed. Three additive
corrections, all required by the actual scanner-integration spec:

1. `security_findings.severity` was NOT NULL, which would force a fabricated
   severity whenever a scanner/rule supplies none. Made nullable so NULL
   means "scanner did not supply a severity," never a guessed default —
   the same "nullable means not calibrated, never a stand-in value"
   precedent `graph_edges.confidence` already established in migration
   `0002`.

2. `security_findings` gains `cvss_score`/`cvss_vector`/`scanner_confidence`/
   `references` — advisory-level metadata the original design didn't
   anticipate, populated only when a scanner actually supplies it.

3. `security_finding_observations` gains `affected_package`/
   `installed_version`/`affected_range`/`fixed_version` (occurrence-level
   dependency metadata — these vary per observation, e.g. the same advisory
   pinned at different versions in different lockfiles) and
   `resolution_status` (Task 8's explicit graph-mapping-attempt record:
   unresolved | exact_symbol | exact_file | ambiguous — never a guessed
   symbol link).

Not run against a live PostgreSQL instance when authored — same caveat as
every prior migration's own header.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_RESOLUTION_STATUSES = ("unresolved", "exact_symbol", "exact_file", "ambiguous")


def upgrade() -> None:
    op.alter_column("security_findings", "severity", existing_type=sa.Text(), nullable=True)

    op.add_column("security_findings", sa.Column("cvss_score", sa.Float(), nullable=True))
    op.add_column("security_findings", sa.Column("cvss_vector", sa.Text(), nullable=True))
    op.add_column("security_findings", sa.Column("scanner_confidence", sa.Text(), nullable=True))
    op.add_column("security_findings", sa.Column("references", JSONB, nullable=True))

    op.add_column(
        "security_finding_observations", sa.Column("affected_package", sa.Text(), nullable=True)
    )
    op.add_column(
        "security_finding_observations", sa.Column("installed_version", sa.Text(), nullable=True)
    )
    op.add_column(
        "security_finding_observations", sa.Column("affected_range", sa.Text(), nullable=True)
    )
    op.add_column(
        "security_finding_observations", sa.Column("fixed_version", sa.Text(), nullable=True)
    )
    op.add_column(
        "security_finding_observations",
        sa.Column("resolution_status", sa.Text(), nullable=False, server_default="unresolved"),
    )
    op.create_check_constraint(
        "resolution_status_allowed",
        "security_finding_observations",
        f"resolution_status IN {_RESOLUTION_STATUSES}",
    )


def downgrade() -> None:
    op.drop_constraint("resolution_status_allowed", "security_finding_observations", type_="check")
    op.drop_column("security_finding_observations", "resolution_status")
    op.drop_column("security_finding_observations", "fixed_version")
    op.drop_column("security_finding_observations", "affected_range")
    op.drop_column("security_finding_observations", "installed_version")
    op.drop_column("security_finding_observations", "affected_package")

    op.drop_column("security_findings", "references")
    op.drop_column("security_findings", "scanner_confidence")
    op.drop_column("security_findings", "cvss_vector")
    op.drop_column("security_findings", "cvss_score")

    op.alter_column("security_findings", "severity", existing_type=sa.Text(), nullable=False)
