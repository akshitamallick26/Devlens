"""Testing Intelligence v1.2: generated-test provenance. Live PostgreSQL
(same `db_session` fixture and explicit skip as every DB suite); no real
LLM call anywhere: the real AnthropicProvider is exercised only through a
monkeypatched `httpx.post`.

Contract: every persisted GeneratedTest is provider-generated (there is no
deterministic fallback that writes one), and its provider/model_name are
the identity of the provider object that generated it, never caller-
supplied configuration. Unknown provenance is NULL, never "": the provider
reports no model version, so model_version is NULL. No provider, a
provider failure, malformed provider output, or a provider that cannot
identify itself persists nothing. Execution never rewrites provenance."""

import json
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.ai.models import AIProviderUnavailableError, AIResponseValidationError
from app.api.routes import testing as testing_route
from app.codeintel.service import list_generated_tests
from app.core.config import Settings
from app.db.session import get_db
from app.main import app
from app.models import AnalysisRun, GeneratedTest, TestExecution
from app.testing import persistence
from app.testing.generation import GeneratedTestResponse
from app.testing.service import ProviderIdentityError, generate_tests_for_target
from tests.ai.fakes import FakeProvider
from tests.testing.conftest import ingest_parse_and_graph, symbol_id

TARGET = "checkout.pricing.apply_discount"
SECRET_KEY = "sk-provenance-secret-do-not-persist"
ANTHROPIC_MODEL = "claude-provenance-test-model"


def _valid_response() -> GeneratedTestResponse:
    return GeneratedTestResponse(
        file_path="test_apply_discount.py",
        source=(
            "from checkout.pricing import apply_discount\n\n\n"
            "def test_apply_discount():\n"
            "    assert apply_discount(100) == 90\n"
        ),
        rationale="Exercises the target directly [T1].",
        cited_evidence_ids=["T1"],
    )


def _ids(db: Session, repo: Path, owner: str = "provenance") -> tuple[uuid.UUID, uuid.UUID]:
    result = ingest_parse_and_graph(db, repo, owner=owner)
    return uuid.UUID(result.repository_id), uuid.UUID(result.revision_id)


def _generate(
    db: Session, provider: Any, repository_id: uuid.UUID, revision_id: uuid.UUID, **kwargs: Any
) -> Any:
    return generate_tests_for_target(
        db,
        provider,
        repository_id,
        revision_id,
        target_symbol_id=symbol_id(db, revision_id, TARGET),
        **kwargs,
    )


def _reloaded(db: Session, generated_test_id: uuid.UUID | None) -> tuple[Any, ...]:
    """Provenance as stored in PostgreSQL, not as held in the identity map."""
    assert generated_test_id is not None
    db.expire_all()
    row = db.execute(
        select(GeneratedTest.provider, GeneratedTest.model_name, GeneratedTest.model_version).where(
            GeneratedTest.id == generated_test_id
        )
    ).one()
    return tuple(row)


def _counts(db: Session, repository_id: uuid.UUID) -> tuple[int, int]:
    tests = db.execute(
        select(func.count()).where(GeneratedTest.repository_id == repository_id)
    ).scalar_one()
    runs = db.execute(
        select(func.count())
        .where(AnalysisRun.repository_id == repository_id)
        .where(AnalysisRun.analysis_type == "test_generation")
    ).scalar_one()
    return tests, runs


# ---- Provider-generated: provenance is the generating provider's identity ----


def test_provider_identity_is_persisted_exactly_and_unknown_version_is_null(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    repository_id, revision_id = _ids(db_session, testing_fixture_repo)
    provider = FakeProvider(
        response=_valid_response(), provider_name="acme-ai", model_name="acme-model-7"
    )
    result = _generate(db_session, provider, repository_id, revision_id)
    outcome = result.outcomes[0]
    assert outcome.status == "ready"

    assert _reloaded(db_session, outcome.generated_test_id) == ("acme-ai", "acme-model-7", None)
    run = db_session.get(AnalysisRun, result.analysis_run_id)
    assert run is not None
    assert run.config == {
        "provider": "acme-ai",
        "model_name": "acme-model-7",
        "model_version": None,
        "execute": False,
    }


def test_validation_failed_tests_still_record_who_generated_them(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    repository_id, revision_id = _ids(db_session, testing_fixture_repo)
    bad = _valid_response().model_copy(update={"file_path": "../escape.py"})
    provider = FakeProvider(response=bad, provider_name="acme-ai", model_name="acme-model-7")
    outcome = _generate(db_session, provider, repository_id, revision_id).outcomes[0]
    assert outcome.status == "validation_failed"
    assert _reloaded(db_session, outcome.generated_test_id) == ("acme-ai", "acme-model-7", None)


def test_execution_does_not_mutate_provenance(
    db_session: Session, testing_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_id, revision_id = _ids(db_session, testing_fixture_repo)
    before_execution: list[tuple[Any, ...]] = []
    original = persistence.record_execution

    def _recording(db: Session, generated_test: GeneratedTest, *args: Any) -> TestExecution:
        db.flush()
        before_execution.append(_reloaded(db, generated_test.id))
        return original(db, generated_test, *args)

    monkeypatch.setattr(persistence, "record_execution", _recording)
    provider = FakeProvider(
        response=_valid_response(), provider_name="acme-ai", model_name="acme-model-7"
    )
    outcome = _generate(db_session, provider, repository_id, revision_id, execute=True).outcomes[0]
    assert outcome.status == "passed" and outcome.execution_id is not None
    assert before_execution == [("acme-ai", "acme-model-7", None)]
    assert _reloaded(db_session, outcome.generated_test_id) == before_execution[0]


# ---- Nothing provider-generated: nothing persisted ----


def test_no_provider_persists_nothing(db_session: Session, testing_fixture_repo: Path) -> None:
    repository_id, revision_id = _ids(db_session, testing_fixture_repo)
    result = _generate(db_session, None, repository_id, revision_id)
    assert result.analysis_status == "no_provider"
    assert [o.generated_test_id for o in result.outcomes] == [None]
    assert _counts(db_session, repository_id) == (0, 0)


@pytest.mark.parametrize(
    "error",
    [
        AIProviderUnavailableError("request failed"),
        AIResponseValidationError("response did not match schema"),
    ],
)
def test_provider_failure_or_malformed_output_persists_no_test(
    db_session: Session, testing_fixture_repo: Path, error: Exception
) -> None:
    repository_id, revision_id = _ids(db_session, testing_fixture_repo)
    provider = FakeProvider(error=error, provider_name="acme-ai", model_name="acme-model-7")
    outcome = _generate(db_session, provider, repository_id, revision_id).outcomes[0]
    assert outcome.status == "generation_failed"
    assert outcome.generated_test_id is None
    assert _counts(db_session, repository_id)[0] == 0


class _AnonymousProvider:
    """Generates fine, but does not say who it is."""

    def generate_structured(self, **_: Any) -> BaseModel:
        return _valid_response()


@pytest.mark.parametrize(
    "provider",
    [
        FakeProvider(response=_valid_response(), provider_name="", model_name="acme-model-7"),
        FakeProvider(response=_valid_response(), provider_name="acme-ai", model_name=""),
        FakeProvider(response=_valid_response(), provider_name="  ", model_name="\t"),
        _AnonymousProvider(),
    ],
)
def test_a_provider_without_identity_persists_nothing(
    db_session: Session, testing_fixture_repo: Path, provider: Any
) -> None:
    repository_id, revision_id = _ids(db_session, testing_fixture_repo)
    with pytest.raises(ProviderIdentityError):
        _generate(db_session, provider, repository_id, revision_id)
    assert _counts(db_session, repository_id) == (0, 0)


def test_database_rejects_empty_string_provenance(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    repository_id, revision_id = _ids(db_session, testing_fixture_repo)
    outcome = _generate(
        db_session, FakeProvider(response=_valid_response()), repository_id, revision_id
    ).outcomes[0]
    for column in ("provider", "model_name", "model_version"):
        with pytest.raises(IntegrityError, match="provenance_not_empty"):
            with db_session.begin_nested():
                db_session.execute(
                    GeneratedTest.__table__.update()
                    .where(GeneratedTest.id == outcome.generated_test_id)
                    .values({column: ""})
                )
    assert _reloaded(db_session, outcome.generated_test_id) == ("fake", "fake-model", None)


# ---- Isolation and readback ----


def test_provenance_is_per_row_and_readback_is_revision_scoped(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    repo_a, rev_a = _ids(db_session, testing_fixture_repo, owner="prov-a")
    repo_b, rev_b = _ids(db_session, testing_fixture_repo, owner="prov-b")
    _generate(
        db_session,
        FakeProvider(response=_valid_response(), provider_name="p-a", model_name="m-a"),
        repo_a,
        rev_a,
    )
    _generate(
        db_session,
        FakeProvider(response=_valid_response(), provider_name="p-b", model_name="m-b"),
        repo_b,
        rev_b,
    )
    db_session.expire_all()
    page_a = list_generated_tests(db_session, repo_a, rev_a)
    page_b = list_generated_tests(db_session, repo_b, rev_b)
    assert [(t.provider, t.model_name) for t in page_a.tests] == [("p-a", "m-a")]
    assert [(t.provider, t.model_name) for t in page_b.tests] == [("p-b", "m-b")]


# ---- HTTP: the real AnthropicProvider, configured from settings ----


@pytest.fixture
def anthropic_client(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[TestClient, list[dict[str, Any]]]]:
    """The generate route with an Anthropic key configured. `httpx.post`
    returns whatever the test queues; every outgoing request is recorded."""
    settings = Settings(ai_provider="anthropic", ai_model=ANTHROPIC_MODEL, ai_api_key=SECRET_KEY)
    monkeypatch.setattr(testing_route, "get_settings", lambda: settings)
    sent: list[dict[str, Any]] = []
    queued: list[str] = []

    def _post(url: str, **kwargs: Any) -> httpx.Response:
        sent.append(kwargs)
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": queued.pop(0)}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", _post)

    def _live() -> Generator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _live
    try:
        client = TestClient(app)
        client.queued = queued  # type: ignore[attr-defined]
        yield client, sent
    finally:
        app.dependency_overrides.pop(get_db, None)


def _post_generate(client: TestClient, repository_id: uuid.UUID, body: dict[str, Any]) -> Any:
    response = client.post(f"/repositories/{repository_id}/testing/generate", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_route_records_the_anthropic_model_it_called_and_reads_it_back(
    db_session: Session,
    testing_fixture_repo: Path,
    anthropic_client: tuple[TestClient, list[dict[str, Any]]],
) -> None:
    client, sent = anthropic_client
    repository_id, revision_id = _ids(db_session, testing_fixture_repo)
    target = symbol_id(db_session, revision_id, TARGET)
    client.queued.extend([_valid_response().model_dump_json()] * 5)  # type: ignore[attr-defined]
    body = _post_generate(
        client, repository_id, {"revision_id": str(revision_id), "symbol_ids": [str(target)]}
    )
    generated_ids = [o["generated_test_id"] for o in body["outcomes"] if o["generated_test_id"]]
    assert len(generated_ids) >= 1
    assert sent and all(request["json"]["model"] == ANTHROPIC_MODEL for request in sent)

    for generated_id in generated_ids:
        assert _reloaded(db_session, uuid.UUID(generated_id)) == (
            "anthropic",
            ANTHROPIC_MODEL,
            None,
        )
    run = db_session.get(AnalysisRun, uuid.UUID(body["analysis_run_id"]))
    assert run is not None
    stored = json.dumps(run.config) + repr(_reloaded(db_session, uuid.UUID(generated_ids[0])))
    assert SECRET_KEY not in stored and "x-api-key" not in stored

    readback = client.get(
        f"/repositories/{repository_id}/revisions/{revision_id}/testing/generated-tests"
    )
    assert readback.status_code == 200
    tests = readback.json()["tests"]
    assert {(t["provider"], t["model_name"]) for t in tests} == {("anthropic", ANTHROPIC_MODEL)}
    assert SECRET_KEY not in readback.text


@pytest.mark.parametrize("raw", ["not json at all", json.dumps({"file_path": "x.py"})])
def test_route_with_malformed_anthropic_output_persists_nothing(
    db_session: Session,
    testing_fixture_repo: Path,
    anthropic_client: tuple[TestClient, list[dict[str, Any]]],
    raw: str,
) -> None:
    client, _ = anthropic_client
    repository_id, revision_id = _ids(db_session, testing_fixture_repo)
    target = symbol_id(db_session, revision_id, TARGET)
    client.queued.extend([raw] * 10)  # type: ignore[attr-defined]
    body = _post_generate(
        client, repository_id, {"revision_id": str(revision_id), "symbol_ids": [str(target)]}
    )
    assert body["outcomes"]
    assert {o["status"] for o in body["outcomes"]} == {"generation_failed"}
    assert _counts(db_session, repository_id)[0] == 0
    assert SECRET_KEY not in json.dumps(body)
