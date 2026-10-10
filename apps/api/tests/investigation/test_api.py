"""API-boundary tests for the Investigation Sessions route: request
validation, error-code mapping, and response serialization. The underlying
service functions are monkeypatched — no database needed, same convention
as every other tests/*/test_api.py in this project (e.g.
tests/automation/test_api.py).
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

import app.api.routes.investigation as investigation_route
from app.db.session import get_db
from app.investigation.models import ContextRefView, MessagePage, MessageView, SessionSnapshot
from app.investigation.service import (
    ContextRefNotFoundError,
    CrossRepositoryReferenceError,
    InvalidContextRefError,
    InvalidUpdateError,
    RepositoryNotFoundError,
    RevisionConflictError,
    RevisionNotFoundError,
    SessionNotFoundError,
)
from app.main import app


@pytest.fixture(autouse=True)
def _override_db() -> Generator[None]:
    app.dependency_overrides[get_db] = lambda: iter([None])
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)


client = TestClient(app)

REPOSITORY_ID = str(uuid.uuid4())
SESSION_ID = str(uuid.uuid4())


def _empty_snapshot(title: str | None = "t") -> SessionSnapshot:
    return SessionSnapshot(
        id=uuid.UUID(SESSION_ID),
        repository_id=uuid.UUID(REPOSITORY_ID),
        title=title,
        status="active",
        started_at_revision_id=None,
        current_revision_id=None,
        created_at="2026-01-01T00:00:00+00:00",
        last_active_at="2026-01-01T00:00:00+00:00",
        message_page=MessagePage(messages=(), has_more=False, next_before_sequence=None),
        context_refs=(),
    )


# ---- UUID validation (422) ----


def test_invalid_repository_uuid_on_create_is_422() -> None:
    response = client.post("/repositories/not-a-uuid/sessions", json={})
    assert response.status_code == 422


def test_invalid_revision_uuid_on_create_is_422() -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/sessions", json={"revision_id": "not-a-uuid"}
    )
    assert response.status_code == 422


def test_invalid_session_uuid_on_get_is_422() -> None:
    response = client.get(f"/repositories/{REPOSITORY_ID}/sessions/not-a-uuid")
    assert response.status_code == 422


# ---- Create ----


def test_create_session_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        investigation_route, "create_session", lambda *a, **k: _empty_snapshot("New session")
    )
    response = client.post(f"/repositories/{REPOSITORY_ID}/sessions", json={"title": "New session"})
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "New session"
    assert isinstance(body["id"], str)
    assert body["messages"]["messages"] == []
    assert body["context_refs"] == []


def test_create_session_repository_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise RepositoryNotFoundError("no such repository")

    monkeypatch.setattr(investigation_route, "create_session", _raise)
    response = client.post(f"/repositories/{REPOSITORY_ID}/sessions", json={})
    assert response.status_code == 404


def test_create_session_revision_conflict_maps_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise RevisionConflictError("wrong repository")

    monkeypatch.setattr(investigation_route, "create_session", _raise)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/sessions", json={"revision_id": str(uuid.uuid4())}
    )
    assert response.status_code == 409


def test_oversized_title_is_422_via_pydantic() -> None:
    response = client.post(f"/repositories/{REPOSITORY_ID}/sessions", json={"title": "x" * 1000})
    assert response.status_code == 422


# ---- Get ----


def test_get_session_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(investigation_route, "get_session", lambda *a, **k: _empty_snapshot())
    response = client.get(f"/repositories/{REPOSITORY_ID}/sessions/{SESSION_ID}")
    assert response.status_code == 200


def test_get_session_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise SessionNotFoundError("no such session")

    monkeypatch.setattr(investigation_route, "get_session", _raise)
    response = client.get(f"/repositories/{REPOSITORY_ID}/sessions/{SESSION_ID}")
    assert response.status_code == 404


def test_message_page_query_bounds_are_enforced() -> None:
    response = client.get(
        f"/repositories/{REPOSITORY_ID}/sessions/{SESSION_ID}", params={"message_limit": 99999}
    )
    assert response.status_code == 422


def test_recents_limit_query_bounds_are_enforced() -> None:
    response = client.get(f"/repositories/{REPOSITORY_ID}/sessions", params={"limit": 99999})
    assert response.status_code == 422


# ---- Update ----


def test_update_session_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        investigation_route, "update_session", lambda *a, **k: _empty_snapshot("renamed")
    )
    response = client.patch(
        f"/repositories/{REPOSITORY_ID}/sessions/{SESSION_ID}", json={"title": "renamed"}
    )
    assert response.status_code == 200
    assert response.json()["title"] == "renamed"


def test_update_session_no_fields_maps_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise InvalidUpdateError("at least one field required")

    monkeypatch.setattr(investigation_route, "update_session", _raise)
    response = client.patch(f"/repositories/{REPOSITORY_ID}/sessions/{SESSION_ID}", json={})
    assert response.status_code == 400


def test_update_session_revision_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise RevisionNotFoundError("no such revision")

    monkeypatch.setattr(investigation_route, "update_session", _raise)
    response = client.patch(
        f"/repositories/{REPOSITORY_ID}/sessions/{SESSION_ID}",
        json={"revision_id": str(uuid.uuid4())},
    )
    assert response.status_code == 404


# ---- Messages ----


def test_append_message_success(monkeypatch: pytest.MonkeyPatch) -> None:
    view = MessageView(
        id=uuid.uuid4(),
        session_id=uuid.UUID(SESSION_ID),
        role="user",
        content="hello",
        sequence_number=1,
        created_at="2026-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(investigation_route, "append_message", lambda *a, **k: view)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/sessions/{SESSION_ID}/messages",
        json={"role": "user", "content": "hello"},
    )
    assert response.status_code == 200
    assert response.json()["sequence_number"] == 1


def test_oversized_message_content_is_422_via_pydantic() -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/sessions/{SESSION_ID}/messages",
        json={"role": "user", "content": "x" * 100_000},
    )
    assert response.status_code == 422


def test_append_message_session_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise SessionNotFoundError("no such session")

    monkeypatch.setattr(investigation_route, "append_message", _raise)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/sessions/{SESSION_ID}/messages",
        json={"role": "user", "content": "hi"},
    )
    assert response.status_code == 404


# ---- Context ----


def test_add_context_ref_success(monkeypatch: pytest.MonkeyPatch) -> None:
    view = ContextRefView(
        id=uuid.uuid4(),
        session_id=uuid.UUID(SESSION_ID),
        message_id=None,
        ref_type="selected_file",
        file_id=uuid.uuid4(),
        symbol_id=None,
        analysis_run_id=None,
        finding_id=None,
        graph_camera_state=None,
        created_at="2026-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(investigation_route, "add_context_ref", lambda *a, **k: view)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/sessions/{SESSION_ID}/context",
        json={"ref_type": "selected_file", "file_id": str(uuid.uuid4())},
    )
    assert response.status_code == 200
    assert response.json()["ref_type"] == "selected_file"


def test_add_context_ref_cross_repository_maps_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise CrossRepositoryReferenceError("wrong repository")

    monkeypatch.setattr(investigation_route, "add_context_ref", _raise)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/sessions/{SESSION_ID}/context",
        json={"ref_type": "selected_file", "file_id": str(uuid.uuid4())},
    )
    assert response.status_code == 409


def test_add_context_ref_invalid_shape_maps_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise InvalidContextRefError("selected_file requires file_id")

    monkeypatch.setattr(investigation_route, "add_context_ref", _raise)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/sessions/{SESSION_ID}/context",
        json={"ref_type": "selected_file"},
    )
    assert response.status_code == 400


def test_remove_context_ref_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(investigation_route, "remove_context_ref", lambda *a, **k: None)
    response = client.delete(
        f"/repositories/{REPOSITORY_ID}/sessions/{SESSION_ID}/context/{uuid.uuid4()}"
    )
    assert response.status_code == 204


def test_remove_context_ref_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise ContextRefNotFoundError("no such context ref")

    monkeypatch.setattr(investigation_route, "remove_context_ref", _raise)
    response = client.delete(
        f"/repositories/{REPOSITORY_ID}/sessions/{SESSION_ID}/context/{uuid.uuid4()}"
    )
    assert response.status_code == 404
