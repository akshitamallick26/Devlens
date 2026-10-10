"""Investigation Sessions v1.3: context-reference repository/revision
isolation on live PostgreSQL, with every row created through valid schema
relationships (no random foreign keys).

Contract (docs/architecture/investigation-sessions.md sections 3 and 5):
a session belongs to exactly one repository. Context refs are historical
breadcrumbs, so a file/symbol/analysis run from any revision of the
session's own repository is accepted and stored exactly as given; anything
from another repository is rejected and nothing is persisted. Findings are
repository-scoped. graph_focus camera state is opaque client data. Switching
the session's current revision never rewrites existing refs, and a revision
from another repository is rejected."""

import threading
import uuid
from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_db
from app.investigation import service
from app.main import app
from app.models import (
    AnalysisRun,
    InvestigationSession,
    Repository,
    RepositoryRevision,
    SecurityFinding,
    SessionContextRef,
    SourceBlob,
    SourceFile,
    Symbol,
)


@dataclass(frozen=True)
class _Rev:
    revision_id: uuid.UUID
    file_id: uuid.UUID
    symbol_id: uuid.UUID
    run_id: uuid.UUID


@dataclass(frozen=True)
class _Repo:
    repository_id: uuid.UUID
    revs: tuple[_Rev, ...]
    finding_id: uuid.UUID
    blob_hashes: tuple[str, ...]


def _build(db: Session, owner: str, shas: list[str]) -> _Repo:
    repo = Repository(owner=owner, name="r", url=f"https://x/{owner}/r", default_branch="main")
    db.add(repo)
    db.flush()
    revs, hashes = [], []
    for index, sha in enumerate(shas):
        rev = RepositoryRevision(
            repository_id=repo.id, commit_sha=sha, committed_at=datetime.now(UTC)
        )
        db.add(rev)
        db.flush()
        content = f"def f():\n    return {index}\n"
        blob = SourceBlob(
            content_hash=uuid.uuid4().hex, inline_content=content, size_bytes=len(content)
        )
        db.add(blob)
        db.flush()
        source_file = SourceFile(
            repository_id=repo.id,
            revision_id=rev.id,
            path="src/mod.py",
            content_hash=blob.content_hash,
            size_bytes=len(content),
        )
        db.add(source_file)
        db.flush()
        symbol = Symbol(
            repository_id=repo.id,
            revision_id=rev.id,
            file_id=source_file.id,
            kind="function",
            name="f",
            qualified_name="src.mod.f",
            start_line=1,
            end_line=2,
            analyzer="tree-sitter-python@0.21",
        )
        run = AnalysisRun(
            repository_id=repo.id,
            revision_id=rev.id,
            analysis_type="parsing",
            analyzer_name="tree-sitter-python",
            analyzer_version="0.21.0",
        )
        db.add_all([symbol, run])
        db.flush()
        revs.append(_Rev(rev.id, source_file.id, symbol.id, run.id))
        hashes.append(blob.content_hash)
    finding = SecurityFinding(
        repository_id=repo.id,
        analyzer="osv-scanner",
        rule_id="GHSA-1",
        fingerprint=f"fp-{owner}",
        severity="high",
        title="t",
        message="m",
    )
    db.add(finding)
    db.flush()
    return _Repo(repo.id, tuple(revs), finding.id, tuple(hashes))


@pytest.fixture
def repo_a(db_session: Session) -> _Repo:
    return _build(db_session, "iso-a", ["a" * 40, "b" * 40])


@pytest.fixture
def repo_b(db_session: Session) -> _Repo:
    return _build(db_session, "iso-b", ["c" * 40])


def _typed(rev: _Rev, finding_id: uuid.UUID) -> list[tuple[str, str, uuid.UUID]]:
    return [
        ("selected_file", "file_id", rev.file_id),
        ("selected_symbol", "symbol_id", rev.symbol_id),
        ("analysis_reference", "analysis_run_id", rev.run_id),
        ("finding_reference", "finding_id", finding_id),
    ]


def _state(db: Session, session_id: uuid.UUID) -> tuple[Any, ...]:
    db.expire_all()
    row = db.get(InvestigationSession, session_id)
    assert row is not None
    refs = db.execute(
        select(
            SessionContextRef.id,
            SessionContextRef.ref_type,
            SessionContextRef.file_id,
            SessionContextRef.symbol_id,
            SessionContextRef.analysis_run_id,
            SessionContextRef.finding_id,
        )
        .where(SessionContextRef.session_id == session_id)
        .order_by(SessionContextRef.id)
    ).all()
    return (
        row.repository_id,
        row.status,
        row.started_at_revision_id,
        row.current_revision_id,
        row.last_active_at,
        tuple(tuple(r) for r in refs),
    )


# ---- Cross-revision: historical breadcrumbs are accepted exactly ----


def test_refs_from_every_revision_of_the_own_repository_are_stored_exactly(
    db_session: Session, repo_a: _Repo
) -> None:
    a1, a2 = repo_a.revs
    session_id = service.create_session(db_session, repo_a.repository_id, a2.revision_id).id
    expected = set()
    for rev in (a1, a2):
        for ref_type, column, entity_id in _typed(rev, repo_a.finding_id):
            view = service.add_context_ref(
                db_session, repo_a.repository_id, session_id, ref_type, **{column: entity_id}
            )
            assert getattr(view, column) == entity_id
            expected.add((ref_type, entity_id))
    stored = {
        (r.ref_type, r.file_id or r.symbol_id or r.analysis_run_id or r.finding_id)
        for r in service.get_session(db_session, repo_a.repository_id, session_id).context_refs
    }
    assert stored == expected  # the finding is repository-scoped: one row, not two
    a1_symbol = db_session.get(Symbol, a1.symbol_id)
    assert a1_symbol is not None and a1_symbol.revision_id == a1.revision_id


def test_switching_revision_archiving_and_reactivating_never_rewrite_refs(
    db_session: Session, repo_a: _Repo, repo_b: _Repo
) -> None:
    a1, a2 = repo_a.revs
    repository_id = repo_a.repository_id
    session_id = service.create_session(db_session, repository_id, a1.revision_id).id
    service.add_context_ref(
        db_session, repository_id, session_id, "selected_symbol", symbol_id=a1.symbol_id
    )
    service.update_session(
        db_session, repository_id, session_id, revision_id=a2.revision_id, revision_id_set=True
    )
    service.add_context_ref(
        db_session, repository_id, session_id, "selected_symbol", symbol_id=a2.symbol_id
    )
    before = _state(db_session, session_id)
    assert before[2:4] == (a1.revision_id, a2.revision_id)  # started_at, current
    assert {r[3] for r in before[5]} == {a1.symbol_id, a2.symbol_id}

    with pytest.raises(service.RevisionConflictError):
        service.update_session(
            db_session,
            repository_id,
            session_id,
            revision_id=repo_b.revs[0].revision_id,
            revision_id_set=True,
        )
    assert _state(db_session, session_id) == before

    service.update_session(
        db_session, repository_id, session_id, status="archived", status_set=True
    )
    service.update_session(db_session, repository_id, session_id, status="active", status_set=True)
    assert _state(db_session, session_id)[5] == before[5]


# ---- Cross-repository: rejected, nothing persisted, nothing bumped ----


def test_every_typed_ref_from_another_repository_is_rejected_without_side_effects(
    db_session: Session, repo_a: _Repo, repo_b: _Repo
) -> None:
    session_id = service.create_session(
        db_session, repo_a.repository_id, repo_a.revs[0].revision_id
    ).id
    for ref_type, column, entity_id in _typed(repo_b.revs[0], repo_b.finding_id):
        before = _state(db_session, session_id)
        with pytest.raises(
            service.CrossRepositoryReferenceError, match="belongs to a different repository"
        ):
            service.add_context_ref(
                db_session, repo_a.repository_id, session_id, ref_type, **{column: entity_id}
            )
        assert _state(db_session, session_id) == before, ref_type


def test_a_session_is_only_reachable_through_its_own_repository(
    db_session: Session, repo_a: _Repo, repo_b: _Repo
) -> None:
    a1 = repo_a.revs[0]
    session_id = service.create_session(db_session, repo_a.repository_id, a1.revision_id).id
    ref = service.add_context_ref(
        db_session, repo_a.repository_id, session_id, "selected_file", file_id=a1.file_id
    )
    other_session = service.create_session(db_session, repo_b.repository_id).id
    before = _state(db_session, session_id)
    with pytest.raises(service.SessionNotFoundError):
        service.add_context_ref(
            db_session,
            repo_b.repository_id,
            session_id,
            "selected_file",
            file_id=repo_b.revs[0].file_id,
        )
    with pytest.raises(service.SessionNotFoundError):
        service.remove_context_ref(db_session, repo_b.repository_id, session_id, ref.id)
    with pytest.raises(service.ContextRefNotFoundError):
        service.remove_context_ref(db_session, repo_b.repository_id, other_session, ref.id)
    with pytest.raises(service.RevisionConflictError):
        service.create_session(db_session, repo_a.repository_id, repo_b.revs[0].revision_id)
    assert _state(db_session, session_id) == before


def test_a_foreign_id_beside_a_valid_one_is_never_persisted(
    db_session: Session, repo_a: _Repo, repo_b: _Repo
) -> None:
    """Only the id matching ref_type is validated by the service; any other
    typed id is stopped by the one-hot CHECK constraint (currently surfacing
    as IntegrityError). Either way, nothing from repository B is stored."""
    a1, b1 = repo_a.revs[0], repo_b.revs[0]
    session_id = service.create_session(db_session, repo_a.repository_id, a1.revision_id).id
    attempts: list[tuple[str, dict[str, uuid.UUID]]] = [
        ("selected_file", {"file_id": a1.file_id, "symbol_id": b1.symbol_id}),
        ("selected_symbol", {"symbol_id": a1.symbol_id, "finding_id": repo_b.finding_id}),
        ("analysis_reference", {"analysis_run_id": a1.run_id, "file_id": b1.file_id}),
        ("finding_reference", {"finding_id": repo_a.finding_id, "analysis_run_id": b1.run_id}),
    ]
    for ref_type, ids in attempts:
        before = _state(db_session, session_id)
        with pytest.raises((service.InvestigationError, IntegrityError)):
            service.add_context_ref(db_session, repo_a.repository_id, session_id, ref_type, **ids)
        db_session.rollback()
        assert _state(db_session, session_id) == before, ref_type

    focus = service.add_context_ref(
        db_session,
        repo_a.repository_id,
        session_id,
        "graph_focus",
        graph_camera_state={"focus_symbol_id": str(b1.symbol_id)},  # opaque, never resolved here
        file_id=b1.file_id,
        symbol_id=b1.symbol_id,
    )
    stored = db_session.get(SessionContextRef, focus.id)
    assert stored is not None and (stored.file_id, stored.symbol_id) == (None, None)


# ---- HTTP layer ----


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient]:
    def _live() -> Generator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _live
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_http_layer_cannot_bypass_repository_or_revision_ownership(
    db_session: Session, repo_a: _Repo, repo_b: _Repo, client: TestClient
) -> None:
    a1, a2 = repo_a.revs
    b1 = repo_b.revs[0]
    base = f"/repositories/{repo_a.repository_id}/sessions"
    assert client.post(base, json={"revision_id": str(b1.revision_id)}).status_code == 409
    created = client.post(base, json={"revision_id": str(a1.revision_id)})
    assert created.status_code == 200
    session_url = f"{base}/{created.json()['id']}"
    first = client.post(
        f"{session_url}/context",
        json={"ref_type": "selected_symbol", "symbol_id": str(a1.symbol_id)},
    )
    assert first.status_code == 200

    snapshot = client.get(session_url).json()
    for ref_type, column, entity_id in _typed(b1, repo_b.finding_id):
        response = client.post(
            f"{session_url}/context", json={"ref_type": ref_type, column: str(entity_id)}
        )
        assert response.status_code == 409, ref_type
        assert client.get(session_url).json() == snapshot

    foreign = f"/repositories/{repo_b.repository_id}/sessions/{created.json()['id']}"
    assert client.get(foreign).status_code == 404
    assert (
        client.post(
            f"{foreign}/context",
            json={"ref_type": "selected_symbol", "symbol_id": str(b1.symbol_id)},
        ).status_code
        == 404
    )
    assert client.delete(f"{foreign}/context/{first.json()['id']}").status_code == 404
    assert client.patch(session_url, json={"revision_id": str(b1.revision_id)}).status_code == 409
    assert client.get(session_url).json() == snapshot

    switched = client.patch(session_url, json={"revision_id": str(a2.revision_id)}).json()
    assert (switched["started_at_revision_id"], switched["current_revision_id"]) == (
        str(a1.revision_id),
        str(a2.revision_id),
    )
    client.post(
        f"{session_url}/context",
        json={"ref_type": "selected_symbol", "symbol_id": str(a2.symbol_id)},
    )
    refs = client.get(session_url).json()["context_refs"]
    assert {r["symbol_id"] for r in refs} == {str(a1.symbol_id), str(a2.symbol_id)}


# ---- Concurrency (committed rows, cleaned up) ----


def test_concurrent_adds_dedupe_and_never_admit_a_foreign_ref(engine: Engine) -> None:
    factory = sessionmaker(bind=engine)
    suffix = uuid.uuid4().hex[:8]
    with factory() as setup:
        a = _build(setup, f"conc-a-{suffix}", ["d" * 40])
        b = _build(setup, f"conc-b-{suffix}", ["e" * 40])
        setup.commit()
        session_id = service.create_session(setup, a.repository_id).id
    try:
        barrier = threading.Barrier(12)
        results: list[tuple[uuid.UUID, str, object]] = []
        lock = threading.Lock()

        def add(symbol_id: uuid.UUID) -> None:
            with factory() as db:
                barrier.wait()
                try:
                    view = service.add_context_ref(
                        db, a.repository_id, session_id, "selected_symbol", symbol_id=symbol_id
                    )
                    outcome: tuple[uuid.UUID, str, object] = (symbol_id, "ok", view.id)
                except Exception as exc:  # noqa: BLE001
                    outcome = (symbol_id, "error", type(exc).__name__)
                with lock:
                    results.append(outcome)

        own, foreign = a.revs[0].symbol_id, b.revs[0].symbol_id
        threads = [
            threading.Thread(target=add, args=(own if i % 2 else foreign,)) for i in range(12)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        own_results = [r for r in results if r[0] == own]
        assert {r[1] for r in own_results} == {"ok"} and len({r[2] for r in own_results}) == 1
        assert {(r[1], r[2]) for r in results if r[0] == foreign} == {
            ("error", "CrossRepositoryReferenceError")
        }
        with factory() as check:
            stored = check.execute(
                select(SessionContextRef.symbol_id).where(
                    SessionContextRef.session_id == session_id
                )
            ).scalars()
            assert list(stored) == [own]
    finally:
        with factory() as cleanup:
            for repo in (a, b):
                cleanup.delete(cleanup.get(Repository, repo.repository_id))
            cleanup.flush()
            cleanup.execute(
                SourceBlob.__table__.delete().where(
                    SourceBlob.content_hash.in_(a.blob_hashes + b.blob_hashes)
                )
            )
            cleanup.commit()
            leftover = cleanup.execute(
                select(func.count()).where(Repository.owner.like(f"conc-%-{suffix}"))
            ).scalar_one()
            assert leftover == 0
