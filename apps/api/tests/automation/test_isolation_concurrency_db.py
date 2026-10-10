"""Automation Intelligence v1.1: repository/revision isolation, the real
HTTP endpoint, and concurrency of the run upsert, on live PostgreSQL with the
real ingestion -> parsing -> graph pipeline.

Contract: an automation run is identified by (repository, revision, parent
revision or none) within analysis_type 'automation_analysis'; repeated calls
reuse that one row, other analyzer types never collide with it. A parent
must be the current revision's real git parent in the same repository: one
from another repository is "not_found" (409), a root commit with any
requested parent is "mismatch" (409), an unknown revision of the request
repository is 404. Parsing, graph and chunking always run as full revision
rebuilds."""

import threading
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.automation import persistence, service
from app.db.revision_lock import RevisionBusyError
from app.db.session import get_db
from app.main import app
from app.models import (
    AnalysisRun,
    Repository,
    RepositoryRevision,
    SourceBlob,
    SourceFile,
    Symbol,
)
from tests.automation.conftest import ingest_revision, modify_and_commit

AUTOMATION = "automation_analysis"


def _ids(result: object) -> tuple[uuid.UUID, uuid.UUID]:
    return uuid.UUID(result.repository_id), uuid.UUID(result.revision_id)  # type: ignore[attr-defined]


def _runs(
    db: Session, revision_id: uuid.UUID, analysis_type: str = AUTOMATION
) -> list[AnalysisRun]:
    db.expire_all()
    return list(
        db.execute(
            select(AnalysisRun).where(
                AnalysisRun.revision_id == revision_id, AnalysisRun.analysis_type == analysis_type
            )
        ).scalars()
    )


def _two_revisions(db: Session, repo: Path, owner: str) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    first = ingest_revision(db, repo, owner=owner)
    modify_and_commit(repo, "app/math_utils.py", "def add(a, b):\n    return a + b + 0\n", "second")
    second = ingest_revision(db, repo, owner=owner)
    repository_id, parent_id = _ids(first)
    _, current_id = _ids(second)
    return repository_id, parent_id, current_id


# ---- Revision / repository isolation ----


def test_auto_resolution_never_borrows_another_repositorys_parent(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    """Repository B ingested the parent commit; repository A only ingests
    the child. A's parent was never ingested in A, so it is "no_parent",
    never B's revision with the matching commit_sha."""
    ingest_revision(db_session, automation_fixture_repo, owner="iso-b")
    modify_and_commit(
        automation_fixture_repo, "app/math_utils.py", "def add(a, b):\n    return b + a\n", "v2"
    )
    repository_a, current = _ids(ingest_revision(db_session, automation_fixture_repo, "iso-a"))

    result = service.run_automation_analysis(db_session, repository_a, current)

    assert result.parent_revision_id is None
    assert result.summary.files_modified == 0 and result.summary.files_added > 0
    (run,) = _runs(db_session, current)
    assert run.parent_revision_id is None and run.repository_id == repository_a


def test_each_revision_reports_only_its_own_delta(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    repository_id, r1, r2 = _two_revisions(db_session, automation_fixture_repo, "iso-delta")

    r2_result = service.run_automation_analysis(db_session, repository_id, r2)
    r1_result = service.run_automation_analysis(db_session, repository_id, r1)

    assert r2_result.parent_revision_id == r1
    assert [e.path for _, e in r2_result.file_delta.modified] == ["app/math_utils.py"]
    assert r1_result.parent_revision_id is None  # a root commit: nothing to diff
    assert r1_result.file_delta.modified == () and r1_result.file_delta.deleted == ()
    for run in _runs(db_session, r1) + _runs(db_session, r2):
        assert run.repository_id == repository_id
    r2_symbols = set(
        db_session.execute(select(Symbol.id).where(Symbol.revision_id == r2)).scalars()
    )
    # R1 was never parsed before R2's run, so R2's symbols are all "added".
    changed = {s.symbol_id for s in r2_result.symbol_delta.added} | {
        current.symbol_id for _, current in r2_result.symbol_delta.modified
    }
    assert changed and changed <= r2_symbols  # R2's own rows, never R1's


def test_full_revision_rebuild_is_what_every_stage_reports(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    repository_id, _, current = _two_revisions(db_session, automation_fixture_repo, "iso-exec")
    parsing_before = len(_runs(db_session, current, "parsing"))
    result = service.run_automation_analysis(db_session, repository_id, current)
    for plan in (result.parsing_execution, result.graph_execution, result.rag_invalidation):
        assert plan.execution_strategy == "full_revision_rebuild"
    assert len(_runs(db_session, current, "parsing")) == parsing_before + 1


# ---- The real endpoint ----


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient]:
    def _live() -> Generator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _live
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_endpoint_status_codes_and_persisted_state(
    db_session: Session, automation_fixture_repo: Path, client: TestClient
) -> None:
    repository_id, parent, current = _two_revisions(db_session, automation_fixture_repo, "api-a")
    other_repository, other_revision = _ids(
        ingest_revision(db_session, automation_fixture_repo, owner="api-b")
    )
    url = f"/repositories/{repository_id}/automation/analyze"

    def post(body: dict[str, str]) -> tuple[int, dict[str, object]]:
        response = client.post(url, json=body)
        return response.status_code, response.json()

    rejected = [
        (
            {"revision_id": str(current), "parent_revision_id": str(other_revision)},
            409,
            "no revision",
        ),
        (
            {"revision_id": str(current), "parent_revision_id": str(uuid.uuid4())},
            409,
            "no revision",
        ),
        ({"revision_id": str(parent), "parent_revision_id": str(current)}, 409, "root commit"),
        ({"revision_id": str(other_revision)}, 404, "no revision"),
        ({"revision_id": str(uuid.uuid4())}, 404, "no revision"),
        ({"revision_id": "not-a-uuid"}, 422, "invalid uuid"),
    ]
    for body, code, detail in rejected:
        status, payload = post(body)
        assert status == code, body
        assert detail in str(payload["detail"]), body
    assert _runs(db_session, current) == [] and _runs(db_session, parent) == []
    assert _runs(db_session, other_revision) == []

    db_session.add(
        AnalysisRun(
            repository_id=repository_id,
            revision_id=current,
            analysis_type="security_scan",
            analyzer_name="osv-scanner",
            analyzer_version="1",
            status="completed",
        )
    )
    db_session.commit()

    first_status, first = post({"revision_id": str(current), "parent_revision_id": str(parent)})
    second_status, second = post({"revision_id": str(current), "parent_revision_id": str(parent)})
    assert (first_status, second_status) == (200, 200)
    assert first["analysis_run_id"] == second["analysis_run_id"]
    assert first["parent_revision_id"] == str(parent)
    (run,) = _runs(db_session, current)
    assert str(run.id) == first["analysis_run_id"] and run.parent_revision_id == parent
    assert len(_runs(db_session, current, "security_scan")) == 1  # untouched

    auto_status, auto = post({"revision_id": str(current)})
    assert auto_status == 200 and auto["parent_revision_id"] == str(parent)
    assert auto["analysis_run_id"] == first["analysis_run_id"]  # same identity, auto-resolved
    assert _runs(db_session, other_revision) == []
    assert (
        db_session.execute(
            select(func.count())
            .where(AnalysisRun.repository_id == other_repository)
            .where(AnalysisRun.analysis_type == AUTOMATION)
        ).scalar_one()
        == 0
    )


# ---- Concurrency (committed rows, cleaned up) ----


@pytest.fixture
def committed(
    engine: Engine, automation_fixture_repo: Path
) -> Generator[tuple[sessionmaker[Session], uuid.UUID, uuid.UUID, uuid.UUID]]:
    factory = sessionmaker(bind=engine)
    owner = f"conc-{uuid.uuid4().hex[:8]}"
    with factory() as setup:
        repository_id, parent, current = _two_revisions(setup, automation_fixture_repo, owner)
    try:
        yield factory, repository_id, parent, current
    finally:
        with factory() as cleanup:
            hashes = set(
                cleanup.execute(
                    select(SourceFile.content_hash).where(SourceFile.repository_id == repository_id)
                ).scalars()
            )
            cleanup.delete(cleanup.get(Repository, repository_id))
            cleanup.flush()
            still_used = set(
                cleanup.execute(
                    select(SourceFile.content_hash).where(SourceFile.content_hash.in_(hashes))
                ).scalars()
            )
            cleanup.execute(
                SourceBlob.__table__.delete().where(
                    SourceBlob.content_hash.in_(hashes - still_used)
                )
            )
            cleanup.commit()


def _in_threads(count: int, work: object) -> list[object]:
    barrier = threading.Barrier(count)
    results: list[object] = []
    lock = threading.Lock()

    def run(index: int) -> None:
        barrier.wait()
        try:
            outcome: object = work(index)  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001
            outcome = exc
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_concurrent_upserts_create_one_run_per_identity_and_never_touch_other_types(
    committed: tuple[sessionmaker[Session], uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    factory, repository_id, parent, current = committed

    def work(index: int) -> object:
        with factory() as db:
            kind = index % 3
            if kind == 2:  # an unrelated analyzer type on the same revision
                db.add(
                    AnalysisRun(
                        repository_id=repository_id,
                        revision_id=current,
                        analysis_type="parsing",
                        analyzer_name="concurrent-parser",
                        analyzer_version="1",
                        status="completed",
                    )
                )
                db.commit()
                return ("parsing", None)
            parent_id = parent if kind == 1 else None
            run = persistence.start_or_reuse_run(db, repository_id, current, parent_id)
            return ("with_parent" if kind == 1 else "root", run.id)

    results = _in_threads(18, work)
    assert not [r for r in results if isinstance(r, Exception)], results
    ids_by_kind: dict[str, set[object]] = {}
    for kind, run_id in results:  # type: ignore[misc]
        ids_by_kind.setdefault(kind, set()).add(run_id)
    assert len(ids_by_kind["root"]) == 1 and len(ids_by_kind["with_parent"]) == 1
    assert ids_by_kind["root"] != ids_by_kind["with_parent"]

    with factory() as check:
        parents = sorted(str(r.parent_revision_id) for r in _runs(check, current))
        parsing = [
            r.id for r in _runs(check, current, "parsing") if r.analyzer_name == "concurrent-parser"
        ]
    assert parents == sorted([str(parent), "None"])
    assert len(parsing) == 6


def test_concurrent_identical_automation_calls_yield_one_run_and_one_symbol_set(
    committed: tuple[sessionmaker[Session], uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    factory, repository_id, parent, current = committed

    def work(_: int) -> object:
        with factory() as db:
            return service.run_automation_analysis(
                db, repository_id, current, parent
            ).analysis_run_id

    results = _in_threads(4, work)
    run_ids = {r for r in results if isinstance(r, uuid.UUID)}
    errors = [r for r in results if not isinstance(r, uuid.UUID)]
    assert len(run_ids) == 1, results  # whoever ran, it was the same logical run
    assert all(isinstance(e, RevisionBusyError) for e in errors), errors

    with factory() as check:
        (run,) = _runs(check, current)
        assert run.status == "completed" and run.id in run_ids
        symbols = (
            check.execute(select(Symbol.qualified_name).where(Symbol.revision_id == current))
            .scalars()
            .all()
        )
    assert len(symbols) == len(set(symbols))  # no duplicated symbol set


# ---- Deleting a parent revision (migration 0011) ----


def test_deleting_the_parent_revision_removes_only_the_runs_computed_against_it(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    """A revision can hold both identities: a no-parent run from before its
    parent was ingested, and a with-parent run after. Deleting the parent
    must not rewrite the with-parent run into the (taken) no-parent
    identity: it is removed, the genuine no-parent run is untouched."""
    repository_id, parent, current = _two_revisions(db_session, automation_fixture_repo, "del-p")
    root_run = persistence.start_or_reuse_run(db_session, repository_id, current, None).id
    parent_run = persistence.start_or_reuse_run(db_session, repository_id, current, parent).id

    db_session.delete(db_session.get(RepositoryRevision, parent))
    db_session.commit()

    remaining = _runs(db_session, current)
    assert [(r.id, r.parent_revision_id) for r in remaining] == [(root_run, None)]
    assert db_session.get(AnalysisRun, parent_run) is None

    db_session.delete(db_session.get(Repository, repository_id))
    db_session.commit()
    assert (
        db_session.execute(
            select(func.count()).where(AnalysisRun.repository_id == repository_id)
        ).scalar_one()
        == 0
    )
