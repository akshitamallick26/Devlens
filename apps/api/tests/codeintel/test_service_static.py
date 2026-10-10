"""Database-free checks for app/codeintel/service.py: every read function
runs against a recording fake Session that compiles each statement with the
PostgreSQL dialect. Proves, without a live database, that the statements
compile, that every statement on a revision-local table binds both the
requested repository and revision ids (and no other repository/revision),
and that no read path writes. Live semantics are in test_codeintel_db.py."""

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Executable

from app.codeintel import service
from app.models import Repository, RepositoryRevision, SourceFile

REVISION_LOCAL_TABLES = (
    "source_files",
    "symbols",
    "graph_edges",
    "analysis_runs",
    "generated_tests",
    "test_executions",
    "security_finding_observations",
)

REPO = Repository(id=uuid.uuid4(), owner="o", name="r", url="https://x/o/r", default_branch="m")
REV = RepositoryRevision(
    id=uuid.uuid4(), repository_id=REPO.id, commit_sha="a" * 40, committed_at=datetime.now(UTC)
)
FILE = SourceFile(
    id=uuid.uuid4(),
    repository_id=REPO.id,
    revision_id=REV.id,
    path="app/m.py",
    content_hash="c" * 64,
    size_bytes=10,
    parsing_status="parsed",
)
TARGET = uuid.uuid4()  # file/symbol/run id handed back by single-row lookups


class _Result:
    def __init__(self, sql: str) -> None:
        self.sql = sql

    def scalar_one_or_none(self) -> Any:
        if "FROM source_blobs" in self.sql:
            return "line one\nline two"
        if self.sql.startswith("SELECT source_files."):
            return FILE
        return TARGET

    def scalar_one(self) -> int:
        return 0

    def one(self) -> tuple[int, int]:
        return (0, 0)

    def one_or_none(self) -> None:
        return None

    def all(self) -> list[Any]:
        return []

    def scalars(self) -> "_Result":
        return self


@dataclass
class _RecordingSession:
    statements: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def get(self, model: type, ident: uuid.UUID) -> Any:
        return {(Repository, REPO.id): REPO, (RepositoryRevision, REV.id): REV}.get((model, ident))

    def execute(self, statement: Executable) -> _Result:
        compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
        self.statements.append((str(compiled), dict(compiled.params)))
        return _Result(str(compiled))

    def _forbidden(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("code intelligence reads must not write")

    add = add_all = flush = commit = delete = merge = _forbidden


CALLS: dict[str, Callable[[Any], object]] = {
    "browse_root": lambda db: service.browse_directory(db, REPO.id, REV.id, ""),
    "browse_dir": lambda db: service.browse_directory(db, REPO.id, REV.id, "app/api"),
    "file_detail": lambda db: service.get_file_detail(db, REPO.id, REV.id, FILE.id),
    "source": lambda db: service.get_source_range(db, REPO.id, REV.id, FILE.id, 1, 50),
    "search": lambda db: service.search_symbols(db, REPO.id, REV.id, "add_%", 10),
    "neighborhood": lambda db: service.graph_neighborhood(
        db, REPO.id, REV.id, TARGET, "both", 3, 50
    ),
    "findings": lambda db: service.list_findings(db, REPO.id, REV.id),
    "tests": lambda db: service.list_generated_tests(db, REPO.id, REV.id),
}


def scope_violations(
    sql: str,
    params: dict[str, Any],
    repo: uuid.UUID,
    rev: uuid.UUID,
    tables: tuple[str, ...] = REVISION_LOCAL_TABLES,
) -> list[str]:
    """Per-table check: every revision-local table in the statement must
    carry `<table>.repository_id = :p` and `<table>.revision_id = :q` bound to
    the requested ids, unless it is joined on its own primary key from
    another table (`<table>.id = other.fk`), which inherits that table's
    scope."""
    problems = []
    for table in tables:
        # Each occurrence is checked under the name it is referenced by in
        # the statement: the table itself, or its alias (`symbols AS
        # symbols_1`), so a self-join must scope every alias.
        names = {
            alias or table for alias in re.findall(rf"\b(?:FROM|JOIN) {table}\b(?: AS (\w+))?", sql)
        }
        for name in sorted(names):
            if re.search(rf"\b{name}\.id = \w+\.\w+", sql):
                continue
            for column, expected in (("repository_id", repo), ("revision_id", rev)):
                bound = [
                    params.get(param)
                    for param in re.findall(rf"\b{name}\.{column} = %\((\w+)\)s", sql)
                ]
                if expected not in bound:
                    problems.append(f"{name} lacks {column} = requested id")
    return problems


@pytest.mark.parametrize("name", sorted(CALLS))
def test_reads_compile_bind_scope_and_never_write(name: str) -> None:
    db = _RecordingSession()
    CALLS[name](db)
    assert db.statements
    for sql, params in db.statements:
        assert scope_violations(sql, params, REPO.id, REV.id) == [], sql


def _compiled(statement: Any) -> tuple[str, dict[str, Any]]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


def test_scope_checker_detects_a_missing_repository_predicate() -> None:
    from sqlalchemy import select

    sql, params = _compiled(select(SourceFile).where(SourceFile.revision_id == REV.id))
    assert scope_violations(sql, params, REPO.id, REV.id) == [
        "source_files lacks repository_id = requested id"
    ]


def test_scope_checker_detects_a_joined_table_without_its_own_scope() -> None:
    from sqlalchemy import select

    from app.models import GraphEdge, Symbol

    statement = (
        select(GraphEdge.id)
        .join(Symbol, Symbol.qualified_name == GraphEdge.target_external_ref)
        .where(GraphEdge.repository_id == REPO.id, GraphEdge.revision_id == REV.id)
    )
    sql, params = _compiled(statement)
    assert "symbols lacks revision_id = requested id" in scope_violations(
        sql, params, REPO.id, REV.id
    )


def test_scope_checker_detects_the_wrong_revision_id() -> None:
    from sqlalchemy import select

    other = uuid.uuid4()
    sql, params = _compiled(
        select(SourceFile).where(
            SourceFile.repository_id == REPO.id, SourceFile.revision_id == other
        )
    )
    assert scope_violations(sql, params, REPO.id, REV.id) == [
        "source_files lacks revision_id = requested id"
    ]


def test_symbol_detail_missing_symbol_is_not_found() -> None:
    with pytest.raises(service.NotFoundError):
        service.get_symbol_detail(_RecordingSession(), REPO.id, REV.id, TARGET)


def test_like_wildcards_in_queries_are_escaped() -> None:
    db = _RecordingSession()
    service.search_symbols(db, REPO.id, REV.id, "add_%", 10)
    sql, params = db.statements[-1]
    assert "ESCAPE" in sql
    assert any(isinstance(v, str) and "/_/%" in v for v in params.values())


def test_source_range_returns_only_requested_lines() -> None:
    result = service.get_source_range(_RecordingSession(), REPO.id, REV.id, FILE.id, 2, 9)
    assert result.lines == ("line two",)
    assert (result.start_line, result.end_line, result.total_lines) == (2, 2, 2)
    assert result.truncated is False


def test_wrong_repository_revision_is_a_conflict() -> None:
    other_repo = uuid.uuid4()
    db = _RecordingSession()
    db.get = lambda model, ident: (  # type: ignore[method-assign]
        Repository(id=other_repo) if model is Repository else REV
    )
    with pytest.raises(service.RevisionConflictError):
        service.browse_directory(db, other_repo, REV.id)


def test_scope_checker_requires_every_alias_of_a_self_join_to_be_scoped() -> None:
    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    from app.models import GraphEdge, Symbol

    target = aliased(Symbol)
    statement = (
        select(GraphEdge.id)
        .join(target, target.qualified_name == GraphEdge.target_external_ref)
        .where(GraphEdge.repository_id == REPO.id, GraphEdge.revision_id == REV.id)
    )
    sql, params = _compiled(statement)
    assert "symbols_1 lacks revision_id = requested id" in scope_violations(
        sql, params, REPO.id, REV.id
    )
