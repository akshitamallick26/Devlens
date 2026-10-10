"""The narrow AI-provider abstraction — no vendor SDK types appear here.
See docs/architecture/impact-intelligence.md section 5. Callers (e.g.
app/impact/explanation.py) depend only on `AIProvider`, never on a
concrete implementation or on `httpx` directly.
"""

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class AIProviderError(Exception):
    """Base for every error this layer can raise. Callers catch this one
    type and fall back to a deterministic-only result — see
    docs/architecture/impact-intelligence.md section 8."""


class AIProviderUnavailableError(AIProviderError):
    """The provider could not be reached at all (network failure, timeout,
    non-2xx response)."""


class AIResponseValidationError(AIProviderError):
    """The provider responded, but the content wasn't valid JSON matching
    the requested response model."""


class AIConfigurationError(AIProviderError):
    """AI *was* configured (an API key is present) but the configuration
    is invalid — e.g. an unsupported `ai_provider` name. Distinct from "not
    configured at all" (which `get_provider()` handles silently by
    returning `None` — a normal, fully-supported state, not an error): this
    is a user mistake worth a clear, typed signal, even though callers
    still degrade gracefully to `deterministic_only` rather than losing the
    deterministic result over it (impact-intelligence.md section 5/10)."""


class AIProvider(Protocol):
    # Identity of the provider/model this instance actually calls. Callers
    # that persist provenance (app/testing/service.py) read it from here,
    # never from separately-passed configuration strings, so what is
    # recorded is always what generated the output.
    @property
    def provider_name(self) -> str: ...

    @property
    def model_name(self) -> str: ...

    def generate_structured(
        self, *, system_prompt: str, user_content: str, response_model: type[T]
    ) -> T:
        """Returns an instance of `response_model`, constructed from the
        provider's response. Raises `AIProviderError` (or a subclass) on
        any failure — never returns a partially-valid or guessed object."""
        ...


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Returns one embedding vector per input text, in the same order
        as `texts`. Raises `AIProviderError` (or a subclass) on any
        failure — including a dimension mismatch against what the caller
        expects — never returns a partial or guessed result. A different
        capability from `AIProvider.generate_structured` (vector
        representation vs. text generation), sharing the same exception
        hierarchy because the failure semantics are identical in kind: see
        docs/architecture/rag-codebase-qa.md section 4."""
        ...
