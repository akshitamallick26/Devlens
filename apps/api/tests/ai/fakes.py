"""Reusable fake AIProvider/EmbeddingProvider for tests — no real network
call is ever made anywhere in the suite. Shared by tests/impact/, tests/ai/,
and tests/rag/.
"""

import hashlib
from dataclasses import dataclass, field
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass
class FakeProvider:
    """Exactly one of `response`/`error` should be set. Matches the
    `AIProvider` protocol structurally (duck-typed, no inheritance
    needed)."""

    response: BaseModel | None = None
    error: Exception | None = None
    # The fake's own, real identity: what persisted provenance must record
    # for output this fake generated.
    provider_name: str = "fake"
    model_name: str = "fake-model"

    def generate_structured(
        self, *, system_prompt: str, user_content: str, response_model: type[T]
    ) -> T:
        if self.error is not None:
            raise self.error
        assert self.response is not None, "FakeProvider needs either `response` or `error` set"
        return self.response  # type: ignore[return-value]


def _deterministic_vector(text: str, dimension: int) -> list[float]:
    """A fixed-width, deterministic pseudo-embedding: the same text always
    produces the same vector (hashed, not random), and different texts
    reliably produce different vectors — good enough to exercise real
    pgvector distance ordering in tests without a real embedding model."""
    digest = hashlib.sha256(text.encode()).digest()
    return [((digest[i % len(digest)] / 255.0) * 2.0 - 1.0) for i in range(dimension)]


@dataclass
class FakeEmbeddingProvider:
    """Exactly one of `vectors`/`error` should be set. `vectors`, if given,
    maps exact input text to a precomputed vector (for tests that need
    specific, controlled similarity relationships); any text not in that
    map falls back to `_deterministic_vector`."""

    dimension: int = 1536
    vectors: dict[str, list[float]] = field(default_factory=dict)
    error: Exception | None = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.error is not None:
            raise self.error
        return [
            self.vectors.get(text) or _deterministic_vector(text, self.dimension) for text in texts
        ]
