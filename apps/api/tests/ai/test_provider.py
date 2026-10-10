"""Tests for AnthropicProvider's parsing/validation logic. `httpx.post` is
monkeypatched — no real network call is ever made (Task 14/19: no live LLM
calls in the normal test suite).
"""

import httpx
import pytest
from pydantic import BaseModel

from app.ai.models import (
    AIConfigurationError,
    AIProviderUnavailableError,
    AIResponseValidationError,
)
from app.ai.provider import AnthropicProvider, get_provider
from app.core.config import Settings


class _DummyResponse(BaseModel):
    value: str


def _fake_response(status_code: int, json_body: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_body,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )


def test_generate_structured_parses_valid_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _fake_response(
            200, {"content": [{"type": "text", "text": '{"value": "hello"}'}]}
        ),
    )
    provider = AnthropicProvider(api_key="k", model="m")
    result = provider.generate_structured(
        system_prompt="s", user_content="u", response_model=_DummyResponse
    )
    assert result.value == "hello"


def test_generate_structured_raises_on_malformed_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _fake_response(200, {"content": [{"type": "text", "text": "not json"}]}),
    )
    provider = AnthropicProvider(api_key="k", model="m")
    with pytest.raises(AIResponseValidationError):
        provider.generate_structured(
            system_prompt="s", user_content="u", response_model=_DummyResponse
        )


def test_generate_structured_raises_on_missing_required_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _fake_response(200, {"content": [{"type": "text", "text": "{}"}]}),
    )
    provider = AnthropicProvider(api_key="k", model="m")
    with pytest.raises(AIResponseValidationError):
        provider.generate_structured(
            system_prompt="s", user_content="u", response_model=_DummyResponse
        )


def test_generate_structured_raises_on_unexpected_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _fake_response(200, {"unexpected": "shape"}))
    provider = AnthropicProvider(api_key="k", model="m")
    with pytest.raises(AIResponseValidationError):
        provider.generate_structured(
            system_prompt="s", user_content="u", response_model=_DummyResponse
        )


def test_generate_structured_raises_on_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", _raise)
    provider = AnthropicProvider(api_key="k", model="m")
    with pytest.raises(AIProviderUnavailableError):
        provider.generate_structured(
            system_prompt="s", user_content="u", response_model=_DummyResponse
        )


def test_generate_structured_raises_on_non_2xx_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _fake_response(500, {"error": "boom"}))
    provider = AnthropicProvider(api_key="k", model="m")
    with pytest.raises(AIProviderUnavailableError):
        provider.generate_structured(
            system_prompt="s", user_content="u", response_model=_DummyResponse
        )


# ---- Task 4: exact request contract -----------------------------------------


def test_request_has_correct_auth_headers_and_model_and_separates_system_from_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _capture(*args: object, **kwargs: object) -> httpx.Response:
        captured.update(kwargs)
        return _fake_response(200, {"content": [{"type": "text", "text": '{"value": "ok"}'}]})

    monkeypatch.setattr(httpx, "post", _capture)
    provider = AnthropicProvider(api_key="sk-the-real-secret", model="claude-the-configured-model")
    provider.generate_structured(
        system_prompt="SYSTEM INSTRUCTION TEXT",
        user_content="UNTRUSTED REPOSITORY EVIDENCE TEXT",
        response_model=_DummyResponse,
    )

    headers = captured["headers"]
    assert headers["x-api-key"] == "sk-the-real-secret"
    assert headers["anthropic-version"]
    assert headers["content-type"] == "application/json"

    body = captured["json"]
    assert body["model"] == "claude-the-configured-model"  # taken from configuration, not hardcoded
    assert body["system"] == "SYSTEM INSTRUCTION TEXT"
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "UNTRUSTED REPOSITORY EVIDENCE TEXT"
    # The two must never be merged into one field — that's the whole
    # system/evidence separation this architecture depends on.
    assert "UNTRUSTED REPOSITORY EVIDENCE TEXT" not in body["system"]
    assert "SYSTEM INSTRUCTION TEXT" not in body["messages"][0]["content"]


def test_request_uses_bounded_explicit_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def _capture(*args: object, **kwargs: object) -> httpx.Response:
        captured.update(kwargs)
        return _fake_response(200, {"content": [{"type": "text", "text": '{"value": "ok"}'}]})

    monkeypatch.setattr(httpx, "post", _capture)
    provider = AnthropicProvider(api_key="k", model="m")
    provider.generate_structured(system_prompt="s", user_content="u", response_model=_DummyResponse)

    timeout = captured["timeout"]
    # An explicit httpx.Timeout, not an unbounded/None value, and not one
    # single uniform number either — see provider.py's _TIMEOUT comment.
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect is not None
    assert timeout.read is not None
    assert timeout.connect < timeout.read  # fail fast on connect, generous on generation


def test_provider_error_payload_does_not_become_a_successful_explanation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A realistic Anthropic error-shaped body (no "content" key at all)
    must fail closed, never be coerced into a response_model instance."""
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _fake_response(
            400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
        ),
    )
    provider = AnthropicProvider(api_key="k", model="m")
    with pytest.raises(AIProviderUnavailableError):
        provider.generate_structured(
            system_prompt="s", user_content="u", response_model=_DummyResponse
        )


def test_non_string_text_content_fails_closed_not_with_an_unhandled_typeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for a bug found during adversarial hardening: a
    provider response whose "text" field isn't a string used to crash with
    an un-typed TypeError from json.loads() instead of failing closed."""
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _fake_response(200, {"content": [{"type": "text", "text": 12345}]}),
    )
    provider = AnthropicProvider(api_key="k", model="m")
    with pytest.raises(AIResponseValidationError):
        provider.generate_structured(
            system_prompt="s", user_content="u", response_model=_DummyResponse
        )


# ---- get_provider() factory -------------------------------------------------


def test_get_provider_returns_none_without_api_key() -> None:
    settings = Settings(ai_api_key=None)
    assert get_provider(settings) is None


def test_get_provider_returns_anthropic_provider_when_configured() -> None:
    settings = Settings(ai_api_key="sk-test", ai_provider="anthropic", ai_model="claude-x")
    provider = get_provider(settings)
    assert isinstance(provider, AnthropicProvider)


def test_get_provider_never_makes_a_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError("network call made while merely constructing a provider")

    monkeypatch.setattr(httpx, "post", _fail)
    settings = Settings(ai_api_key="sk-test")
    get_provider(settings)  # must not raise


# ---- Task 11: secrets never leak via a raised exception ---------------------


@pytest.mark.parametrize(
    "status_code,json_body",
    [
        (401, {"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}),
        (403, {"type": "error", "error": {"type": "permission_error", "message": "forbidden"}}),
        (429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}),
        (500, {"type": "error", "error": {"type": "api_error", "message": "internal"}}),
    ],
)
def test_error_responses_never_leak_the_api_key_in_the_raised_exception(
    monkeypatch: pytest.MonkeyPatch, status_code: int, json_body: dict
) -> None:
    secret = "sk-ant-THIS-MUST-NEVER-APPEAR-IN-AN-ERROR-MESSAGE"
    repo_content = "UNTRUSTED-REPOSITORY-SOURCE-THAT-MUST-NEVER-LEAK-EITHER"
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _fake_response(status_code, json_body))
    provider = AnthropicProvider(api_key=secret, model="m")
    with pytest.raises(AIProviderUnavailableError) as exc_info:
        provider.generate_structured(
            system_prompt="s", user_content=repo_content, response_model=_DummyResponse
        )
    message = str(exc_info.value)
    assert secret not in message
    assert repo_content not in message


def test_connection_failure_never_leaks_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A realistic connection failure — httpx's own exception message is
    built from the failure reason, never from the request object, so it
    never contains headers/secrets regardless of what the caller passed."""
    secret = "sk-ant-THIS-MUST-NEVER-APPEAR-IN-AN-ERROR-MESSAGE"

    def _raise(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx, "post", _raise)
    provider = AnthropicProvider(api_key=secret, model="m")
    with pytest.raises(AIProviderUnavailableError) as exc_info:
        provider.generate_structured(
            system_prompt="s", user_content="u", response_model=_DummyResponse
        )
    assert secret not in str(exc_info.value)


def test_get_provider_raises_configuration_error_for_unsupported_provider_name() -> None:
    """A key IS present (the user clearly intends to use AI), but the
    provider name isn't one this codebase implements — this is a
    misconfiguration, not "AI simply isn't set up," so it's a typed,
    explicit error rather than a silent None (Task 5)."""
    settings = Settings(ai_api_key="sk-test", ai_provider="openai")
    with pytest.raises(AIConfigurationError):
        get_provider(settings)
