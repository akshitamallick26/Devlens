"""Database-free tests for app/overview/service.py: the whole service runs
against a recording fake Session that compiles every statement with the
PostgreSQL dialect and returns empty results. This proves, without a live
database:

- every statement compiles for PostgreSQL;
- retrieval is read-only (no add/flush/commit/delete/merge ever called);
- every statement touching a revision-local table carries the requested
  revision id as a bound parameter, and no other revision id ever appears;
- the request-validation paths raise the documented typed errors.

Live semantics (counts, rankings, isolation against real rows) are covered
by tests/overview/test_service_db.py, which needs PostgreSQL.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql import Executable

from app.models import Repository, RepositoryRevision
from app.models.analysis import ANALYSIS_TYPES
from app.overview import service
from app.overview.models import NOT_RUN
from tests.codeintel.test_service_static import scope_violations

REVISION_LOCAL_TABLES = (
    "source_files",
    "symbols",
    "graph_edges",
    "analysis_runs",
    "code_chunks",
    "generated_tests",
    "test_executions",
    "generated_artifacts",
    "security_finding_observations",
)


# Returned by scalar_one_or_none() so the security-findings branch runs too.
SECURITY_RUN_ID = uuid.uuid4()


class _Result:
    def __init__(self, width: int, sql: str) -> None:
        self._width = width
        self._sql = sql

    def one(self) -> tuple[int, ...]:
        return (0,) * self._width

    def all(self) -> list[Any]:
        # One top-level directory row so the per-directory symbol-count
        # query is exercised as well.
        if "split_part" in self._sql and "GROUP BY" in self._sql and self._width == 3:
            return [("src", 1, 10)]
        return []

    def scalar_one(self) -> int | None:
        # max(timestamp) over no rows is NULL, every count is 0.
        return None if "max(" in self._sql else 0

    def scalar_one_or_none(self) -> uuid.UUID | None:
        return SECURITY_RUN_ID if "security_scan" in str(self._sql) else None

    def scalars(self) -> "_Result":
        return self


@dataclass
class _RecordingSession:
    objects: dict[tuple[type, uuid.UUID], Any]
    statements: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def get(self, model: type, ident: uuid.UUID) -> Any:
        return self.objects.get((model, ident))

    def execute(self, statement: Executable) -> _Result:
        compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
        sql = str(compiled)
        params = dict(compiled.params)
        self.statements.append((sql, params))
        width = len(getattr(statement, "selected_columns", ())) or 1
        return _Result(width, sql + repr(params))

    def _forbidden(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("overview retrieval must be read-only")

    add = add_all = flush = commit = delete = merge = _forbidden


def _session(*, current: bool = True) -> tuple[_RecordingSession, Repository, RepositoryRevision]:
    repository = Repository(
        id=uuid.uuid4(), owner="o", name="r", url="https://x/o/r", default_branch="main"
    )
    repository.status = "ready"
    revision = RepositoryRevision(
        id=uuid.uuid4(),
        repository_id=repository.id,
        commit_sha="a" * 40,
        committed_at=datetime(2026, 1, 1, tzinfo=UTC),
        discovered_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    repository.current_revision_id = revision.id if current else None
    db = _RecordingSession(
        objects={
            (Repository, repository.id): repository,
            (RepositoryRevision, revision.id): revision,
        }
    )
    return db, repository, revision


def test_every_statement_compiles_and_retrieval_is_read_only() -> None:
    db, repository, revision = _session()
    overview = service.get_repository_overview(db, repository.id, revision.id)  # type: ignore[arg-type]
    assert overview.revision.revision_id == revision.id
    sql = "\n".join(statement for statement, _ in db.statements)
    assert "security_finding_observations" in sql  # findings branch ran
    assert overview.structure.top_level_directories[0].name == "src"
    checked = [s for s, _ in db.statements if any(f"FROM {t}" in s for t in REVISION_LOCAL_TABLES)]
    assert len(checked) >= 15


def test_every_revision_local_statement_is_bound_to_the_requested_revision() -> None:
    db, repository, revision = _session()
    service.get_repository_overview(db, repository.id, revision.id)  # type: ignore[arg-type]
    for sql, params in db.statements:
        uuid_params = {v for v in params.values() if isinstance(v, uuid.UUID)}
        # No id other than this repository/revision (and the security run
        # whose observations are counted) is ever bound.
        assert uuid_params <= {repository.id, revision.id, SECURITY_RUN_ID}, sql
        # Per-table: every revision-local table carries its own predicates.
        assert (
            scope_violations(sql, params, repository.id, revision.id, REVISION_LOCAL_TABLES) == []
        ), sql


def test_omitted_revision_uses_current_revision() -> None:
    db, repository, revision = _session()
    overview = service.get_repository_overview(db, repository.id)  # type: ignore[arg-type]
    assert overview.revision.revision_id == revision.id
    assert overview.revision.is_current is True


def test_empty_revision_reports_not_run_never_healthy() -> None:
    db, repository, revision = _session()
    overview = service.get_repository_overview(db, repository.id, revision.id)  # type: ignore[arg-type]
    assert [s.analysis_type for s in overview.analysis_statuses] == list(ANALYSIS_TYPES)
    assert {s.status for s in overview.analysis_statuses} == {NOT_RUN}
    assert overview.security.latest_run.status == NOT_RUN
    assert overview.security.finding_count == 0
    assert overview.automation.changed_file_count is None
    assert overview.rag.chunking_run.status == NOT_RUN


def test_unknown_repository_raises() -> None:
    db, _, revision = _session()
    with pytest.raises(service.RepositoryNotFoundError):
        service.get_repository_overview(db, uuid.uuid4(), revision.id)  # type: ignore[arg-type]


def test_unknown_revision_raises() -> None:
    db, repository, _ = _session()
    with pytest.raises(service.RevisionNotFoundError):
        service.get_repository_overview(db, repository.id, uuid.uuid4())  # type: ignore[arg-type]


def test_missing_current_revision_raises_not_found() -> None:
    db, repository, _ = _session(current=False)
    with pytest.raises(service.RevisionNotFoundError):
        service.get_repository_overview(db, repository.id)  # type: ignore[arg-type]


def test_revision_of_another_repository_is_a_conflict() -> None:
    db, repository, _ = _session()
    other_db, _, other_revision = _session()
    db.objects.update(other_db.objects)
    with pytest.raises(service.RevisionConflictError):
        service.get_repository_overview(db, repository.id, other_revision.id)  # type: ignore[arg-type]


def test_automation_config_counts_require_integers() -> None:
    from app.models import AnalysisRun

    run = AnalysisRun(
        id=uuid.uuid4(),
        repository_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        analysis_type="automation_analysis",
        analyzer_name="a",
        analyzer_version="1",
        status="completed",
        config={
            "changed_file_count": 3,
            "changed_symbol_count": "7",
            "impacted_symbol_count": True,
            "limitations": ["x", "y"],
        },
    )
    summary = service._automation(run)
    assert summary.changed_file_count == 3
    assert summary.changed_symbol_count is None
    assert summary.impacted_symbol_count is None
    assert summary.affected_file_count is None
    assert summary.limitation_count == 2
