"""API-boundary tests for the impact-analysis route: request validation and
status-code mapping. The underlying service functions are monkeypatched —
no database needed, same as tests/test_health.py's TestClient usage — real
end-to-end behavior is covered by tests/impact/test_service.py.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

import app.api.routes.impact as impact_route
from app.ai.models import AIConfigurationError
from app.db.session import get_db
from app.impact.explanation import ImpactExplanation, ImpactIntelligenceResult, ResolvedEvidence
from app.impact.models import ImpactAnalysisResult, SymbolView
from app.impact.service import ImpactAnalysisRunResult
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


def test_request_requires_exactly_one_target() -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/impact", json={"revision_id": REVISION_ID}
    )
    assert response.status_code == 422


def test_request_rejects_both_targets_given_together() -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/impact",
        json={
            "revision_id": REVISION_ID,
            "symbol_id": str(uuid.uuid4()),
            "qualified_name": "a.b",
        },
    )
    assert response.status_code == 422


def test_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        impact_route,
        "run_impact_analysis_by_qualified_name",
        lambda *a, **k: ImpactAnalysisRunResult(status="not_found"),
    )
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/impact",
        json={"revision_id": REVISION_ID, "qualified_name": "does.not.exist"},
    )
    assert response.status_code == 404


def test_ambiguous_maps_to_409_with_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = SymbolView(
        id=uuid.uuid4(),
        qualified_name="a.b",
        kind="function",
        file_id=uuid.uuid4(),
        file_path="a.py",
        parent_symbol_id=None,
    )
    monkeypatch.setattr(
        impact_route,
        "run_impact_analysis_by_qualified_name",
        lambda *a, **k: ImpactAnalysisRunResult(status="ambiguous", candidates=(candidate,)),
    )
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/impact",
        json={"revision_id": REVISION_ID, "qualified_name": "a.b"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["candidates"][0]["symbol_id"] == str(candidate.id)


def _completed_run_result(target: SymbolView) -> ImpactAnalysisRunResult:
    result = ImpactAnalysisResult(
        revision_id=uuid.UUID(REVISION_ID),
        target_symbol_id=target.id,
        target_symbol=target,
        direct_impacts=(),
        transitive_impacts=(),
        impacted_files=(),
        max_depth=0,
    )
    return ImpactAnalysisRunResult(status="completed", result=result, analysis_run_id=uuid.uuid4())


def test_deterministic_request_never_calls_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """include_explanation defaults to False — get_provider must not even
    be called."""
    target = SymbolView(
        id=uuid.uuid4(),
        qualified_name="a.b",
        kind="function",
        file_id=uuid.uuid4(),
        file_path="a.py",
        parent_symbol_id=None,
    )
    monkeypatch.setattr(
        impact_route,
        "run_impact_analysis_by_symbol_id",
        lambda *a, **k: _completed_run_result(target),
    )

    def _fail_get_provider(*args: object, **kwargs: object) -> None:
        raise AssertionError("get_provider called despite include_explanation=False")

    monkeypatch.setattr(impact_route, "get_provider", _fail_get_provider)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/impact",
        json={"revision_id": REVISION_ID, "symbol_id": str(target.id)},
    )
    assert response.status_code == 200
    assert response.json()["analysis_status"] == "deterministic"
    assert response.json()["explanation"] is None


def test_explanation_requested_without_provider_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    target = SymbolView(
        id=uuid.uuid4(),
        qualified_name="a.b",
        kind="function",
        file_id=uuid.uuid4(),
        file_path="a.py",
        parent_symbol_id=None,
    )
    monkeypatch.setattr(
        impact_route,
        "run_impact_analysis_by_symbol_id",
        lambda *a, **k: _completed_run_result(target),
    )
    monkeypatch.setattr(impact_route, "get_provider", lambda: None)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/impact",
        json={"revision_id": REVISION_ID, "symbol_id": str(target.id), "include_explanation": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["analysis_status"] == "deterministic_only"
    assert body["explanation"] is None


def test_explanation_requested_with_misconfigured_provider_still_returns_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_provider() raising AIConfigurationError (a key is present but
    ai_provider is unsupported) must degrade exactly like "not configured
    at all" from the API consumer's perspective — never a 500, never a
    lost deterministic result (Task 5/10)."""
    target = SymbolView(
        id=uuid.uuid4(),
        qualified_name="a.b",
        kind="function",
        file_id=uuid.uuid4(),
        file_path="a.py",
        parent_symbol_id=None,
    )
    monkeypatch.setattr(
        impact_route,
        "run_impact_analysis_by_symbol_id",
        lambda *a, **k: _completed_run_result(target),
    )

    def _raise_configuration_error() -> None:
        raise AIConfigurationError("unsupported ai_provider")

    monkeypatch.setattr(impact_route, "get_provider", _raise_configuration_error)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/impact",
        json={"revision_id": REVISION_ID, "symbol_id": str(target.id), "include_explanation": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["analysis_status"] == "deterministic_only"
    assert body["explanation"] is None
    assert body["target_qualified_name"] == "a.b"  # deterministic payload intact


def test_explanation_provider_success_is_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    target = SymbolView(
        id=uuid.uuid4(),
        qualified_name="a.b",
        kind="function",
        file_id=uuid.uuid4(),
        file_path="a.py",
        parent_symbol_id=None,
    )
    monkeypatch.setattr(
        impact_route,
        "run_impact_analysis_by_symbol_id",
        lambda *a, **k: _completed_run_result(target),
    )
    monkeypatch.setattr(impact_route, "get_provider", lambda: object())  # any non-None sentinel

    intelligence = ImpactIntelligenceResult(
        analysis_status="explained",
        deterministic_result=_completed_run_result(target).result,  # type: ignore[arg-type]
        explanation=ImpactExplanation(
            summary="s [E1]",
            why_it_matters="w",
            affected_areas=["x"],
            review_recommendations=["r"],
            test_recommendations=["t"],
            evidence=["E1"],
            limitations="l",
        ),
        evidence_map={"E1": ResolvedEvidence(file_path="a.py", line_start=1, line_end=2)},
    )
    monkeypatch.setattr(impact_route, "generate_impact_explanation", lambda *a, **k: intelligence)

    response = client.post(
        f"/repositories/{REPOSITORY_ID}/impact",
        json={"revision_id": REVISION_ID, "symbol_id": str(target.id), "include_explanation": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["analysis_status"] == "explained"
    assert body["explanation"]["summary"] == "s [E1]"
    assert body["evidence"]["E1"]["file_path"] == "a.py"


def test_explanation_provider_failure_still_returns_deterministic_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = SymbolView(
        id=uuid.uuid4(),
        qualified_name="a.b",
        kind="function",
        file_id=uuid.uuid4(),
        file_path="a.py",
        parent_symbol_id=None,
    )
    run_result = _completed_run_result(target)
    monkeypatch.setattr(
        impact_route, "run_impact_analysis_by_symbol_id", lambda *a, **k: run_result
    )
    monkeypatch.setattr(impact_route, "get_provider", lambda: object())
    fallback = ImpactIntelligenceResult(
        analysis_status="deterministic_only",
        deterministic_result=run_result.result,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(impact_route, "generate_impact_explanation", lambda *a, **k: fallback)

    response = client.post(
        f"/repositories/{REPOSITORY_ID}/impact",
        json={"revision_id": REVISION_ID, "symbol_id": str(target.id), "include_explanation": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["analysis_status"] == "deterministic_only"
    assert body["explanation"] is None
    # The deterministic payload is still fully present.
    assert body["target_qualified_name"] == "a.b"


def test_completed_result_is_serialized_not_an_orm_object(monkeypatch: pytest.MonkeyPatch) -> None:
    target = SymbolView(
        id=uuid.uuid4(),
        qualified_name="a.b",
        kind="function",
        file_id=uuid.uuid4(),
        file_path="a.py",
        parent_symbol_id=None,
    )
    result = ImpactAnalysisResult(
        revision_id=uuid.UUID(REVISION_ID),
        target_symbol_id=target.id,
        target_symbol=target,
        direct_impacts=(),
        transitive_impacts=(),
        impacted_files=(),
        max_depth=0,
    )
    run_result = ImpactAnalysisRunResult(
        status="completed", result=result, analysis_run_id=uuid.uuid4()
    )
    monkeypatch.setattr(
        impact_route, "run_impact_analysis_by_symbol_id", lambda *a, **k: run_result
    )
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/impact",
        json={"revision_id": REVISION_ID, "symbol_id": str(target.id)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["target_qualified_name"] == "a.b"
    assert body["direct_impacts"] == []
    assert isinstance(body["target_symbol_id"], str)  # plain string, not an ORM repr
