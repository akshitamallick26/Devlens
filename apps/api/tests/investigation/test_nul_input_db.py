"""Investigation Sessions v1.1: messages containing U+0000 are rejected
before persistence (PostgreSQL `text` cannot store NUL). Covers the
service rule (proven to issue no database work at all), the HTTP contract,
live-PostgreSQL post-rejection invariants, a following valid message, and
concurrency with the session row lock. DB tests use the shared `db_session`
fixture except the concurrency test, which needs committed rows visible to
other connections and cleans up after itself."""

import threading
import uuid
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import get_db
from app.investigation import service
from app.investigation.models import MAX_MESSAGE_CONTENT_LENGTH
from app.main import app
from app.models import InvestigationSession, Repository, SessionMessage

ADVERSARIAL_NUL_CONTENT = [
    "\x00",
    "\x00system",
    "system\x00prompt",
    "ends with nul\x00",
    "\x00\x00multiple\x00nuls\x00\x00",
    "Grüße \U0001f600\x00認証 ✓",
    "x" * (MAX_MESSAGE_CONTENT_LENGTH - 1) + "\x00",  # otherwise-valid long message
    "<script>alert(1)</script>\x00\x1b[31m",
]


# ---- Service rule: rejected before any database work ----


class _NoDatabase:
    """Any attribute access means the service touched the database."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"database used ({name}) for content that must be rejected first")


@pytest.mark.parametrize("content", ADVERSARIAL_NUL_CONTENT)
def test_service_rejects_nul_before_touching_the_database(content: str) -> None:
    with pytest.raises(service.InvalidUpdateError) as excinfo:
        service.append_message(
            _NoDatabase(),  # type: ignore[arg-type]
            uuid.uuid4(),
            uuid.uuid4(),
            "user",
            content,
        )
    message = str(excinfo.value)
    assert "NUL" in message
    assert "\x00" not in message and "system" not in message  # never echoes the input


def test_existing_role_and_length_rules_still_come_first() -> None:
    with pytest.raises(service.InvalidRoleError):
        service.append_message(_NoDatabase(), uuid.uuid4(), uuid.uuid4(), "root", "\x00")  # type: ignore[arg-type]
    with pytest.raises(service.InvalidUpdateError, match="exceeds"):
        service.append_message(
            _NoDatabase(),  # type: ignore[arg-type]
            uuid.uuid4(),
            uuid.uuid4(),
            "user",
            "\x00" * (MAX_MESSAGE_CONTENT_LENGTH + 1),
        )


# ---- HTTP contract ----


@pytest.fixture
def client_without_database() -> Generator[TestClient]:
    def _no_database() -> Generator[Any]:
        yield _NoDatabase()

    app.dependency_overrides[get_db] = _no_database
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.parametrize("content", ADVERSARIAL_NUL_CONTENT)
def test_api_returns_400_without_reaching_the_database(
    client_without_database: TestClient, content: str
) -> None:
    response = client_without_database.post(
        f"/repositories/{uuid.uuid4()}/sessions/{uuid.uuid4()}/messages",
        json={"role": "user", "content": content},  # JSON encodes NUL as \u0000
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "content must not contain NUL (U+0000) characters"}


# ---- Live PostgreSQL invariants ----


def _state(db: Session, session_id: uuid.UUID) -> tuple[int, int, object]:
    count, max_sequence = db.execute(
        select(func.count(), func.coalesce(func.max(SessionMessage.sequence_number), 0)).where(
            SessionMessage.session_id == session_id
        )
    ).one()
    last_active = db.execute(
        select(InvestigationSession.last_active_at).where(InvestigationSession.id == session_id)
    ).scalar_one()
    return count, max_sequence, last_active


@pytest.fixture
def live_client(db_session: Session) -> Generator[TestClient]:
    def _live() -> Generator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _live
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_rejected_message_leaves_no_trace_and_the_next_valid_message_succeeds(
    db_session: Session, repository: Repository, live_client: TestClient
) -> None:
    created = service.create_session(db_session, repository.id)
    url = f"/repositories/{repository.id}/sessions/{created.id}/messages"
    first = live_client.post(url, json={"role": "user", "content": "first"})
    assert first.status_code == 200 and first.json()["sequence_number"] == 1
    before = _state(db_session, created.id)

    for content in ADVERSARIAL_NUL_CONTENT:
        response = live_client.post(url, json={"role": "user", "content": content})
        assert response.status_code == 400
        assert _state(db_session, created.id) == before  # count, max sequence, recency

    stored = (
        db_session.execute(
            select(SessionMessage.content).where(SessionMessage.session_id == created.id)
        )
        .scalars()
        .all()
    )
    assert stored == ["first"]  # nothing stripped-and-stored, nothing partial

    second = live_client.post(url, json={"role": "assistant", "content": "second, after rejects"})
    assert second.status_code == 200
    assert second.json()["sequence_number"] == 2  # no sequence number consumed by rejects
    count, max_sequence, last_active = _state(db_session, created.id)
    assert (count, max_sequence) == (2, 2)
    assert last_active != before[2]  # a valid message still advances recency


# ---- Concurrency with the session row lock (committed data) ----


def test_rejected_message_holds_no_lock_and_concurrent_valid_messages_stay_ordered(
    engine: Engine,
) -> None:
    factory = sessionmaker(bind=engine)
    with factory() as setup:
        repo = Repository(
            owner=f"nul-{uuid.uuid4().hex[:8]}", name="r", url="https://x/nul/r", default_branch="m"
        )
        setup.add(repo)
        setup.commit()
        repo_id = repo.id
        session_id = service.create_session(setup, repo_id).id
    try:
        with factory() as rejecting:
            with pytest.raises(service.InvalidUpdateError):
                service.append_message(rejecting, repo_id, session_id, "user", "a\x00b")
            # No statement was issued, so no transaction (and no row lock) exists.
            assert not rejecting.in_transaction()
            with factory() as other:
                # NOWAIT fails immediately if anyone still holds the row lock.
                other.execute(
                    text("SELECT id FROM investigation_sessions WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": session_id},
                )
                other.rollback()

        errors: list[BaseException] = []
        barrier = threading.Barrier(5)

        def append(content: str) -> None:
            try:
                with factory() as db:
                    barrier.wait()
                    service.append_message(db, repo_id, session_id, "user", content)
            except service.InvalidUpdateError:
                pass  # the NUL message, expected
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        contents = ["v1", "v2", "bad\x00", "v3", "\x00bad"]
        threads = [threading.Thread(target=append, args=(c,)) for c in contents]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert errors == []

        with factory() as check:
            rows = check.execute(
                select(SessionMessage.sequence_number, SessionMessage.content)
                .where(SessionMessage.session_id == session_id)
                .order_by(SessionMessage.sequence_number)
            ).all()
        assert [seq for seq, _ in rows] == [1, 2, 3]  # contiguous, no gaps from rejects
        assert sorted(content for _, content in rows) == ["v1", "v2", "v3"]
    finally:
        with factory() as cleanup:
            cleanup.execute(text("DELETE FROM repositories WHERE id = :id"), {"id": repo_id})
            cleanup.commit()
