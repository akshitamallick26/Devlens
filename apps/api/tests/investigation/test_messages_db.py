"""Hardening Investigation Sessions v1: message ordering, pagination, and
recency tests (Task 6/9/12/17). Requires live PostgreSQL — same
`db_session`/`repository` fixtures and explicit-skip behavior as every
other DB-dependent suite in this project.
"""

import uuid

import pytest
from sqlalchemy.orm import Session

from app.investigation import service
from app.models import Repository


def test_append_messages_assigns_increasing_sequence_numbers(
    db_session: Session, repository: Repository
) -> None:
    created = service.create_session(db_session, repository.id)
    m1 = service.append_message(db_session, repository.id, created.id, "user", "hello")
    m2 = service.append_message(db_session, repository.id, created.id, "assistant", "hi")
    m3 = service.append_message(db_session, repository.id, created.id, "system", "note")

    assert (m1.sequence_number, m2.sequence_number, m3.sequence_number) == (1, 2, 3)


def test_invalid_role_is_rejected(db_session: Session, repository: Repository) -> None:
    created = service.create_session(db_session, repository.id)
    with pytest.raises(service.InvalidRoleError):
        service.append_message(db_session, repository.id, created.id, "narrator", "hello")


def test_append_to_nonexistent_session_raises(db_session: Session, repository: Repository) -> None:
    with pytest.raises(service.SessionNotFoundError):
        service.append_message(db_session, repository.id, uuid.uuid4(), "user", "hello")


def test_append_bumps_recency(db_session: Session, repository: Repository) -> None:
    created = service.create_session(db_session, repository.id)
    before = created.last_active_at
    service.append_message(db_session, repository.id, created.id, "user", "hello")
    after = service.get_session(db_session, repository.id, created.id).last_active_at
    assert after >= before


def test_message_page_is_returned_in_ascending_sequence_order(
    db_session: Session, repository: Repository
) -> None:
    created = service.create_session(db_session, repository.id)
    for i in range(5):
        service.append_message(db_session, repository.id, created.id, "user", f"msg-{i}")

    snapshot = service.get_session(db_session, repository.id, created.id, message_limit=50)
    sequence_numbers = [m.sequence_number for m in snapshot.message_page.messages]
    assert sequence_numbers == sorted(sequence_numbers)
    assert [m.content for m in snapshot.message_page.messages] == [f"msg-{i}" for i in range(5)]


def test_bounded_pagination_covers_every_message_exactly_once(
    db_session: Session, repository: Repository
) -> None:
    created = service.create_session(db_session, repository.id)
    total = 7
    for i in range(total):
        service.append_message(db_session, repository.id, created.id, "user", f"msg-{i}")

    collected: list[int] = []
    before_sequence: int | None = None
    page_size = 3
    for _ in range(total):  # generous upper bound on iterations
        page = service.get_session(
            db_session,
            repository.id,
            created.id,
            message_limit=page_size,
            message_before_sequence=before_sequence,
        ).message_page
        collected = [m.sequence_number for m in page.messages] + collected
        if not page.has_more:
            break
        before_sequence = page.next_before_sequence

    assert collected == list(range(1, total + 1))


def test_message_page_defaults_to_most_recent_window(
    db_session: Session, repository: Repository
) -> None:
    created = service.create_session(db_session, repository.id)
    for i in range(5):
        service.append_message(db_session, repository.id, created.id, "user", f"msg-{i}")

    page = service.get_session(db_session, repository.id, created.id, message_limit=2).message_page
    assert [m.content for m in page.messages] == ["msg-3", "msg-4"]
    assert page.has_more is True
    assert page.next_before_sequence == 4
