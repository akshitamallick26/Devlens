"""API-boundary tests for the Automation Intelligence route: request
validation and response serialization. The underlying service function is
monkeypatched — no database needed, same convention as every other
tests/*/test_api.py in this project.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

import app.api.routes.automation as automation_route
from app.automation.models import (
    AutomationRunResult,
    DeterministicChangeSummary,
    FileDelta,
    ParentResolution,
    RAGInvalidationPlan,
    SecurityInvalidationPlan,
    StageExecutionPlan,
    SymbolDelta,
    TestingInvalidationPlan,
)
from app.automation.service import ParentVerificationError, RevisionNotFoundError
from app.db.session import get_db
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


def _empty_result(status: str = "completed") -> AutomationRunResult:
    return AutomationRunResult(
        repository_id=uuid.UUID(REPOSITORY_ID),
        current_revision_id=uuid.UUID(REVISION_ID),
        parent_revision_id=None,
        analysis_run_id=uuid.uuid4(),
        analysis_status=status,
        file_delta=FileDelta(added=(), deleted=(), modified=(), unchanged=()),
        symbol_delta=SymbolDelta(added=(), deleted=(), modified=(), unchanged=()),
        parsing_execution=StageExecutionPlan(
            stage="parsing",
            invalidated=False,
            execution_strategy="full_revision_rebuild",
            status="completed",
            detail=None,
        ),
        graph_execution=StageExecutionPlan(
            stage="graph_construction",
            invalidated=False,
            execution_strategy="full_revision_rebuild",
            status="completed",
            detail=None,
        ),
        impact_summary=None,
        rag_invalidation=RAGInvalidationPlan(
            status="rebuilt",
            chunks_invalidated=0,
            embeddings_require_regeneration=True,
            execution_strategy="full_revision_rebuild",
            detail=None,
        ),
        testing_invalidation=TestingInvalidationPlan(
            stale_generated_test_ids=(), changed_target_symbol_ids=(), detail=None
        ),
        security_invalidation=SecurityInvalidationPlan(
            rerun_required=True, reason="no parent revision", detail=None
        ),
        summary=DeterministicChangeSummary(
            files_added=0,
            files_modified=0,
            files_deleted=0,
            files_unchanged=0,
            symbols_added=0,
            symbols_modified=0,
            symbols_deleted=0,
            symbols_unchanged=0,
            directly_affected_symbols=0,
            transitively_affected_symbols=0,
            affected_files=0,
            chunks_invalidated=0,
            embeddings_require_regeneration=True,
            stale_generated_tests=0,
            changed_test_targets=0,
            security_rerun_required=True,
            deleted_symbols_with_unavailable_impact=0,
        ),
        limitations=("no parent revision",),
    )


def test_invalid_repository_uuid_is_422() -> None:
    response = client.post(
        "/repositories/not-a-uuid/automation/analyze", json={"revision_id": REVISION_ID}
    )
    assert response.status_code == 422


def test_invalid_revision_uuid_is_422() -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/automation/analyze", json={"revision_id": "not-a-uuid"}
    )
    assert response.status_code == 422


def test_invalid_parent_revision_uuid_is_422() -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/automation/analyze",
        json={"revision_id": REVISION_ID, "parent_revision_id": "not-a-uuid"},
    )
    assert response.status_code == 422


def test_revision_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise RevisionNotFoundError("no such revision")

    monkeypatch.setattr(automation_route, "run_automation_analysis", _raise)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/automation/analyze", json={"revision_id": REVISION_ID}
    )
    assert response.status_code == 404


def test_parent_mismatch_maps_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise ParentVerificationError(
            ParentResolution(status="mismatch", parent_revision_id=None, detail="not the parent")
        )

    monkeypatch.setattr(automation_route, "run_automation_analysis", _raise)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/automation/analyze",
        json={"revision_id": REVISION_ID, "parent_revision_id": str(uuid.uuid4())},
    )
    assert response.status_code == 409


def test_completed_result_is_serialized_not_an_orm_object(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _empty_result()
    monkeypatch.setattr(automation_route, "run_automation_analysis", lambda *a, **k: result)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/automation/analyze", json={"revision_id": REVISION_ID}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["analysis_status"] == "completed"
    assert isinstance(body["analysis_run_id"], str)  # never a raw UUID/ORM object
    assert body["parent_revision_id"] is None
    assert body["file_delta"]["rename_detection"] == "unsupported"
    assert body["summary"]["security_rerun_required"] is True
    # Hardening Blocker 2: execution strategy is always visible, distinct
    # from the invalidation decision.
    assert body["parsing_execution"]["execution_strategy"] == "full_revision_rebuild"
    assert body["parsing_execution"]["invalidated"] is False
    assert body["graph_execution"]["execution_strategy"] == "full_revision_rebuild"
    assert body["rag_invalidation"]["execution_strategy"] == "full_revision_rebuild"
    assert "execution_note" in body and "full-revision rebuilds" in body["execution_note"]


def test_partial_status_is_preserved_through_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _empty_result(status="partial")
    monkeypatch.setattr(automation_route, "run_automation_analysis", lambda *a, **k: result)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/automation/analyze", json={"revision_id": REVISION_ID}
    )
    assert response.json()["analysis_status"] == "partial"
