"""analysis_runs: a deleted parent revision deletes the automation runs computed against it

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-09

Automation Intelligence v1.1. `parent_revision_id` (migration 0007) is part
of an automation run's identity: the partial unique indexes key on it, and
the run's delta and impact are only meaningful relative to that parent.
With ON DELETE SET NULL, deleting the parent revision rewrote a with-parent
run into a no-parent run, silently changing its identity; when the same
revision also had a no-parent run, the rewrite collided with
uq_analysis_runs_automation_without_parent and the delete (including a
whole-repository delete) failed.

ON DELETE CASCADE removes those runs with their parent instead, the same
lifecycle every other revision-derived row already has. The FK stays
simple (not composite), as 0007 explains; only the delete action changes,
and only automation rows ever populate this column.

Downgrade restores ON DELETE SET NULL.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_FK = "fk_analysis_runs_parent_revision_id_repository_revisions"


def _recreate(ondelete: str) -> None:
    op.drop_constraint(_FK, "analysis_runs", type_="foreignkey")
    op.create_foreign_key(
        _FK,
        "analysis_runs",
        "repository_revisions",
        ["parent_revision_id"],
        ["id"],
        ondelete=ondelete,
    )


def upgrade() -> None:
    _recreate("CASCADE")


def downgrade() -> None:
    _recreate("SET NULL")
