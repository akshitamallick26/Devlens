"""Deployment-safety configuration: public_demo is the default and the safe
mode, AI and generated-test execution are opt-in, and contradictory or
malformed configuration is a startup error rather than a silent fallback.
See docs/architecture/deployment-safety.md. No database needed: service
functions are monkeypatched, same convention as tests/*/test_api.py.
"""

import json
import uuid
from collections.abc import Callable, Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.api.routes.testing as testing_route
from app.ai.embedding_provider import OpenAIEmbeddingProvider, get_embedding_provider
from app.ai.provider import AnthropicProvider, get_provider
from app.api.rate_limit import get_rate_limiter
from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.investigation.models import MAX_GRAPH_CAMERA_STATE_BYTES
from app.main import app
from app.testing import sandbox
from app.testing.service import TestGenerationRunResult

SECRET = "sk-ant-THIS-MUST-NEVER-APPEAR-IN-A-RESPONSE"
REPOSITORY_ID = str(uuid.uuid4())
REVISION_ID = str(uuid.uuid4())

# The suite's development configuration (tests/conftest.py), removed so
# Settings falls back to its own defaults.
_UNSET_SUITE_CONFIG: dict[str, str | None] = {
    "DEPLOYMENT_MODE": None,
    "TEST_EXECUTION_ENABLED": None,
    "RATE_LIMIT_ENABLED": None,
}
PUBLIC_DEMO: dict[str, str | None] = {**_UNSET_SUITE_CONFIG, "DEPLOYMENT_MODE": "public_demo"}

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated() -> Generator[None]:
    app.dependency_overrides[get_db] = lambda: iter([None])
    get_rate_limiter().reset()
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)
        get_rate_limiter().reset()


def _defaults(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Settings:
    """Settings with no suite environment and no .env file."""
    for name in _UNSET_SUITE_CONFIG:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, **kwargs)  # type: ignore[call-arg]


# ---- Settings: safe defaults, fail closed ----------------------------------


def test_default_mode_is_public_demo_with_everything_dangerous_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _defaults(monkeypatch)
    assert settings.deployment_mode == "public_demo"
    assert not settings.ai_features_allowed
    assert not settings.test_execution_allowed
    assert settings.rate_limiting_active


def test_a_configured_key_alone_does_not_enable_ai_in_public_demo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _defaults(monkeypatch, ai_api_key=SECRET, embedding_api_key=SECRET)
    assert not settings.ai_features_allowed
    assert get_provider(settings) is None
    assert get_embedding_provider(settings) is None


def test_public_demo_ai_is_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _defaults(
        monkeypatch, ai_api_key=SECRET, embedding_api_key=SECRET, public_demo_ai_enabled=True
    )
    assert isinstance(get_provider(settings), AnthropicProvider)
    assert isinstance(get_embedding_provider(settings), OpenAIEmbeddingProvider)
    # Opting in to AI never opts in to execution.
    assert not settings.test_execution_allowed


@pytest.mark.parametrize("mode", ["production", "Public_Demo", "dev", ""])
def test_unrecognized_mode_is_a_startup_error_not_a_fallback(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("DEPLOYMENT_MODE", mode)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_execution_cannot_be_enabled_in_public_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="TEST_EXECUTION_ENABLED"):
        _defaults(monkeypatch, test_execution_enabled=True)


def test_rate_limiting_cannot_be_disabled_in_public_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="RATE_LIMIT_ENABLED"):
        _defaults(monkeypatch, rate_limit_enabled=False)


@pytest.mark.parametrize("variable", ["PUBLIC_DEMO_AI_ENABLED", "TEST_EXECUTION_ENABLED"])
def test_malformed_boolean_is_a_startup_error(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    for name in _UNSET_SUITE_CONFIG:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEPLOYMENT_MODE", "development")
    monkeypatch.setenv(variable, "maybe")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "field",
    [
        "rate_limit_window_seconds",
        "rate_limit_ingest_per_client",
        "rate_limit_analysis_global",
        "ingestion_max_files",
        "ingestion_max_total_bytes",
        "testing_execution_timeout_seconds",
        "security_semgrep_timeout_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_limits_are_rejected(
    monkeypatch: pytest.MonkeyPatch, field: str, value: int
) -> None:
    with pytest.raises(ValidationError):
        _defaults(monkeypatch, **{field: value})


def test_development_keeps_existing_behavior_with_execution_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _defaults(monkeypatch, deployment_mode="development", ai_api_key=SECRET)
    assert isinstance(get_provider(settings), AnthropicProvider)
    assert not settings.test_execution_allowed
    assert not settings.rate_limiting_active

    opted_in = _defaults(
        monkeypatch,
        deployment_mode="development",
        test_execution_enabled=True,
        rate_limit_enabled=True,
    )
    assert opted_in.test_execution_allowed
    assert opted_in.rate_limiting_active


# ---- Generated-test execution: rejected server-side -------------------------


def _record_service_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_generate(_db: object, llm_provider: object, *args: object, **kwargs: Any) -> Any:
        calls.append({"llm_provider": llm_provider, **kwargs})
        return TestGenerationRunResult(
            repository_id=uuid.UUID(REPOSITORY_ID),
            revision_id=uuid.UUID(REVISION_ID),
            analysis_run_id=None,
            analysis_status="no_provider",
            outcomes=(),
        )

    monkeypatch.setattr(testing_route, "generate_tests_for_target", fake_generate)
    return calls


def _generate(execute: bool) -> Any:
    return client.post(
        f"/repositories/{REPOSITORY_ID}/testing/generate",
        json={"revision_id": REVISION_ID, "execute": execute},
    )


@pytest.mark.parametrize(
    "config",
    [
        PUBLIC_DEMO,
        {**PUBLIC_DEMO, "PUBLIC_DEMO_AI_ENABLED": "true"},
        {"DEPLOYMENT_MODE": "development", "TEST_EXECUTION_ENABLED": None},
        {"DEPLOYMENT_MODE": "development", "TEST_EXECUTION_ENABLED": "false"},
    ],
)
def test_execute_is_rejected_with_a_typed_403_before_any_generation(
    monkeypatch: pytest.MonkeyPatch,
    override_settings: Callable[..., None],
    config: dict[str, str | None],
) -> None:
    override_settings(**config, AI_API_KEY=SECRET)
    calls = _record_service_calls(monkeypatch)

    def no_provider_lookup(*args: object, **kwargs: object) -> None:
        raise AssertionError("provider looked up for a rejected request")

    monkeypatch.setattr(testing_route, "get_provider", no_provider_lookup)

    response = _generate(execute=True)

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "capability_disabled"
    assert detail["capability"] == "test_execution"
    assert detail["message"]
    assert calls == []


def test_generation_without_execution_still_works_in_public_demo_without_ai(
    monkeypatch: pytest.MonkeyPatch, override_settings: Callable[..., None]
) -> None:
    override_settings(**PUBLIC_DEMO, AI_API_KEY=SECRET)
    calls = _record_service_calls(monkeypatch)

    response = _generate(execute=False)

    assert response.status_code == 200
    assert response.json()["analysis_status"] == "no_provider"
    assert len(calls) == 1
    assert calls[0]["llm_provider"] is None  # key configured, AI off by policy
    assert calls[0]["execute"] is False


def test_execute_reaches_the_service_when_development_opts_in(
    monkeypatch: pytest.MonkeyPatch, override_settings: Callable[..., None]
) -> None:
    override_settings(DEPLOYMENT_MODE="development", TEST_EXECUTION_ENABLED="true")
    calls = _record_service_calls(monkeypatch)

    response = _generate(execute=True)

    assert response.status_code == 200
    assert calls[0]["execute"] is True


def test_sandbox_refuses_before_writing_or_spawning_anything(
    monkeypatch: pytest.MonkeyPatch, override_settings: Callable[..., None]
) -> None:
    override_settings(**PUBLIC_DEMO)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("sandbox touched the filesystem or started a process")

    monkeypatch.setattr("app.testing.sandbox.tempfile.mkdtemp", forbidden)
    monkeypatch.setattr("app.testing.sandbox.subprocess.run", forbidden)
    test_file = sandbox.MaterializedFile("test_x.py", "def test_x():\n    assert True\n")

    with pytest.raises(sandbox.ExecutionDisabledError):
        sandbox.run_generated_test([], test_file)


def test_sandbox_refuses_in_development_without_opt_in(
    override_settings: Callable[..., None],
) -> None:
    override_settings(DEPLOYMENT_MODE="development", TEST_EXECUTION_ENABLED=None)
    test_file = sandbox.MaterializedFile("test_x.py", "def test_x():\n    assert True\n")
    with pytest.raises(sandbox.ExecutionDisabledError):
        sandbox.run_generated_test([], test_file)


# ---- Investigation write bounds -------------------------------------------------

CONTEXT_PATH = f"/repositories/{REPOSITORY_ID}/sessions/not-a-uuid/context"


def _camera_state_of_size(size: int) -> dict[str, object]:
    """A graph_camera_state that measures exactly `size` bytes as the validator
    measures it (UTF-8 of json.dumps with default separators)."""
    state: dict[str, object] = {"focus_symbol_id": ""}
    overhead = len(json.dumps(state, ensure_ascii=False).encode("utf-8"))
    state["focus_symbol_id"] = "x" * (size - overhead)
    return state


def test_oversized_graph_camera_state_is_rejected_before_the_handler() -> None:
    response = client.post(
        CONTEXT_PATH,
        json={
            "ref_type": "graph_focus",
            "graph_camera_state": _camera_state_of_size(MAX_GRAPH_CAMERA_STATE_BYTES + 1),
        },
    )
    assert response.status_code == 422
    assert "graph_camera_state" in str(response.json()["detail"])


def test_graph_camera_state_at_the_limit_reaches_the_handler() -> None:
    state = _camera_state_of_size(MAX_GRAPH_CAMERA_STATE_BYTES)
    response = client.post(
        CONTEXT_PATH, json={"ref_type": "graph_focus", "graph_camera_state": state}
    )
    # Past body validation: the handler's own id check answers.
    assert response.status_code == 422
    assert response.json()["detail"] == "invalid session_id"


def test_the_workstation_camera_state_is_far_below_the_limit() -> None:
    """The exact shape SaveToSession.tsx sends."""
    state = {
        "focus_symbol_id": str(uuid.uuid4()),
        "direction": "dependents",
        "depth": 3,
        "revision_id": str(uuid.uuid4()),
    }
    assert len(json.dumps(state).encode("utf-8")) < MAX_GRAPH_CAMERA_STATE_BYTES // 10


# ---- Health and capabilities -------------------------------------------------


def test_health_works_in_public_demo(override_settings: Callable[..., None]) -> None:
    override_settings(**PUBLIC_DEMO)
    for _ in range(50):  # never rate limited
        response = client.get("/health")
        assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_capabilities_in_public_demo_report_policy_not_keys(
    override_settings: Callable[..., None],
) -> None:
    override_settings(**PUBLIC_DEMO, AI_API_KEY=SECRET, EMBEDDING_API_KEY=SECRET)

    response = client.get("/capabilities")

    assert response.status_code == 200
    assert SECRET not in response.text
    body = response.json()
    assert body["deployment_mode"] == "public_demo"
    assert body["ai_explanations"]["status"] == "disabled"
    assert body["embeddings"]["status"] == "disabled"
    assert body["test_execution"]["status"] == "disabled"
    assert body["rate_limiting"]["active"] is True
    assert set(body["rate_limiting"]["buckets"]) == {"ingest", "pipeline", "analysis", "session"}


def test_capabilities_in_development(override_settings: Callable[..., None]) -> None:
    override_settings(
        DEPLOYMENT_MODE="development",
        TEST_EXECUTION_ENABLED="true",
        AI_API_KEY=SECRET,
        EMBEDDING_API_KEY=None,
    )
    body = client.get("/capabilities").json()
    assert body["ai_explanations"]["status"] == "enabled"
    assert body["embeddings"]["status"] == "not_configured"
    assert body["test_execution"]["status"] == "enabled"
    assert body["rate_limiting"]["active"] is False


def test_suite_runs_under_the_explicit_development_configuration() -> None:
    settings = get_settings()
    assert settings.deployment_mode == "development"
    assert settings.test_execution_allowed
    assert not settings.rate_limiting_active
