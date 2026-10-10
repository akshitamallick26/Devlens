"""Hardening Investigation Sessions v1: lifecycle tests (Task 3/17's
Creation/Retrieval/Revision matrices). Requires live PostgreSQL — same
`db_session`/`repository`/`revision` fixtures and explicit-skip behavior as
every other DB-dependent suite in this project.
"""

import uuid

import pytest
from sqlalchemy.orm import Session

from app.investigation import service
from app.models import Repository, RepositoryRevision
from tests.conftest import make_repo_and_revision


def test_create_session_with_explicit_revision(
    db_session: Session, repository: Repository, revision: RepositoryRevision
) -> None:
    snapshot = service.create_session(db_session, repository.id, revision.id, "Payment refactor")
    assert snapshot.repository_id == repository.id
    assert snapshot.started_at_revision_id == revision.id
    assert snapshot.current_revision_id == revision.id
    assert snapshot.title == "Payment refactor"
    assert snapshot.status == "active"
    assert snapshot.message_page.messages == ()
    assert snapshot.context_refs == ()


def test_create_session_without_revision_defaults_to_repository_current_revision(
    db_session: Session, repository: Repository, revision: RepositoryRevision
) -> None:
    repository.current_revision_id = revision.id
    db_session.flush()

    snapshot = service.create_session(db_session, repository.id)
    assert snapshot.started_at_revision_id == revision.id
    assert snapshot.current_revision_id == revision.id


def test_create_session_without_revision_and_no_repository_current_revision_is_null(
    db_session: Session, repository: Repository
) -> None:
    snapshot = service.create_session(db_session, repository.id)
    assert snapshot.started_at_revision_id is None
    assert snapshot.current_revision_id is None


def test_create_session_invalid_repository_raises(db_session: Session) -> None:
    with pytest.raises(service.RepositoryNotFoundError):
        service.create_session(db_session, uuid.uuid4())


def test_create_session_explicit_revision_from_another_repository_is_a_conflict(
    db_session: Session, repository: Repository
) -> None:
    _other_repo, other_revision = make_repo_and_revision(db_session, "other-owner")
    with pytest.raises(service.RevisionConflictError):
        service.create_session(db_session, repository.id, other_revision.id)


def test_create_session_explicit_nonexistent_revision_is_not_found(
    db_session: Session, repository: Repository
) -> None:
    with pytest.raises(service.RevisionNotFoundError):
        service.create_session(db_session, repository.id, uuid.uuid4())


def test_get_session_round_trips(
    db_session: Session, repository: Repository, revision: RepositoryRevision
) -> None:
    created = service.create_session(db_session, repository.id, revision.id, "t")
    fetched = service.get_session(db_session, repository.id, created.id)
    assert fetched.id == created.id
    assert fetched.title == "t"


def test_get_session_nonexistent_raises(db_session: Session, repository: Repository) -> None:
    with pytest.raises(service.SessionNotFoundError):
        service.get_session(db_session, repository.id, uuid.uuid4())


def test_get_session_belonging_to_another_repository_raises(
    db_session: Session, repository: Repository
) -> None:
    other_repo, _other_revision = make_repo_and_revision(db_session, "other-owner")
    created = service.create_session(db_session, other_repo.id)
    with pytest.raises(service.SessionNotFoundError):
        service.get_session(db_session, repository.id, created.id)


def test_rename_session_updates_title_and_bumps_recency(
    db_session: Session, repository: Repository
) -> None:
    created = service.create_session(db_session, repository.id, title="old")
    before = created.last_active_at

    updated = service.update_session(
        db_session, repository.id, created.id, title="new", title_set=True
    )
    assert updated.title == "new"
    assert updated.last_active_at >= before


def test_update_session_with_no_fields_raises_invalid_update(
    db_session: Session, repository: Repository
) -> None:
    created = service.create_session(db_session, repository.id)
    with pytest.raises(service.InvalidUpdateError):
        service.update_session(db_session, repository.id, created.id)


def test_archive_does_not_bump_recency(db_session: Session, repository: Repository) -> None:
    created = service.create_session(db_session, repository.id)
    before = created.last_active_at

    archived = service.update_session(
        db_session, repository.id, created.id, status="archived", status_set=True
    )
    assert archived.status == "archived"
    assert archived.last_active_at == before


def test_reactivate_bumps_recency(db_session: Session, repository: Repository) -> None:
    created = service.create_session(db_session, repository.id)
    service.update_session(
        db_session, repository.id, created.id, status="archived", status_set=True
    )
    before = service.get_session(db_session, repository.id, created.id).last_active_at

    reactivated = service.update_session(
        db_session, repository.id, created.id, status="active", status_set=True
    )
    assert reactivated.status == "active"
    assert reactivated.last_active_at >= before


def test_update_session_invalid_status_value_raises(
    db_session: Session, repository: Repository
) -> None:
    created = service.create_session(db_session, repository.id)
    with pytest.raises(service.InvalidUpdateError):
        service.update_session(
            db_session, repository.id, created.id, status="deleted", status_set=True
        )


def test_explicit_revision_switch_is_deterministic(
    db_session: Session, repository: Repository, revision: RepositoryRevision
) -> None:
    created = service.create_session(db_session, repository.id)
    assert created.current_revision_id is None

    switched = service.update_session(
        db_session, repository.id, created.id, revision_id=revision.id, revision_id_set=True
    )
    assert switched.current_revision_id == revision.id
    assert switched.started_at_revision_id is None  # immutable — never backfilled


def test_switch_revision_to_another_repositorys_revision_is_a_conflict(
    db_session: Session, repository: Repository, revision: RepositoryRevision
) -> None:
    created = service.create_session(db_session, repository.id, revision.id)
    _other_repo, other_revision = make_repo_and_revision(db_session, "other-owner")
    with pytest.raises(service.RevisionConflictError):
        service.update_session(
            db_session,
            repository.id,
            created.id,
            revision_id=other_revision.id,
            revision_id_set=True,
        )


def test_switch_revision_to_nonexistent_revision_is_not_found(
    db_session: Session, repository: Repository
) -> None:
    created = service.create_session(db_session, repository.id)
    with pytest.raises(service.RevisionNotFoundError):
        service.update_session(
            db_session, repository.id, created.id, revision_id=uuid.uuid4(), revision_id_set=True
        )


def test_pinned_session_remains_pinned_after_repository_current_revision_changes(
    db_session: Session, repository: Repository, revision: RepositoryRevision
) -> None:
    """Section 5's core scenario: session created against R1; repository's
    current revision later moves to R2; reopening the session must still
    show R1 — nothing auto-advances it."""
    created = service.create_session(db_session, repository.id, revision.id)

    newer_revision = RepositoryRevision(
        repository_id=repository.id,
        commit_sha="f" * 40,
        parent_sha=revision.commit_sha,
        committed_at=revision.committed_at,
    )
    db_session.add(newer_revision)
    db_session.flush()
    repository.current_revision_id = newer_revision.id
    db_session.flush()

    reopened = service.get_session(db_session, repository.id, created.id)
    assert reopened.current_revision_id == revision.id
    assert reopened.current_revision_id != newer_revision.id
