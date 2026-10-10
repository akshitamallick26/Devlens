"""One concrete `EmbeddingProvider`: a single HTTP call to OpenAI's
Embeddings API via httpx — same "no vendor SDK" discipline as
app/ai/provider.py. See docs/architecture/rag-codebase-qa.md section 4.

`get_embedding_provider()` mirrors `app/ai/provider.py::get_provider()`
exactly: `None` when no key is configured (normal, silent), raises
`AIConfigurationError` when a key is present but the provider name isn't
supported, never called at import/startup time.
"""

import httpx

from app.ai.models import (
    AIConfigurationError,
    AIProviderUnavailableError,
    AIResponseValidationError,
    EmbeddingProvider,
)
from app.core.config import Settings, get_settings

_OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"

# OpenAI's embedding models have no separate "version" concept beyond the
# model name itself — this is the one shared value both embedding
# generation (app/rag/embeddings.py) and query-time retrieval
# (app/rag/qa.py) must use identically, or vector search would never match
# anything (Embedding's unique key, and every retrieval WHERE clause,
# includes model_version exactly). Matches
# app/models/rag.py::Embedding.model_version's own documented
# empty-string convention for "provider exposes no explicit version".
DEFAULT_EMBEDDING_MODEL_VERSION = ""

# Same asymmetric-timeout reasoning as app/ai/provider.py: fail fast on
# connect/write/pool, generous on read — embedding a batch of chunks can
# take longer than a trivial request/response round-trip.
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)


class OpenAIEmbeddingProvider:
    """Real network call — never exercised by the automated test suite.
    Tests use a fake `EmbeddingProvider` or monkeypatch `httpx.post`."""

    def __init__(self, api_key: str, model: str, expected_dimension: int) -> None:
        self._api_key = api_key
        self._model = model
        self._expected_dimension = expected_dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            response = httpx.post(
                _OPENAI_EMBEDDINGS_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self._model, "input": texts},
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Verified not to leak the API key or input text — httpx's own
            # exception messages are built from status/reason/URL only
            # (identical verification as app/ai/provider.py's secret-leak
            # tests; see tests/ai/test_embedding_provider.py).
            raise AIProviderUnavailableError(f"OpenAI embeddings request failed: {exc}") from exc

        try:
            payload = response.json()
            items = payload["data"]
            if not isinstance(items, list) or len(items) != len(texts):
                raise AIResponseValidationError(
                    f"expected {len(texts)} embeddings, got "
                    f"{len(items) if isinstance(items, list) else type(items).__name__}"
                )
            # Defensive re-ordering by the API's own "index" field rather
            # than trusting array order, even though OpenAI documents it
            # as already-ordered — fail closed if "index" is ever missing
            # rather than silently trusting array position.
            ordered = sorted(items, key=lambda item: item["index"])
            vectors = [item["embedding"] for item in ordered]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AIResponseValidationError(f"unexpected response shape: {exc}") from exc

        for vector in vectors:
            if not isinstance(vector, list) or len(vector) != self._expected_dimension:
                raise AIResponseValidationError(
                    f"expected {self._expected_dimension}-dimensional vectors, "
                    f"got {len(vector) if isinstance(vector, list) else type(vector).__name__}"
                )
        return vectors


def get_embedding_provider(
    settings: Settings | None = None, expected_dimension: int = 1536
) -> EmbeddingProvider | None:
    """`None` (not configured, silent) vs. `AIConfigurationError` raised
    (configured incorrectly) — identical convention to
    `app/ai/provider.py::get_provider()`, including returning `None` when
    deployment policy disables AI (Settings.ai_features_allowed)."""
    settings = settings or get_settings()
    if not settings.ai_features_allowed:
        return None
    if not settings.embedding_api_key:
        return None
    if settings.embedding_provider != "openai":
        raise AIConfigurationError(
            f"unsupported embedding_provider={settings.embedding_provider!r}; "
            "only 'openai' is implemented in v1"
        )
    return OpenAIEmbeddingProvider(
        api_key=settings.embedding_api_key,
        model=settings.embedding_model,
        expected_dimension=expected_dimension,
    )
