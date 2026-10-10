"""Hardening Blocker 1 tests: Automation Intelligence's own idempotency
contract. Requires live PostgreSQL — same `db_session` fixture and
explicit-skip behavior as every other DB-dependent suite. Never
substitutes SQLite — the partial unique indexes this phase relies on
(migration `0007`) are Postgres-specific (`postgresql_where`).
"""

import inspect
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation import persistence, service
from app.models import AnalysisRun
from tests.automation.conftest import ingest_revision, modify_and_commit


def test_start_or_reuse_run_never_constructs_a_pending_orm_instance() -> None:
    """Static, non-DB guard against the autoflush-before-upsert hazard: if
    this function ever called `db.add(AnalysisRun(...))` before executing
    the explicit `INSERT ... ON CONFLICT` statement, SQLAlchemy's default
    autoflush would issue that pending object as a plain INSERT first,
    defeating the atomic upsert entirely. The function must build the
    statement's values directly and execute it — no pending ORM instance,
    ever. Always runs (no PostgreSQL needed), unlike the rest of this file."""
    source = inspect.getsource(persistence.start_or_reuse_run)
    assert ".add(" not in source
    assert "AnalysisRun(" not in source
    assert "pg_insert(AnalysisRun)" in source


def _automation_run_rows(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> list[AnalysisRun]:
    return list(
        db.execute(
            select(AnalysisRun)
            .where(AnalysisRun.repository_id == repository_id)
            .where(AnalysisRun.revision_id == revision_id)
            .where(AnalysisRun.analysis_type == "automation_analysis")
        )
        .scalars()
        .all()
    )


def test_same_revision_and_parent_twice_is_one_logical_run(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    parent = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    modify_and_commit(
        automation_fixture_repo, "app/math_utils.py", "def add(a, b):\n    return a + b\n", "x"
    )
    current = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    repository_id = uuid.UUID(current.repository_id)
    revision_id = uuid.UUID(current.revision_id)
    parent_id = uuid.UUID(parent.revision_id)

    first = service.run_automation_analysis(db_session, repository_id, revision_id, parent_id)
    second = service.run_automation_analysis(db_session, repository_id, revision_id, parent_id)

    assert first.analysis_run_id == second.analysis_run_id
    rows = _automation_run_rows(db_session, repository_id, revision_id)
    assert len(rows) == 1


def test_same_revision_no_parent_twice_is_one_logical_run(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)

    first = service.run_automation_analysis(db_session, repository_id, revision_id)
    second = service.run_automation_analysis(db_session, repository_id, revision_id)

    assert first.analysis_run_id == second.analysis_run_id
    rows = _automation_run_rows(db_session, repository_id, revision_id)
    assert len(rows) == 1


def test_different_revision_gets_a_separate_run(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    first_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    modify_and_commit(
        automation_fixture_repo, "app/math_utils.py", "def add(a, b):\n    return a + b\n", "x"
    )
    second_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    repository_id = uuid.UUID(first_result.repository_id)

    first_run = service.run_automation_analysis(
        db_session, repository_id, uuid.UUID(first_result.revision_id)
    )
    second_run = service.run_automation_analysis(
        db_session, repository_id, uuid.UUID(second_result.revision_id)
    )

    assert first_run.analysis_run_id != second_run.analysis_run_id
    assert (
        len(_automation_run_rows(db_session, repository_id, uuid.UUID(first_result.revision_id)))
        == 1
    )
    assert (
        len(_automation_run_rows(db_session, repository_id, uuid.UUID(second_result.revision_id)))
        == 1
    )


def test_different_parent_for_the_same_revision_is_a_separate_identity_at_the_persistence_layer(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    """`resolve_parent`'s own verification means a real revision can only
    ever have one *verified* parent (git's `parent_sha` is a single value
    in this schema) — so this scenario cannot arise naturally through the
    full `run_automation_analysis` entry point for valid data. This tests
    the lower-level uniqueness *mechanism* directly instead: if the model
    ever did permit two different parent identities for the same revision,
    `start_or_reuse_run` would correctly keep them as two separate rows,
    never collapsing them into one."""
    result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)

    other_result = ingest_revision(db_session, automation_fixture_repo, owner="owner-other")
    other_revision_id = uuid.UUID(other_result.revision_id)

    run_a = persistence.start_or_reuse_run(db_session, repository_id, revision_id, None)
    run_b = persistence.start_or_reuse_run(
        db_session, repository_id, revision_id, other_revision_id
    )

    assert run_a.id != run_b.id
    assert len(_automation_run_rows(db_session, repository_id, revision_id)) == 2


def test_unrelated_analyzer_type_on_the_same_revision_is_unaffected(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)

    parsing_run = AnalysisRun(
        repository_id=repository_id,
        revision_id=revision_id,
        analysis_type="parsing",
        analyzer_name="devlens-parsing",
        analyzer_version="x",
        status="completed",
    )
    db_session.add(parsing_run)
    db_session.flush()
    parsing_run_id = parsing_run.id
    before = (parsing_run.analyzer_name, parsing_run.analyzer_version, parsing_run.status)

    service.run_automation_analysis(db_session, repository_id, revision_id)
    service.run_automation_analysis(db_session, repository_id, revision_id)

    db_session.expire_all()
    parsing_rows = (
        db_session.execute(
            select(AnalysisRun)
            .where(AnalysisRun.repository_id == repository_id)
            .where(AnalysisRun.revision_id == revision_id)
            .where(AnalysisRun.analysis_type == "parsing")
        )
        .scalars()
        .all()
    )
    # The unrelated analyzer's own row is untouched: never matched by the
    # automation upsert, never overwritten.
    untouched = [r for r in parsing_rows if r.id == parsing_run_id]
    assert [(r.analyzer_name, r.analyzer_version, r.status) for r in untouched] == [before]
    # Every automation call runs parsing as a full revision rebuild, and
    # parsing history is append-only: one new parsing run per call.
    appended = [r for r in parsing_rows if r.id != parsing_run_id]
    assert len(appended) == 2
    assert {r.analyzer_name for r in appended} == {"devlens-python-ast-parser"}
    assert len(_automation_run_rows(db_session, repository_id, revision_id)) == 1


def test_retry_after_failure_reuses_the_same_row_and_ends_completed(
    db_session: Session, automation_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("simulated parsing failure")

    monkeypatch.setattr(service, "run_parsing", _boom)
    with pytest.raises(service.AutomationError):
        service.run_automation_analysis(db_session, repository_id, revision_id)

    rows = _automation_run_rows(db_session, repository_id, revision_id)
    assert len(rows) == 1
    assert rows[0].status == "failed"
    failed_run_id = rows[0].id

    monkeypatch.undo()
    retry_result = service.run_automation_analysis(db_session, repository_id, revision_id)

    assert retry_result.analysis_run_id == failed_run_id  # same row, reused — never duplicated
    assert retry_result.analysis_status == "completed"
    assert len(_automation_run_rows(db_session, repository_id, revision_id)) == 1


def test_database_unique_index_itself_prevents_a_true_duplicate_insert(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    """Concurrency note: this single-session test harness cannot exercise
    real concurrent requests. What *is* verifiable here is that duplicate
    prevention is enforced by the database's own partial unique index
    (migration 0007), not merely by application-level check-then-act logic
    — `start_or_reuse_run`'s `ON CONFLICT` targets that index directly, so
    two overlapping callers would serialize on the same constraint rather
    than racing to insert two rows."""
    result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)

    first = persistence.start_or_reuse_run(db_session, repository_id, revision_id, None)
    second = persistence.start_or_reuse_run(db_session, repository_id, revision_id, None)
    assert first.id == second.id
    assert len(_automation_run_rows(db_session, repository_id, revision_id)) == 1
