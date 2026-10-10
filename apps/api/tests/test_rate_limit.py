"""Rate limiting of the write endpoints (app/api/rate_limit.py,
docs/architecture/deployment-safety.md section 3): the sliding-window
limiter itself, against a fake clock, and its wiring into every write
route. No database needed: each route is driven with ids it rejects before
touching one, and the limiter runs before the handler.
"""

import re
import threading
from collections.abc import Callable, Generator, Iterable, Iterator

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import BaseRoute

from app.api.rate_limit import (
    RateLimitDependency,
    RateLimitExceededError,
    RateLimitRule,
    SlidingWindowRateLimiter,
    get_rate_limiter,
)
from app.db.session import get_db
from app.main import app

client = TestClient(app)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _isolated() -> Generator[None]:
    app.dependency_overrides[get_db] = lambda: iter([None])
    get_rate_limiter().reset()
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_db, None)
        get_rate_limiter().reset()


RULE = RateLimitRule(per_client=2, global_limit=3, window_seconds=60.0)


# ---- the limiter ----------------------------------------------------------------


def test_per_client_ceiling_then_retry_after() -> None:
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(clock)
    limiter.hit("ingest", "a", RULE)
    clock.now += 10
    limiter.hit("ingest", "a", RULE)

    with pytest.raises(RateLimitExceededError) as exc:
        limiter.hit("ingest", "a", RULE)
    assert exc.value.scope == "client"
    assert exc.value.retry_after_seconds == 50  # the oldest hit leaves the window in 50s

    limiter.hit("ingest", "b", RULE)  # another client is unaffected


def test_window_slides() -> None:
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(clock)
    limiter.hit("ingest", "a", RULE)
    clock.now += 30
    limiter.hit("ingest", "a", RULE)
    clock.now += 30  # the first hit is now exactly one window old
    limiter.hit("ingest", "a", RULE)
    with pytest.raises(RateLimitExceededError):
        limiter.hit("ingest", "a", RULE)


def test_global_ceiling_cannot_be_bypassed_by_rotating_client_keys() -> None:
    limiter = SlidingWindowRateLimiter(FakeClock())
    for key in ("a", "b", "c"):
        limiter.hit("ingest", key, RULE)
    with pytest.raises(RateLimitExceededError) as exc:
        limiter.hit("ingest", "spoofed-new-key", RULE)
    assert exc.value.scope == "global"


def test_rejected_requests_consume_no_capacity() -> None:
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(clock)
    limiter.hit("ingest", "a", RULE)
    limiter.hit("ingest", "a", RULE)
    for _ in range(100):
        with pytest.raises(RateLimitExceededError):
            limiter.hit("ingest", "a", RULE)
    clock.now += 60
    limiter.hit("ingest", "a", RULE)
    limiter.hit("ingest", "a", RULE)


def test_buckets_are_independent() -> None:
    limiter = SlidingWindowRateLimiter(FakeClock())
    limiter.hit("ingest", "a", RULE)
    limiter.hit("ingest", "a", RULE)
    limiter.hit("analysis", "a", RULE)


def test_stale_client_keys_are_swept() -> None:
    clock = FakeClock()
    limiter = SlidingWindowRateLimiter(clock)
    rule = RateLimitRule(per_client=1, global_limit=1000, window_seconds=60.0)
    for i in range(100):
        limiter.hit("ingest", f"client-{i}", rule)
    assert limiter.tracked_client_keys() == 100
    clock.now += 120
    limiter.hit("ingest", "late", rule)
    assert limiter.tracked_client_keys() == 1


def test_concurrent_requests_never_exceed_the_global_ceiling() -> None:
    limiter = SlidingWindowRateLimiter()
    rule = RateLimitRule(per_client=1000, global_limit=25, window_seconds=3600.0)
    admitted: list[int] = []
    barrier = threading.Barrier(50)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            limiter.hit("pipeline", f"c{i % 5}", rule)
            admitted.append(i)
        except RateLimitExceededError:
            pass

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(admitted) == 25


# ---- wiring into the write routes ----------------------------------------------------

# Every state-mutating route in the API and the bucket that limits it. The
# inventory test below fails if a write route is added, removed or rewired
# without updating this table, so none can be left unclassified.
WRITE_ROUTE_BUCKETS = {
    ("POST", "/repositories/ingest"): "ingest",
    ("POST", "/repositories/{repository_id}/revisions/{revision_id}/analyze"): "pipeline",
    ("POST", "/repositories/{repository_id}/automation/analyze"): "pipeline",
    ("POST", "/repositories/{repository_id}/impact"): "analysis",
    ("POST", "/repositories/{repository_id}/qa"): "analysis",
    ("POST", "/repositories/{repository_id}/testing/generate"): "analysis",
    ("POST", "/repositories/{repository_id}/documentation/generate"): "analysis",
    ("POST", "/repositories/{repository_id}/security/analyze"): "analysis",
    ("POST", "/repositories/{repository_id}/sessions"): "session",
    ("PATCH", "/repositories/{repository_id}/sessions/{session_id}"): "session",
    ("POST", "/repositories/{repository_id}/sessions/{session_id}/messages"): "session",
    ("POST", "/repositories/{repository_id}/sessions/{session_id}/context"): "session",
    (
        "DELETE",
        "/repositories/{repository_id}/sessions/{session_id}/context/{context_ref_id}",
    ): "session",
}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

BAD = "not-a-uuid"
# Every path parameter filled with an id each handler rejects (422) before
# touching the database.
WRITE_ROUTES = [
    (method, bucket, re.sub(r"\{[a-z_]+\}", BAD, template))
    for (method, template), bucket in WRITE_ROUTE_BUCKETS.items()
]
ONE_PER_CLIENT = {
    "DEPLOYMENT_MODE": "public_demo",
    "TEST_EXECUTION_ENABLED": None,
    "RATE_LIMIT_ENABLED": None,
    "RATE_LIMIT_INGEST_PER_CLIENT": "1",
    "RATE_LIMIT_PIPELINE_PER_CLIENT": "1",
    "RATE_LIMIT_ANALYSIS_PER_CLIENT": "1",
    "RATE_LIMIT_SESSION_PER_CLIENT": "1",
}


def _api_routes(routes: Iterable[BaseRoute]) -> Iterator[APIRoute]:
    """Every APIRoute in the app. This FastAPI version keeps included routers
    as nested route objects holding `original_router`; the OpenAPI
    cross-check below fails if this walk ever misses a route."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        nested = getattr(route, "original_router", None)
        if nested is not None:
            yield from _api_routes(nested.routes)


def _limiter_buckets(route: APIRoute) -> list[str]:
    return [
        dependency.call.bucket
        for dependency in route.dependant.dependencies
        if isinstance(dependency.call, RateLimitDependency)
    ]


def test_every_write_operation_in_the_api_is_classified() -> None:
    """From the public OpenAPI schema, independent of how routes are stored."""
    operations = {
        (method.upper(), path)
        for path, item in app.openapi()["paths"].items()
        for method in item
        if method.upper() in WRITE_METHODS
    }
    assert operations == set(WRITE_ROUTE_BUCKETS)


def test_every_write_route_is_limited_by_its_bucket() -> None:
    found: dict[tuple[str, str], list[str]] = {}
    for route in _api_routes(app.routes):
        for method in (route.methods or set()) & WRITE_METHODS:
            found[(method, route.path)] = _limiter_buckets(route)
    assert found == {key: [bucket] for key, bucket in WRITE_ROUTE_BUCKETS.items()}


def test_no_read_route_is_limited() -> None:
    reads = [r for r in _api_routes(app.routes) if not (r.methods or set()) & WRITE_METHODS]
    assert reads
    for route in reads:
        assert _limiter_buckets(route) == [], route.path


@pytest.mark.parametrize(("method", "bucket", "path"), WRITE_ROUTES)
def test_every_write_route_returns_a_typed_429_in_public_demo(
    override_settings: Callable[..., None], method: str, bucket: str, path: str
) -> None:
    override_settings(**ONE_PER_CLIENT)

    first = client.request(method, path, json={})
    assert first.status_code != 429  # rejected or served by the handler, not the limiter

    second = client.request(method, path, json={})
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1
    detail = second.json()["detail"]
    assert set(detail) == {"code", "bucket", "scope", "retry_after_seconds", "message"}
    assert detail["code"] == "rate_limited"
    assert detail["bucket"] == bucket
    assert detail["scope"] == "client"
    assert detail["retry_after_seconds"] == int(second.headers["Retry-After"])
    assert detail["message"]


def test_analysis_routes_share_one_bucket(override_settings: Callable[..., None]) -> None:
    override_settings(**ONE_PER_CLIENT)
    assert client.post(f"/repositories/{BAD}/impact", json={}).status_code != 429
    assert client.post(f"/repositories/{BAD}/qa", json={}).status_code == 429
    # A different bucket still has capacity.
    assert client.post("/repositories/ingest", json={}).status_code != 429


def test_session_writes_share_their_own_bucket(override_settings: Callable[..., None]) -> None:
    override_settings(**{**ONE_PER_CLIENT, "RATE_LIMIT_SESSION_PER_CLIENT": "3"})
    session = f"/repositories/{BAD}/sessions/{BAD}"
    assert client.post(f"/repositories/{BAD}/sessions", json={}).status_code != 429
    assert client.post(f"{session}/messages", json={}).status_code != 429
    assert client.patch(session, json={}).status_code != 429
    limited = client.delete(f"{session}/context/{BAD}")
    assert limited.status_code == 429
    assert limited.json()["detail"]["bucket"] == "session"
    # Saving notes never consumes analysis capacity, and the reverse.
    assert client.post(f"/repositories/{BAD}/impact", json={}).status_code != 429


def test_session_global_ceiling(override_settings: Callable[..., None]) -> None:
    override_settings(
        **{
            **ONE_PER_CLIENT,
            "RATE_LIMIT_SESSION_PER_CLIENT": "100",
            "RATE_LIMIT_SESSION_GLOBAL": "2",
        }
    )
    path = f"/repositories/{BAD}/sessions"
    assert client.post(path, json={}).status_code != 429
    assert client.post(path, json={}).status_code != 429
    response = client.post(path, json={})
    assert response.status_code == 429
    assert response.json()["detail"]["scope"] == "global"


def test_global_ceiling_applies_over_http(override_settings: Callable[..., None]) -> None:
    override_settings(
        **{**ONE_PER_CLIENT, "RATE_LIMIT_INGEST_PER_CLIENT": "100", "RATE_LIMIT_INGEST_GLOBAL": "2"}
    )
    assert client.post("/repositories/ingest", json={}).status_code != 429
    assert client.post("/repositories/ingest", json={}).status_code != 429
    response = client.post("/repositories/ingest", json={})
    assert response.status_code == 429
    assert response.json()["detail"]["scope"] == "global"


def test_client_identity_ignores_forwarding_headers(
    override_settings: Callable[..., None],
) -> None:
    """The application keys on the ASGI client address only. Headers a
    client controls (X-Forwarded-For, X-Real-IP, Forwarded) never select a
    new per-client bucket; only the ASGI server may rewrite the address, and
    only for proxies it is configured to trust (deployment-safety.md 4)."""
    override_settings(**{**ONE_PER_CLIENT, "RATE_LIMIT_SESSION_PER_CLIENT": "2"})
    path = f"/repositories/{BAD}/sessions"
    spoofed = [
        {"X-Forwarded-For": "203.0.113.1"},
        {"X-Real-IP": "203.0.113.2", "Forwarded": "for=203.0.113.3"},
        {"X-Forwarded-For": "203.0.113.4, 198.51.100.9", "X-Client-IP": "203.0.113.5"},
    ]
    assert client.post(path, json={}, headers=spoofed[0]).status_code != 429
    assert client.post(path, json={}, headers=spoofed[1]).status_code != 429
    response = client.post(path, json={}, headers=spoofed[2])
    assert response.status_code == 429
    assert response.json()["detail"]["scope"] == "client"


def test_reads_are_not_limited(override_settings: Callable[..., None]) -> None:
    override_settings(**ONE_PER_CLIENT)
    for _ in range(5):
        assert client.get("/health").status_code == 200
        assert client.get("/capabilities").status_code == 200
        assert client.get(f"/repositories/{BAD}/sessions").status_code != 429
        assert client.get(f"/repositories/{BAD}/sessions/{BAD}").status_code != 429


@pytest.mark.parametrize(("method", "bucket", "path"), WRITE_ROUTES)
def test_development_default_does_not_limit(
    override_settings: Callable[..., None], method: str, bucket: str, path: str
) -> None:
    override_settings(
        DEPLOYMENT_MODE="development",
        RATE_LIMIT_ENABLED=None,
        **{
            f"RATE_LIMIT_{bucket.upper()}_PER_CLIENT": "1",
            f"RATE_LIMIT_{bucket.upper()}_GLOBAL": "1",
        },
    )
    for _ in range(5):
        assert client.request(method, path, json={}).status_code != 429


def test_development_can_opt_in(override_settings: Callable[..., None]) -> None:
    override_settings(
        DEPLOYMENT_MODE="development", RATE_LIMIT_ENABLED="true", RATE_LIMIT_SESSION_PER_CLIENT="1"
    )
    assert client.post(f"/repositories/{BAD}/sessions", json={}).status_code != 429
    assert client.post(f"/repositories/{BAD}/sessions", json={}).status_code == 429
