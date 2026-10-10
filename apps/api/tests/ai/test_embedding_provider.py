"""Tests for OpenAIEmbeddingProvider's request/parsing/validation logic.
`httpx.post` is monkeypatched — no real network call is ever made.
"""

import httpx
import pytest

from app.ai.embedding_provider import OpenAIEmbeddingProvider, get_embedding_provider
from app.ai.models import (
    AIConfigurationError,
    AIProviderUnavailableError,
    AIResponseValidationError,
)
from app.core.config import Settings


def _fake_response(status_code: int, json_body: dict) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=json_body,
        request=httpx.Request("POST", "https://api.openai.com/v1/embeddings"),
    )


def _embedding_payload(vectors: list[list[float]]) -> dict:
    return {
        "data": [
            {"embedding": vector, "index": index, "object": "embedding"}
            for index, vector in enumerate(vectors)
        ],
        "model": "text-embedding-3-small",
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }


def test_embed_returns_vectors_in_input_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **k: _fake_response(200, _embedding_payload([[0.1, 0.2], [0.3, 0.4]])),
    )
    provider = OpenAIEmbeddingProvider(
        api_key="k", model="text-embedding-3-small", expected_dimension=2
    )
    result = provider.embed(["a", "b"])
    assert result == [[0.1, 0.2], [0.3, 0.4]]


def test_embed_reorders_by_index_even_if_api_returns_out_of_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_of_order = {
        "data": [
            {"embedding": [0.3, 0.4], "index": 1},
            {"embedding": [0.1, 0.2], "index": 0},
        ]
    }
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _fake_response(200, out_of_order))
    provider = OpenAIEmbeddingProvider(api_key="k", model="m", expected_dimension=2)
    result = provider.embed(["first", "second"])
    assert result == [[0.1, 0.2], [0.3, 0.4]]


def test_embed_empty_input_returns_empty_without_a_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(*args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError("network call made for empty input")

    monkeypatch.setattr(httpx, "post", _fail)
    provider = OpenAIEmbeddingProvider(api_key="k", model="m", expected_dimension=2)
    assert provider.embed([]) == []


def test_embed_raises_on_dimension_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A vector whose length doesn't match the schema's committed
    EMBEDDING_DIMENSION must fail closed at the provider layer — a clearer,
    earlier error than letting pgvector reject the INSERT downstream."""
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _fake_response(200, _embedding_payload([[0.1, 0.2, 0.3]]))
    )
    provider = OpenAIEmbeddingProvider(api_key="k", model="m", expected_dimension=1536)
    with pytest.raises(AIResponseValidationError):
        provider.embed(["a"])


def test_embed_raises_on_count_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _fake_response(200, _embedding_payload([[0.1, 0.2]]))
    )
    provider = OpenAIEmbeddingProvider(api_key="k", model="m", expected_dimension=2)
    with pytest.raises(AIResponseValidationError):
        provider.embed(["a", "b"])  # two inputs, only one embedding returned


def test_embed_raises_on_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", _raise)
    provider = OpenAIEmbeddingProvider(api_key="k", model="m", expected_dimension=2)
    with pytest.raises(AIProviderUnavailableError):
        provider.embed(["a"])


def test_embed_raises_on_non_2xx_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _fake_response(401, {"error": {"message": "bad key"}})
    )
    provider = OpenAIEmbeddingProvider(api_key="k", model="m", expected_dimension=2)
    with pytest.raises(AIProviderUnavailableError):
        provider.embed(["a"])


def test_request_has_correct_auth_header_and_model_and_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _capture(*args: object, **kwargs: object) -> httpx.Response:
        captured.update(kwargs)
        return _fake_response(200, _embedding_payload([[0.1, 0.2]]))

    monkeypatch.setattr(httpx, "post", _capture)
    provider = OpenAIEmbeddingProvider(
        api_key="sk-the-real-secret", model="the-configured-model", expected_dimension=2
    )
    provider.embed(["chunk text"])

    headers = captured["headers"]
    assert headers["Authorization"] == "Bearer sk-the-real-secret"
    body = captured["json"]
    assert body["model"] == "the-configured-model"
    assert body["input"] == ["chunk text"]
    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect < timeout.read


def test_error_response_never_leaks_the_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-THIS-MUST-NEVER-APPEAR-IN-AN-ERROR-MESSAGE"
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _fake_response(401, {"error": {"message": "bad key"}})
    )
    provider = OpenAIEmbeddingProvider(api_key=secret, model="m", expected_dimension=2)
    with pytest.raises(AIProviderUnavailableError) as exc_info:
        provider.embed(["chunk source code"])
    assert secret not in str(exc_info.value)


# ---- get_embedding_provider() factory ---------------------------------------


def test_get_embedding_provider_returns_none_without_api_key() -> None:
    settings = Settings(embedding_api_key=None)
    assert get_embedding_provider(settings) is None


def test_get_embedding_provider_returns_openai_provider_when_configured() -> None:
    settings = Settings(embedding_api_key="sk-test", embedding_provider="openai")
    provider = get_embedding_provider(settings)
    assert isinstance(provider, OpenAIEmbeddingProvider)


def test_get_embedding_provider_raises_configuration_error_for_unsupported_provider() -> None:
    settings = Settings(embedding_api_key="sk-test", embedding_provider="voyage")
    with pytest.raises(AIConfigurationError):
        get_embedding_provider(settings)


def test_get_embedding_provider_never_makes_a_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*args: object, **kwargs: object) -> httpx.Response:
        raise AssertionError("network call made while merely constructing a provider")

    monkeypatch.setattr(httpx, "post", _fail)
    settings = Settings(embedding_api_key="sk-test")
    get_embedding_provider(settings)  # must not raise
