"""Static schema validation that needs no live database connection: compiles
the full DDL against the Postgres dialect via a mock engine, and cross-checks
it against what the hand-written Alembic migration actually produces. This is
what still runs — and still means something — when PostgreSQL isn't reachable
and every test in tests/db/ skips. See docs/architecture/database-design.md.
"""

import re
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_mock_engine
from sqlalchemy.schema import CreateTable

import app.models  # noqa: F401
from app.db.base import Base

EXPECTED_TABLES = {
    "repositories",
    "repository_revisions",
    "source_blobs",
    "source_files",
    "symbols",
    "graph_edges",
    "analysis_runs",
    "security_findings",
    "security_finding_observations",
    "code_chunks",
    "embeddings",
    "generated_tests",
    "test_executions",
    "generated_artifacts",
    "investigation_sessions",
    "session_messages",
    "session_context_refs",
}


def test_all_expected_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_full_ddl_compiles_against_postgres_dialect() -> None:
    statements: list[str] = []

    def _record(sql, *multiparams, **params) -> None:
        statements.append(str(sql.compile(dialect=mock_engine.dialect)))

    mock_engine = create_mock_engine("postgresql+psycopg://", _record)
    Base.metadata.create_all(mock_engine, checkfirst=False)

    assert len(statements) > 0
    create_table_statements = [s for s in statements if s.strip().startswith("CREATE TABLE")]
    assert len(create_table_statements) == len(EXPECTED_TABLES)


def test_each_table_compiles_individually() -> None:
    """A narrower sanity check: every table's own CREATE TABLE renders
    without error in isolation, independent of cross-table ordering."""
    for table in Base.metadata.tables.values():
        CreateTable(table).compile(
            dialect=create_mock_engine("postgresql+psycopg://", lambda *a, **k: None).dialect
        )


def _model_ddl() -> str:
    statements: list[str] = []

    def _record(sql, *multiparams, **params) -> None:
        statements.append(str(sql.compile(dialect=mock_engine.dialect)))

    mock_engine = create_mock_engine("postgresql+psycopg://", _record)
    Base.metadata.create_all(mock_engine, checkfirst=False)
    return "\n".join(statements)


def _migration_ddl() -> str:
    """Runs the real Alembic migration in offline mode — no DB connection —
    and returns the literal SQL it would issue. This exercises the migration
    file itself (not just the ORM models), the same way `alembic upgrade head
    --sql` does on the command line."""
    api_dir = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head", "--sql"],
        cwd=api_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _constraint_names(ddl: str, prefix: str) -> set[str]:
    return set(re.findall(rf"CONSTRAINT ({prefix}_[a-z_0-9]+)", ddl))


def test_migration_and_models_define_the_same_tables_and_constraints() -> None:
    """Task 3 (migration/model parity): the hand-written Alembic migration
    and the SQLAlchemy models must describe the same schema. Catches drift
    between the two — including the real bug this caught once already (see
    database-design.md's "CHECK constraint naming" section): a naming
    double-prefix that would have silently shipped an ugly-but-working schema."""
    model_ddl = _model_ddl()
    migration_ddl = _migration_ddl()

    table_pattern = r"CREATE TABLE ([a-z_]+)"
    migration_tables = set(re.findall(table_pattern, migration_ddl)) - {"alembic_version"}
    assert migration_tables == set(re.findall(table_pattern, model_ddl)) == EXPECTED_TABLES

    for prefix in ("ck", "fk", "uq", "pk"):
        model_names = _constraint_names(model_ddl, prefix)
        migration_names = _constraint_names(migration_ddl, prefix)
        assert model_names == migration_names, (
            f"{prefix.upper()} constraint names differ between the SQLAlchemy models "
            f"and the Alembic migration.\nOnly in models: {model_names - migration_names}\n"
            f"Only in migration: {migration_names - model_names}"
        )
