"""graph_edges: confidence nullable, resolution_method gains ast_resolution

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06

Two independent corrections to the graph-construction v1 implementation,
neither touching any other table:

1. `graph_edges.confidence` was NOT NULL, forcing the Python graph builder
   to write a fixed `1.0` for every edge — a fabricated score, not an
   absence of one. Made nullable so NULL can mean "not calibrated."
   `ck_graph_edges_confidence_range` is preserved unchanged: Postgres CHECK
   constraints already treat a NULL operand as passing (3-valued SQL logic),
   so no constraint rewrite is needed for this half.

2. `graph_edges.resolution_method` previously allowed only `'syntactic'` or
   `'semantic'`, with `'semantic'` defined elsewhere (domain-model.md) as
   specifically type-checker-backed. The v1 Python resolver does
   deterministic AST-based scope/import/binding resolution with no type
   inference — neither existing value describes that precisely. Added a
   third allowed value, `'ast_resolution'`, without removing the other two
   (a future naive/heuristic resolver may still legitimately write
   `'syntactic'`; a future type-checker-backed resolver may still write
   `'semantic'`). This requires dropping and recreating
   `ck_graph_edges_resolution_method_allowed` since it's a plain CHECK, not
   a native Postgres ENUM type.

Not run against a live PostgreSQL instance when authored — see 0001's own
header for why, and docs/architecture/database-design.md for the same
caveat applied here.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_OLD_RESOLUTION_METHODS = ("syntactic", "semantic")
_NEW_RESOLUTION_METHODS = ("syntactic", "semantic", "ast_resolution")


def upgrade() -> None:
    op.alter_column("graph_edges", "confidence", existing_type=sa.Float(), nullable=True)

    op.drop_constraint("resolution_method_allowed", "graph_edges", type_="check")
    op.create_check_constraint(
        "resolution_method_allowed",
        "graph_edges",
        f"resolution_method IN {_NEW_RESOLUTION_METHODS}",
    )


def downgrade() -> None:
    op.drop_constraint("resolution_method_allowed", "graph_edges", type_="check")
    op.create_check_constraint(
        "resolution_method_allowed",
        "graph_edges",
        f"resolution_method IN {_OLD_RESOLUTION_METHODS}",
    )

    op.alter_column("graph_edges", "confidence", existing_type=sa.Float(), nullable=False)
