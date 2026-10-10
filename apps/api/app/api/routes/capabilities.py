"""What this deployment permits, as typed data. Requests for a disabled AI
feature keep returning each route's existing typed deterministic-only result
(`no_provider` and equivalents), which cannot by itself say whether AI is
off by policy or simply has no key; this endpoint says which. Read-only, no
database access, no secrets: it reports whether a key is configured, never
the key. See docs/architecture/deployment-safety.md.
"""

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.rate_limit import RateLimitBucket, rule_for
from app.core.config import Settings, get_settings

router = APIRouter()

_BUCKETS: tuple[RateLimitBucket, ...] = ("ingest", "pipeline", "analysis", "session")


class CapabilityStatus(BaseModel):
    status: Literal["enabled", "disabled", "not_configured"]
    reason: str | None


class RateLimitBucketResponse(BaseModel):
    per_client: int
    global_limit: int


class RateLimitingResponse(BaseModel):
    active: bool
    window_seconds: float
    buckets: dict[str, RateLimitBucketResponse]


class CapabilitiesResponse(BaseModel):
    deployment_mode: str
    ai_explanations: CapabilityStatus
    embeddings: CapabilityStatus
    test_execution: CapabilityStatus
    rate_limiting: RateLimitingResponse


def _ai_status(settings: Settings, key: str | None) -> CapabilityStatus:
    if not settings.ai_features_allowed:
        return CapabilityStatus(
            status="disabled",
            reason=(
                "AI features are disabled in public_demo mode unless PUBLIC_DEMO_AI_ENABLED=true"
            ),
        )
    if not key:
        return CapabilityStatus(status="not_configured", reason="no API key is configured")
    return CapabilityStatus(status="enabled", reason=None)


def _execution_status(settings: Settings) -> CapabilityStatus:
    if settings.test_execution_allowed:
        return CapabilityStatus(status="enabled", reason=None)
    if settings.deployment_mode == "public_demo":
        reason = "generated-test execution is never available in public_demo mode"
    else:
        reason = "generated-test execution requires TEST_EXECUTION_ENABLED=true"
    return CapabilityStatus(status="disabled", reason=reason)


@router.get("/capabilities", response_model=CapabilitiesResponse)
def capabilities() -> CapabilitiesResponse:
    settings = get_settings()
    rules = {bucket: rule_for(settings, bucket) for bucket in _BUCKETS}
    return CapabilitiesResponse(
        deployment_mode=settings.deployment_mode,
        ai_explanations=_ai_status(settings, settings.ai_api_key),
        embeddings=_ai_status(settings, settings.embedding_api_key),
        test_execution=_execution_status(settings),
        rate_limiting=RateLimitingResponse(
            active=settings.rate_limiting_active,
            window_seconds=settings.rate_limit_window_seconds,
            buckets={
                bucket: RateLimitBucketResponse(
                    per_client=rule.per_client, global_limit=rule.global_limit
                )
                for bucket, rule in rules.items()
            },
        ),
    )
