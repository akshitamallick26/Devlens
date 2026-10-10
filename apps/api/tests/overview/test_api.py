"""API-boundary tests for the Repository Overview route: id parsing, the
optional revision selector, error-code mapping, and response serialization.
The service function is monkeypatched — no database needed, same convention
as every other tests/*/test_api.py in this project.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

import app.api.routes.overview as overview_route
from app.db.session import get_db
from app.main import app
from app.overview import service
from app.overview.models import RepositoryOverview
from tests.overview.test_service_static import _session


@pytest.fixture(autouse=True)
def _override_db() -> Generator[None]:
    app.dependency_overrides[get_db] = lambda: iter([None])
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)


client = TestClient(app)


def _overview() -> RepositoryOverview:
    db, repository, revision = _session()
    return service.get_repository_overview(db, repository.id, revision.id)  # type: ignore[arg-type]


def _patch(monkeypatch: pytest.MonkeyPatch, result: object) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []

    def _fake(db: object, repository_id: uuid.UUID, revision_id: uuid.UUID | None = None) -> object:
        calls.append((repository_id, revision_id))
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(overview_route, "get_repository_overview", _fake)
    return calls


def test_success_serializes_full_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    overview = _overview()
    _patch(monkeypatch, overview)
    response = client.get(f"/repositories/{overview.repository.repository_id}/overview")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "repository",
        "revision",
        "statistics",
        "structure",
        "graph",
        "important_components",
        "analysis_statuses",
        "security",
        "testing",
        "documentation",
        "rag",
        "automation",
        "sessions",
        "limitations",
    }
    assert body["revision"]["revision_id"] == str(overview.revision.revision_id)
    assert body["revision"]["commit_sha"] == "a" * 40
    assert body["revision"]["committed_at"].startswith("2026-01-01")
    assert isinstance(body["analysis_statuses"], list)
    assert body["analysis_statuses"][0]["status"] == "not_run"
    assert body["security"]["latest_run"]["status"] == "not_run"
    assert body["structure"]["top_level_directories"][0]["name"] == "src"
    assert body["limitations"] == list(service.STATIC_LIMITATIONS)


def test_bounded_fields_are_lists_with_truncation_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    overview = _overview()
    _patch(monkeypatch, overview)
    body = client.get(f"/repositories/{overview.repository.repository_id}/overview").json()
    components = body["important_components"]
    for name in (
        "most_depended_upon_symbols",
        "most_dependency_heavy_symbols",
        "most_depended_upon_modules",
        "most_dependency_heavy_modules",
    ):
        assert isinstance(components[name], list)
        assert len(components[name]) <= service.MAX_RANKED_COMPONENTS
        assert components[f"{name}_truncated"] is False
    assert body["structure"]["top_level_directories_truncated"] is False
    assert len(body["sessions"]["recent_sessions"]) <= service.MAX_RECENT_SESSIONS


def test_omitted_revision_passes_none_to_service(monkeypatch: pytest.MonkeyPatch) -> None:
    overview = _overview()
    calls = _patch(monkeypatch, overview)
    repository_id = overview.repository.repository_id
    client.get(f"/repositories/{repository_id}/overview")
    assert calls == [(repository_id, None)]


def test_explicit_revision_is_parsed_and_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    overview = _overview()
    calls = _patch(monkeypatch, overview)
    repository_id = overview.repository.repository_id
    revision_id = uuid.uuid4()
    client.get(f"/repositories/{repository_id}/overview", params={"revision_id": str(revision_id)})
    assert calls == [(repository_id, revision_id)]


def test_unknown_repository_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, service.RepositoryNotFoundError("no repository"))
    response = client.get(f"/repositories/{uuid.uuid4()}/overview")
    assert response.status_code == 404
    assert response.json() == {"detail": "no repository"}


def test_unknown_revision_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, service.RevisionNotFoundError("no revision"))
    response = client.get(
        f"/repositories/{uuid.uuid4()}/overview", params={"revision_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404
    assert response.json() == {"detail": "no revision"}


def test_revision_of_another_repository_is_409(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, service.RevisionConflictError("different repository"))
    response = client.get(
        f"/repositories/{uuid.uuid4()}/overview", params={"revision_id": str(uuid.uuid4())}
    )
    assert response.status_code == 409


def test_invalid_repository_id_is_422() -> None:
    response = client.get("/repositories/not-a-uuid/overview")
    assert response.status_code == 422


def test_invalid_revision_id_is_422() -> None:
    response = client.get(
        f"/repositories/{uuid.uuid4()}/overview", params={"revision_id": "not-a-uuid"}
    )
    assert response.status_code == 422
