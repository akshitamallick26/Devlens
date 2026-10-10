"""API-boundary tests for the Testing/Generated Tests route: request
validation and response serialization. The underlying service function is
monkeypatched — no database needed, same convention as
tests/{impact,rag}/test_api.py.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

import app.api.routes.testing as testing_route
from app.db.session import get_db
from app.main import app
from app.testing.models import TestTarget, ValidationIssue
from app.testing.service import (
    RevisionNotFoundError,
    TargetGenerationOutcome,
    TestGenerationRunResult,
)


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


def _target() -> TestTarget:
    return TestTarget(
        target_symbol_id=uuid.uuid4(),
        qualified_name="checkout.pricing.apply_discount",
        kind="function",
        file_id=uuid.uuid4(),
        file_path="checkout/pricing.py",
        start_line=1,
        end_line=2,
        selection_reason="explicit_target",
        impact_depth=None,
        callers=(),
        callees=(),
        existing_tests=(),
    )


def test_invalid_repository_uuid_is_422() -> None:
    response = client.post(
        "/repositories/not-a-uuid/testing/generate", json={"revision_id": REVISION_ID}
    )
    assert response.status_code == 422


def test_invalid_revision_uuid_is_422() -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/testing/generate", json={"revision_id": "not-a-uuid"}
    )
    assert response.status_code == 422


def test_max_tests_over_the_limit_is_422() -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/testing/generate",
        json={"revision_id": REVISION_ID, "max_tests": 1000},
    )
    assert response.status_code == 422


def test_invalid_symbol_id_in_list_is_422(monkeypatch: pytest.MonkeyPatch) -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/testing/generate",
        json={"revision_id": REVISION_ID, "symbol_ids": ["not-a-uuid"]},
    )
    assert response.status_code == 422


def test_revision_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise RevisionNotFoundError("no such revision")

    monkeypatch.setattr(testing_route, "generate_tests_for_target", _raise)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/testing/generate", json={"revision_id": REVISION_ID}
    )
    assert response.status_code == 404


def test_no_targets_result_is_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    result = TestGenerationRunResult(
        repository_id=uuid.UUID(REPOSITORY_ID),
        revision_id=uuid.UUID(REVISION_ID),
        analysis_run_id=None,
        analysis_status="no_targets",
        outcomes=(),
    )
    monkeypatch.setattr(testing_route, "generate_tests_for_target", lambda *a, **k: result)
    monkeypatch.setattr(testing_route, "get_provider", lambda settings: None)

    response = client.post(
        f"/repositories/{REPOSITORY_ID}/testing/generate", json={"revision_id": REVISION_ID}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["analysis_status"] == "no_targets"
    assert body["outcomes"] == []
    assert "limitations" in body and body["limitations"]


def test_outcome_result_is_serialized_not_an_orm_object(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _target()
    generated_test_id = uuid.uuid4()
    outcome = TargetGenerationOutcome(
        target=target,
        status="validation_failed",
        generated_test_id=generated_test_id,
        execution_id=None,
        validation_issues=(ValidationIssue("unsafe_path", "escapes sandbox"),),
        execution=None,
        error=None,
    )
    result = TestGenerationRunResult(
        repository_id=uuid.UUID(REPOSITORY_ID),
        revision_id=uuid.UUID(REVISION_ID),
        analysis_run_id=uuid.uuid4(),
        analysis_status="completed",
        outcomes=(outcome,),
    )
    monkeypatch.setattr(testing_route, "generate_tests_for_target", lambda *a, **k: result)
    monkeypatch.setattr(testing_route, "get_provider", lambda settings: object())

    response = client.post(
        f"/repositories/{REPOSITORY_ID}/testing/generate", json={"revision_id": REVISION_ID}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["analysis_status"] == "completed"
    assert len(body["outcomes"]) == 1
    item = body["outcomes"][0]
    assert item["status"] == "validation_failed"
    assert item["generated_test_id"] == str(generated_test_id)
    assert isinstance(item["target_symbol_id"], str)  # never a raw UUID/ORM object
    assert item["validation_issues"][0]["code"] == "unsafe_path"
