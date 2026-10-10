"""Hardening Investigation Sessions v1: recents ordering/recency tests
(Task 9/17). Requires live PostgreSQL — same `db_session`/`repository`
fixtures and explicit-skip behavior as every other DB-dependent suite in
this project.
"""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.investigation import service
from app.models import InvestigationSession, Repository


def test_recents_ordered_by_last_active_at_descending(
    db_session: Session, repository: Repository
) -> None:
    older = service.create_session(db_session, repository.id, title="older")
    newer = service.create_session(db_session, repository.id, title="newer")
    service.append_message(db_session, repository.id, newer.id, "user", "bump newer")

    page = service.list_recent_sessions(db_session, repository.id)
    ids_in_order = [s.id for s in page.sessions]
    assert ids_in_order.index(newer.id) < ids_in_order.index(older.id)


def test_recents_tie_break_is_deterministic_by_id(
    db_session: Session, repository: Repository
) -> None:
    a = service.create_session(db_session, repository.id, title="a")
    b = service.create_session(db_session, repository.id, title="b")

    # Force an exact timestamp tie — a real race could land here too.
    tied_timestamp = datetime.now(UTC)
    row_a = db_session.get(InvestigationSession, a.id)
    row_b = db_session.get(InvestigationSession, b.id)
    assert row_a is not None and row_b is not None
    row_a.last_active_at = tied_timestamp
    row_b.last_active_at = tied_timestamp
    db_session.flush()

    page = service.list_recent_sessions(db_session, repository.id)
    tied_ids = [s.id for s in page.sessions if s.id in (a.id, b.id)]
    expected_order = sorted([a.id, b.id], reverse=True)
    assert tied_ids == expected_order


def test_reading_a_session_does_not_mutate_recency(
    db_session: Session, repository: Repository
) -> None:
    created = service.create_session(db_session, repository.id)
    before = created.last_active_at
    for _ in range(3):
        service.get_session(db_session, repository.id, created.id)
    after = service.get_session(db_session, repository.id, created.id).last_active_at
    assert after == before


def test_listing_sessions_does_not_mutate_recency(
    db_session: Session, repository: Repository
) -> None:
    created = service.create_session(db_session, repository.id)
    before = created.last_active_at
    service.list_recent_sessions(db_session, repository.id)
    after = service.get_session(db_session, repository.id, created.id).last_active_at
    assert after == before


def test_message_count_is_aggregated_correctly_per_session(
    db_session: Session, repository: Repository
) -> None:
    empty_session = service.create_session(db_session, repository.id)
    busy_session = service.create_session(db_session, repository.id)
    for i in range(4):
        service.append_message(db_session, repository.id, busy_session.id, "user", f"m{i}")

    page = service.list_recent_sessions(db_session, repository.id)
    counts = {s.id: s.message_count for s in page.sessions}
    assert counts[empty_session.id] == 0
    assert counts[busy_session.id] == 4


def test_recents_pagination_limit_and_offset_and_total_count(
    db_session: Session, repository: Repository
) -> None:
    created_ids = []
    for i in range(5):
        s = service.create_session(db_session, repository.id, title=f"s{i}")
        created_ids.append(s.id)

    first_page = service.list_recent_sessions(db_session, repository.id, limit=2, offset=0)
    second_page = service.list_recent_sessions(db_session, repository.id, limit=2, offset=2)

    assert first_page.total_count == 5
    assert second_page.total_count == 5
    assert len(first_page.sessions) == 2
    assert len(second_page.sessions) == 2
    first_ids = {s.id for s in first_page.sessions}
    second_ids = {s.id for s in second_page.sessions}
    assert first_ids.isdisjoint(second_ids)
