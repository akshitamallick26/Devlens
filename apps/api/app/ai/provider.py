"""One concrete `AIProvider`: a single HTTP call to Anthropic's Messages
API via httpx — not the vendor SDK, to avoid adding a new dependency for
one call shape (see docs/architecture/impact-intelligence.md section 5).

`get_provider()` is the only factory callers should use. It returns `None`
when no API key is configured at all, or when deployment policy disables AI
(DEPLOYMENT_MODE=public_demo without PUBLIC_DEMO_AI_ENABLED; both normal,
fully-supported states) and
raises `AIConfigurationError` when a key *is* present but the configured
provider name isn't supported (a user mistake worth a clear signal — see
`AIConfigurationError`'s docstring). Never raises for the "not configured"
case; never called at import/startup time, so the backend starts and every
deterministic route works identically with or without AI configuration.

Currently only `ai_provider = "anthropic"` is implemented — see
docs/architecture/impact-intelligence.md section 5 (Task 5's "Option A").
`AI_PROVIDER=openai` or any other value does not silently work.
"""

import json

import httpx
from pydantic import ValidationError

from app.ai.models import (
    AIConfigurationError,
    AIProvider,
    AIProviderUnavailableError,
    AIResponseValidationError,
    T,
)
from app.core.config import Settings, get_settings

_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_API_VERSION = "2023-06-01"
_MAX_TOKENS = 4096

# Explicit per-phase timeouts, not one uniform value (impact-intelligence.md
# section 3 of this hardening pass): connect/write/pool all fail fast (the
# request itself is small and either the host is reachable quickly or it
# isn't); read is generous because model generation for up to _MAX_TOKENS
# tokens can legitimately take tens of seconds. Zero retries — deliberate,
# not an oversight: a bounded retry-with-backoff for transient 429/5xx could
# be added later if real usage shows a need, but for v1 the simplest correct
# behavior is "fail once, fail closed, let the caller's existing
# deterministic-only fallback handle it" rather than adding retry-timing
# complexity for a code path that's already optional and already degrades
# gracefully.
_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)


class AnthropicProvider:
    """Real network call — never exercised by the automated test suite
    (docs/architecture/impact-intelligence.md section 10 / Task 14). Tests
    use a fake `AIProvider` implementation, or monkeypatch `httpx.post` to
    inspect the exact request this class builds, without any real network
    access."""

    provider_name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    @property
    def model_name(self) -> str:
        """The exact `model` value sent in every request this instance makes."""
        return self._model

    def generate_structured(
        self, *, system_prompt: str, user_content: str, response_model: type[T]
    ) -> T:
        try:
            response = httpx.post(
                _ANTHROPIC_API_URL,
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": _ANTHROPIC_API_VERSION,
                    "content-type": "application/json",
                },
                json={
                    "model": self._model,
                    "max_tokens": _MAX_TOKENS,
                    "system": system_prompt,
                    "messages": [{"role": "user", "content": user_content}],
                },
                timeout=_TIMEOUT,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Covers connection/DNS/timeout failures AND 4xx/5xx status
            # errors uniformly (httpx.HTTPStatusError is itself an
            # httpx.HTTPError subclass). Verified not to leak the API key,
            # headers, or request body: httpx's own exception messages are
            # built only from status code/reason/URL, never headers or body
            # — see tests/ai/test_provider.py's secret-non-leak tests.
            raise AIProviderUnavailableError(f"Anthropic request failed: {exc}") from exc

        try:
            payload = response.json()
            text = payload["content"][0]["text"]
            if not isinstance(text, str):
                # A provider response shape we don't recognize (e.g. a
                # non-text content block) — fail closed rather than let
                # json.loads raise an un-typed TypeError below.
                raise AIResponseValidationError(f"expected text content, got {type(text).__name__}")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AIResponseValidationError(f"unexpected response shape: {exc}") from exc

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIResponseValidationError(f"response was not valid JSON: {exc}") from exc

        try:
            return response_model.model_validate(parsed)
        except ValidationError as exc:
            raise AIResponseValidationError(f"response did not match schema: {exc}") from exc


def get_provider(settings: Settings | None = None) -> AIProvider | None:
    """`None` (not configured, or disabled by deployment policy; silent) vs.
    `AIConfigurationError` raised (configured incorrectly) — see this
    module's docstring and `AIConfigurationError`. Every AI call in the API
    obtains its provider here, so this is where public_demo's "AI off
    unless explicitly enabled" rule is enforced (Settings.ai_features_allowed,
    docs/architecture/deployment-safety.md); callers' existing `None`
    handling then produces their typed deterministic-only result."""
    settings = settings or get_settings()
    if not settings.ai_features_allowed:
        return None
    if not settings.ai_api_key:
        return None
    if settings.ai_provider != "anthropic":
        raise AIConfigurationError(
            f"unsupported ai_provider={settings.ai_provider!r}; "
            "only 'anthropic' is implemented in v1"
        )
    return AnthropicProvider(api_key=settings.ai_api_key, model=settings.ai_model)
