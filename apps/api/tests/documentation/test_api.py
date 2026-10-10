"""API-boundary tests for the Documentation Intelligence route: request
validation, error-code mapping, and response serialization. The underlying
service functions are monkeypatched — no database needed, same convention
as every other tests/*/test_api.py in this project.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

import app.api.routes.documentation as documentation_route
from app.db.session import get_db
from app.documentation.models import DocumentationArtifactResult, DocumentationArtifactSummary
from app.documentation.service import (
    ArtifactNotFoundError,
    InvalidArtifactTypeError,
    MissingTargetError,
    RevisionNotFoundError,
    TargetConflictError,
    TargetNotFoundError,
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
REVISION_ID = str(uuid.uuid4())
ARTIFACT_ID = str(uuid.uuid4())
ANALYSIS_RUN_ID = str(uuid.uuid4())


def _result(generation_method: str = "deterministic_fallback") -> DocumentationArtifactResult:
    return DocumentationArtifactResult(
        artifact_id=uuid.UUID(ARTIFACT_ID),
        repository_id=uuid.UUID(REPOSITORY_ID),
        revision_id=uuid.UUID(REVISION_ID),
        artifact_type="repository_overview",
        target_file_id=None,
        target_symbol_id=None,
        title="Repository Overview — o/r",
        content="# Repository Overview — o/r\n",
        generation_method=generation_method,
        provider="",
        model_name="",
        model_version="",
        evidence_ids=(),
        cited_evidence_ids=(),
        context_truncated=False,
        analysis_run_id=uuid.UUID(ANALYSIS_RUN_ID),
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _summary() -> DocumentationArtifactSummary:
    return DocumentationArtifactSummary(
        artifact_id=uuid.UUID(ARTIFACT_ID),
        repository_id=uuid.UUID(REPOSITORY_ID),
        revision_id=uuid.UUID(REVISION_ID),
        artifact_type="repository_overview",
        target_file_id=None,
        target_symbol_id=None,
        title="Repository Overview — o/r",
        generation_method="deterministic_fallback",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


# ---- UUID validation (422) ----


def test_invalid_repository_uuid_is_422() -> None:
    response = client.post(
        "/repositories/not-a-uuid/documentation/generate",
        json={"revision_id": REVISION_ID, "artifact_type": "repository_overview"},
    )
    assert response.status_code == 422


def test_invalid_revision_uuid_is_422() -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/documentation/generate",
        json={"revision_id": "not-a-uuid", "artifact_type": "repository_overview"},
    )
    assert response.status_code == 422


def test_invalid_artifact_id_is_422() -> None:
    response = client.get(f"/repositories/{REPOSITORY_ID}/documentation/not-a-uuid")
    assert response.status_code == 422


# ---- Generate ----


def test_generate_success_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documentation_route, "get_provider", lambda *a, **k: None)
    monkeypatch.setattr(documentation_route, "generate_documentation", lambda *a, **k: _result())
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/documentation/generate",
        json={"revision_id": REVISION_ID, "artifact_type": "repository_overview"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["generation_status"] == "fallback"
    assert isinstance(body["artifact_id"], str)
    assert body["content"].startswith("# Repository Overview")


def test_generate_success_llm_generated_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documentation_route, "get_provider", lambda *a, **k: None)
    monkeypatch.setattr(
        documentation_route,
        "generate_documentation",
        lambda *a, **k: _result(generation_method="llm_generated"),
    )
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/documentation/generate",
        json={"revision_id": REVISION_ID, "artifact_type": "repository_overview"},
    )
    assert response.json()["generation_status"] == "generated"


def test_generate_revision_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documentation_route, "get_provider", lambda *a, **k: None)

    def _raise(*args: object, **kwargs: object) -> None:
        raise RevisionNotFoundError("no such revision")

    monkeypatch.setattr(documentation_route, "generate_documentation", _raise)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/documentation/generate",
        json={"revision_id": REVISION_ID, "artifact_type": "repository_overview"},
    )
    assert response.status_code == 404


def test_generate_invalid_artifact_type_maps_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documentation_route, "get_provider", lambda *a, **k: None)

    def _raise(*args: object, **kwargs: object) -> None:
        raise InvalidArtifactTypeError("bad type")

    monkeypatch.setattr(documentation_route, "generate_documentation", _raise)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/documentation/generate",
        json={"revision_id": REVISION_ID, "artifact_type": "not_real"},
    )
    assert response.status_code == 400


def test_generate_missing_target_maps_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documentation_route, "get_provider", lambda *a, **k: None)

    def _raise(*args: object, **kwargs: object) -> None:
        raise MissingTargetError("source_file_id required")

    monkeypatch.setattr(documentation_route, "generate_documentation", _raise)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/documentation/generate",
        json={"revision_id": REVISION_ID, "artifact_type": "module_documentation"},
    )
    assert response.status_code == 400


def test_generate_target_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documentation_route, "get_provider", lambda *a, **k: None)

    def _raise(*args: object, **kwargs: object) -> None:
        raise TargetNotFoundError("no such symbol")

    monkeypatch.setattr(documentation_route, "generate_documentation", _raise)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/documentation/generate",
        json={
            "revision_id": REVISION_ID,
            "artifact_type": "symbol_documentation",
            "symbol_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 404


def test_generate_target_conflict_maps_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documentation_route, "get_provider", lambda *a, **k: None)

    def _raise(*args: object, **kwargs: object) -> None:
        raise TargetConflictError("wrong repository")

    monkeypatch.setattr(documentation_route, "generate_documentation", _raise)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/documentation/generate",
        json={
            "revision_id": REVISION_ID,
            "artifact_type": "symbol_documentation",
            "symbol_id": str(uuid.uuid4()),
        },
    )
    assert response.status_code == 409


# ---- List ----


def test_list_documentation_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documentation_route, "list_artifacts", lambda *a, **k: (_summary(),))
    response = client.get(f"/repositories/{REPOSITORY_ID}/documentation")
    assert response.status_code == 200
    body = response.json()
    assert len(body["artifacts"]) == 1
    assert body["artifacts"][0]["generation_status"] == "fallback"
    assert "content" not in body["artifacts"][0]  # summary never carries full content


def test_list_documentation_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documentation_route, "list_artifacts", lambda *a, **k: ())
    response = client.get(f"/repositories/{REPOSITORY_ID}/documentation")
    assert response.status_code == 200
    assert response.json()["artifacts"] == []


def test_list_documentation_invalid_revision_filter_is_422() -> None:
    response = client.get(
        f"/repositories/{REPOSITORY_ID}/documentation", params={"revision_id": "not-a-uuid"}
    )
    assert response.status_code == 422


# ---- Get one ----


def test_get_documentation_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documentation_route, "get_artifact", lambda *a, **k: _result())
    response = client.get(f"/repositories/{REPOSITORY_ID}/documentation/{ARTIFACT_ID}")
    assert response.status_code == 200
    assert response.json()["artifact_id"] == ARTIFACT_ID


def test_get_documentation_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise ArtifactNotFoundError("no such artifact")

    monkeypatch.setattr(documentation_route, "get_artifact", _raise)
    response = client.get(f"/repositories/{REPOSITORY_ID}/documentation/{ARTIFACT_ID}")
    assert response.status_code == 404
