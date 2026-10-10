"""End to end under DEPLOYMENT_MODE=public_demo, through the real HTTP
routes and a real ingested git fixture repository: the deterministic core
(analysis pipeline, impact analysis, lexical Q&A retrieval) works unchanged,
while AI providers are never called even though keys are configured, and
generated-test execution is refused. Requires live PostgreSQL (same
`db_session` fixture and explicit skip as every other DB suite).
"""

import uuid
from collections.abc import Callable, Generator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.rate_limit import get_rate_limiter
from app.db.session import get_db
from app.main import app
from tests.automation.conftest import ingest_revision

KEY = "sk-test-configured-but-disabled-by-policy"


@pytest.fixture
def public_demo_client(
    db_session: Session,
    override_settings: Callable[..., None],
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[TestClient]:
    override_settings(
        DEPLOYMENT_MODE="public_demo",
        TEST_EXECUTION_ENABLED=None,
        RATE_LIMIT_ENABLED=None,
        AI_API_KEY=KEY,
        EMBEDDING_API_KEY=KEY,
    )

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("an AI provider was called in public_demo")

    monkeypatch.setattr(httpx, "post", no_network)

    def _live() -> Generator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _live
    get_rate_limiter().reset()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)
        get_rate_limiter().reset()


def test_deterministic_analysis_works_and_ai_stays_off(
    db_session: Session, automation_fixture_repo: Path, public_demo_client: TestClient
) -> None:
    ingested = ingest_revision(db_session, automation_fixture_repo, owner="public-demo")
    base = f"/repositories/{ingested.repository_id}"

    pipeline = public_demo_client.post(f"{base}/revisions/{ingested.revision_id}/analyze")
    assert pipeline.status_code == 200
    stages = {s["stage"]: s for s in pipeline.json()["stages"]}
    for stage in ("parsing", "graph", "rag_chunking"):
        assert stages[stage]["status"] == "completed"
    assert stages["rag_embedding"]["status"] == "unavailable"  # key set, AI off by policy

    impact = public_demo_client.post(
        f"{base}/impact",
        json={
            "revision_id": ingested.revision_id,
            "qualified_name": "app.math_utils.add",
            "include_explanation": True,
        },
    )
    assert impact.status_code == 200
    body = impact.json()
    assert body["analysis_status"] == "deterministic_only"
    assert body["explanation"] is None
    # The module imports `add` and `compute` calls it: both depend on it.
    direct = {s["qualified_name"] for s in body["direct_impacts"]}
    assert direct == {"app.caller", "app.caller.compute"}

    qa = public_demo_client.post(
        f"{base}/qa", json={"revision_id": ingested.revision_id, "question": "where is add used"}
    )
    assert qa.status_code == 200
    assert qa.json()["analysis_status"] == "retrieval_only"
    assert qa.json()["evidence_items"]

    execute = public_demo_client.post(
        f"{base}/testing/generate",
        json={"revision_id": ingested.revision_id, "execute": True},
    )
    assert execute.status_code == 403
    assert execute.json()["detail"]["capability"] == "test_execution"


def test_unknown_revision_semantics_are_unchanged(
    db_session: Session, automation_fixture_repo: Path, public_demo_client: TestClient
) -> None:
    ingested = ingest_revision(db_session, automation_fixture_repo, owner="public-demo-404")
    response = public_demo_client.post(
        f"/repositories/{ingested.repository_id}/impact",
        json={"revision_id": str(uuid.uuid4()), "qualified_name": "app.math_utils.add"},
    )
    assert response.status_code == 404


def test_investigation_session_writes_work_within_the_limits(
    db_session: Session, automation_fixture_repo: Path, public_demo_client: TestClient
) -> None:
    ingested = ingest_revision(db_session, automation_fixture_repo, owner="public-demo-session")
    base = f"/repositories/{ingested.repository_id}/sessions"

    created = public_demo_client.post(
        base, json={"revision_id": ingested.revision_id, "title": "Where is add used?"}
    )
    assert created.status_code == 200
    session = f"{base}/{created.json()['id']}"

    message = public_demo_client.post(
        f"{session}/messages", json={"role": "user", "content": "start from add"}
    )
    assert message.status_code == 200

    # The exact graph_camera_state shape the workstation sends.
    context = public_demo_client.post(
        f"{session}/context",
        json={
            "ref_type": "graph_focus",
            "graph_camera_state": {
                "focus_symbol_id": str(uuid.uuid4()),
                "direction": "dependents",
                "depth": 2,
                "revision_id": ingested.revision_id,
            },
        },
    )
    assert context.status_code == 200

    renamed = public_demo_client.patch(session, json={"title": "add: dependents"})
    assert renamed.status_code == 200
    removed = public_demo_client.delete(f"{session}/context/{context.json()['id']}")
    assert removed.status_code == 204

    for _ in range(3):  # reads are never limited
        assert public_demo_client.get(session).status_code == 200
