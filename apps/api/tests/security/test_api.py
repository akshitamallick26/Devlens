"""API-boundary tests for the Security Intelligence route: request
validation and response serialization. The underlying service function is
monkeypatched — no database needed, same convention as
tests/{impact,rag,testing}/test_api.py.
"""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

import app.api.routes.security as security_route
from app.db.session import get_db
from app.main import app
from app.security.models import FindingView
from app.security.service import (
    FindingResult,
    RevisionNotFoundError,
    ScannerStatusSummary,
    SecurityAnalysisResult,
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


def test_invalid_repository_uuid_is_422() -> None:
    response = client.post(
        "/repositories/not-a-uuid/security/analyze", json={"revision_id": REVISION_ID}
    )
    assert response.status_code == 422


def test_invalid_revision_uuid_is_422() -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/security/analyze", json={"revision_id": "not-a-uuid"}
    )
    assert response.status_code == 422


def test_unknown_scanner_name_is_422() -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/security/analyze",
        json={"revision_id": REVISION_ID, "scanners": ["not-a-real-scanner"]},
    )
    assert response.status_code == 422


def test_max_depth_over_the_limit_is_422() -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/security/analyze",
        json={"revision_id": REVISION_ID, "max_depth": 9999},
    )
    assert response.status_code == 422


def test_max_symbols_over_the_limit_is_422() -> None:
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/security/analyze",
        json={"revision_id": REVISION_ID, "max_symbols": 999999},
    )
    assert response.status_code == 422


def test_revision_not_found_maps_to_404(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise RevisionNotFoundError("no such revision")

    monkeypatch.setattr(security_route, "run_security_analysis", _raise)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/security/analyze", json={"revision_id": REVISION_ID}
    )
    assert response.status_code == 404


def test_no_findings_result_is_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    result = SecurityAnalysisResult(
        repository_id=uuid.UUID(REPOSITORY_ID),
        revision_id=uuid.UUID(REVISION_ID),
        analysis_run_id=uuid.uuid4(),
        analysis_status="completed",
        scanner_results=(
            ScannerStatusSummary(
                scanner="osv-scanner",
                status="completed",
                detail=None,
                finding_count=0,
                observation_count=0,
                scanner_version=None,
            ),
        ),
        findings=(),
        limitations=("no findings does not mean the repository is secure",),
    )
    monkeypatch.setattr(security_route, "run_security_analysis", lambda *a, **k: result)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/security/analyze", json={"revision_id": REVISION_ID}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["analysis_status"] == "completed"
    assert body["scanner_results"][0]["status"] == "completed"
    assert body["findings"] == []
    assert body["limitations"]


def test_scanner_unavailable_is_distinguished_from_no_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SecurityAnalysisResult(
        repository_id=uuid.UUID(REPOSITORY_ID),
        revision_id=uuid.UUID(REVISION_ID),
        analysis_run_id=uuid.uuid4(),
        analysis_status="completed",
        scanner_results=(
            ScannerStatusSummary(
                scanner="semgrep",
                status="unavailable",
                detail="semgrep binary not found on PATH",
                finding_count=0,
                observation_count=0,
                scanner_version=None,
            ),
        ),
        findings=(),
        limitations=(),
    )
    monkeypatch.setattr(security_route, "run_security_analysis", lambda *a, **k: result)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/security/analyze", json={"revision_id": REVISION_ID}
    )
    body = response.json()
    assert body["scanner_results"][0]["status"] == "unavailable"
    assert body["scanner_results"][0]["status"] != "completed"


def test_finding_result_is_serialized_not_an_orm_object(monkeypatch: pytest.MonkeyPatch) -> None:
    finding = FindingView(
        id=uuid.uuid4(),
        analyzer="osv-scanner",
        rule_id="GHSA-test",
        severity="high",
        title="Vulnerable dependency",
        message="details",
        status="open",
        cvss_score=None,
        cvss_vector=None,
        scanner_confidence=None,
        references=("https://example.com",),
    )
    finding_result = FindingResult(
        finding=finding, observations=(), blast_radius=None, explanation=None
    )
    result = SecurityAnalysisResult(
        repository_id=uuid.UUID(REPOSITORY_ID),
        revision_id=uuid.UUID(REVISION_ID),
        analysis_run_id=uuid.uuid4(),
        analysis_status="completed",
        scanner_results=(),
        findings=(finding_result,),
        limitations=(),
    )
    monkeypatch.setattr(security_route, "run_security_analysis", lambda *a, **k: result)
    response = client.post(
        f"/repositories/{REPOSITORY_ID}/security/analyze", json={"revision_id": REVISION_ID}
    )
    assert response.status_code == 200
    body = response.json()
    item = body["findings"][0]["finding"]
    assert isinstance(item["finding_id"], str)  # never a raw UUID/ORM object
    assert item["severity"] == "high"
    assert item["rule_id"] == "GHSA-test"
