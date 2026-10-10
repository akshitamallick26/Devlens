"""Investigation Sessions v1.2: the U+0000 input boundary for session titles
(create and PATCH) and graph-focus camera state (JSONB), measured against
live PostgreSQL behavior:

- `text` rejects NUL (DataError); JSONB rejects a \\u0000 escape anywhere,
  keys and nested values included (UntranslatableCharacter);
- every other control character, and the literal six-character text
  "\\u0000", is stored verbatim and must stay accepted.

NUL is rejected in the service before any database work, so the route
returns 400 and nothing is created, assigned, locked or bumped."""

import uuid
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.investigation import service
from app.investigation.models import MAX_TITLE_LENGTH
from app.main import app
from app.models import InvestigationSession, Repository, SessionContextRef
from tests.investigation.test_nul_input_db import _NoDatabase

NUL_TITLES = [
    "\x00",
    "\x00leading",
    "mid\x00dle",
    "trailing\x00",
    "\x00multi\x00ple\x00",
    "Grüße \U0001f600\x00認証",
    "t" * (MAX_TITLE_LENGTH - 1) + "\x00",  # exactly at the length limit
]

NUL_CAMERA_STATES: list[dict[str, Any]] = [
    {"focus_symbol_id": "\x00"},
    {"focus_symbol_id": "abc\x00def", "depth": 2},
    {"fo\x00cus": 1},  # NUL in a key
    {"path": [{"x": ["ok", "tail\x00"]}]},  # nested list/dict value
    {"label": "認証\x00\U0001f600"},
]

VERBATIM_CAMERA_STATE: dict[str, Any] = {
    "focus_symbol_id": "s",
    "direction": "both",
    "depth": 2,
    "note": "tab\tnewline\n\x01\x1b[31mred\x1b[0m \U0001f600 認証",
    "literal": "\\u0000",  # backslash-u text, not a NUL character
}


# ---- Service: rejected before any database work ----


@pytest.mark.parametrize("title", NUL_TITLES)
def test_create_session_rejects_nul_title_before_database_work(title: str) -> None:
    with pytest.raises(service.InvalidUpdateError, match="title must not contain NUL"):
        service.create_session(_NoDatabase(), uuid.uuid4(), title=title)  # type: ignore[arg-type]


@pytest.mark.parametrize("title", NUL_TITLES)
def test_update_session_rejects_nul_title_before_database_work(title: str) -> None:
    with pytest.raises(service.InvalidUpdateError, match="title must not contain NUL"):
        service.update_session(
            _NoDatabase(),  # type: ignore[arg-type]
            uuid.uuid4(),
            uuid.uuid4(),
            title=title,
            title_set=True,
            status="archived",  # other fields in the same PATCH never get applied
            status_set=True,
        )


@pytest.mark.parametrize("state", NUL_CAMERA_STATES)
def test_graph_focus_rejects_nul_camera_state_before_database_work(state: dict[str, Any]) -> None:
    with pytest.raises(service.InvalidUpdateError, match="graph_camera_state must not contain NUL"):
        service.add_context_ref(
            _NoDatabase(),  # type: ignore[arg-type]
            uuid.uuid4(),
            uuid.uuid4(),
            "graph_focus",
            graph_camera_state=state,
        )


def test_json_walk_handles_deep_nesting_without_recursion_errors() -> None:
    state: Any = "\x00"
    for _ in range(20_000):
        state = {"n": [state]}
    with pytest.raises(service.InvalidUpdateError):
        service._reject_nul_in_json("graph_camera_state", state)
    clean: Any = "ok"
    for _ in range(20_000):
        clean = {"n": [clean]}
    service._reject_nul_in_json("graph_camera_state", clean)  # no exception


def test_error_messages_never_echo_the_rejected_value() -> None:
    with pytest.raises(service.InvalidUpdateError) as excinfo:
        service.create_session(_NoDatabase(), uuid.uuid4(), title="secret\x00payload")  # type: ignore[arg-type]
    assert "secret" not in str(excinfo.value) and "\x00" not in str(excinfo.value)


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


def test_every_affected_endpoint_returns_400_without_reaching_the_database(
    client_without_database: TestClient,
) -> None:
    repo, sid = uuid.uuid4(), uuid.uuid4()
    calls = [
        ("post", f"/repositories/{repo}/sessions", {"title": "a\x00b"}, "title"),
        ("patch", f"/repositories/{repo}/sessions/{sid}", {"title": "a\x00b"}, "title"),
        (
            "post",
            f"/repositories/{repo}/sessions/{sid}/context",
            {"ref_type": "graph_focus", "graph_camera_state": {"focus": "a\x00b"}},
            "graph_camera_state",
        ),
    ]
    for method, url, body, field in calls:
        response = getattr(client_without_database, method)(url, json=body)
        assert response.status_code == 400, url
        assert response.json() == {"detail": f"{field} must not contain NUL (U+0000) characters"}


# ---- Live PostgreSQL invariants ----


@pytest.fixture
def live_client(db_session: Session) -> Generator[TestClient]:
    def _live() -> Generator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _live
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _session_state(db: Session, session_id: uuid.UUID) -> tuple[Any, ...]:
    row = db.execute(
        select(
            InvestigationSession.title,
            InvestigationSession.status,
            InvestigationSession.last_active_at,
        ).where(InvestigationSession.id == session_id)
    ).one()
    refs = db.execute(
        select(func.count()).where(SessionContextRef.session_id == session_id)
    ).scalar_one()
    return (*row, refs)


def test_rejected_titles_create_nothing_and_change_nothing(
    db_session: Session, repository: Repository, live_client: TestClient
) -> None:
    def sessions() -> int:
        return db_session.execute(
            select(func.count()).where(InvestigationSession.repository_id == repository.id)
        ).scalar_one()

    for title in NUL_TITLES:
        response = live_client.post(
            f"/repositories/{repository.id}/sessions", json={"title": title}
        )
        assert response.status_code == 400
    assert sessions() == 0

    created = live_client.post(
        f"/repositories/{repository.id}/sessions", json={"title": "Original"}
    )
    assert created.status_code == 200
    session_id = uuid.UUID(created.json()["id"])
    before = _session_state(db_session, session_id)
    url = f"/repositories/{repository.id}/sessions/{session_id}"
    for title in NUL_TITLES:
        response = live_client.patch(url, json={"title": title, "status": "archived"})
        assert response.status_code == 400
        assert _session_state(db_session, session_id) == before  # title, status, recency, refs

    renamed = live_client.patch(url, json={"title": "Renamed \U0001f600 認証\t\x01"})
    assert renamed.status_code == 200
    title, status, last_active, _ = _session_state(db_session, session_id)
    assert title == "Renamed \U0001f600 認証\t\x01"  # supported characters stay verbatim
    assert status == "active" and last_active != before[2]
    assert sessions() == 1


def test_rejected_camera_state_persists_nothing_and_valid_state_is_verbatim(
    db_session: Session, repository: Repository, live_client: TestClient
) -> None:
    session_id = service.create_session(db_session, repository.id, title="graph").id
    before = _session_state(db_session, session_id)
    url = f"/repositories/{repository.id}/sessions/{session_id}/context"
    for state in NUL_CAMERA_STATES:
        response = live_client.post(
            url, json={"ref_type": "graph_focus", "graph_camera_state": state}
        )
        assert response.status_code == 400
        assert _session_state(db_session, session_id) == before  # no ref, no recency bump

    ok = live_client.post(
        url, json={"ref_type": "graph_focus", "graph_camera_state": VERBATIM_CAMERA_STATE}
    )
    assert ok.status_code == 200
    stored = db_session.execute(
        select(SessionContextRef.graph_camera_state).where(
            SessionContextRef.session_id == session_id
        )
    ).scalar_one()
    assert stored == VERBATIM_CAMERA_STATE
    assert _session_state(db_session, session_id)[3] == 1
