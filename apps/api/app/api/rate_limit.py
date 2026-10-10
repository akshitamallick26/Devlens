"""In-process rate limiting for the API's write endpoints. See
docs/architecture/deployment-safety.md section 3.

Why in-process, not Redis: the deployment target is one API instance, so
one process sees every request and a lock-protected dict is exact. Redis
would add a network dependency, a failure mode (what happens when it is
down) and a service to pay for, to solve a multi-instance problem this
deployment does not have. Scaling out to several instances or workers makes
each one enforce its own window, so the effective ceilings multiply; that
is the point at which a shared store becomes necessary.

Each bucket enforces two sliding-window ceilings:

- per client, keyed by `request.client.host`: the ASGI server's client
  address, and nothing else. This module never reads X-Forwarded-For,
  X-Real-IP, Forwarded or any other header, so the application itself
  trusts no client-controlled value. Which address that is (the TCP peer,
  or one uvicorn derived from X-Forwarded-For because the peer is a proxy
  it was told to trust) is deployment configuration, documented in
  deployment-safety.md section 4. A client that can choose that address
  can rotate per-client keys;
- global, across every client. This one cannot be bypassed by any choice of
  client key, so it is the real bound on clones, pipeline runs and provider
  spend per window.

A request is recorded only when both ceilings admit it, so a rejected
request never consumes capacity, and the number of tracked client keys is
bounded by the global ceilings (every live key holds at least one admitted
hit). Stale keys are swept periodically.
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from fastapi import HTTPException, Request

from app.core.config import Settings, get_settings

RateLimitBucket = Literal["ingest", "pipeline", "analysis", "session"]
RateLimitScope = Literal["client", "global"]

_SWEEP_INTERVAL_SECONDS = 60.0


@dataclass(frozen=True)
class RateLimitRule:
    per_client: int
    global_limit: int
    window_seconds: float


class RateLimitExceededError(Exception):
    def __init__(self, bucket: str, scope: RateLimitScope, retry_after_seconds: int) -> None:
        super().__init__(f"rate limit exceeded for {bucket} ({scope})")
        self.bucket = bucket
        self.scope = scope
        self.retry_after_seconds = retry_after_seconds


def _prune(hits: deque[float], cutoff: float) -> None:
    while hits and hits[0] <= cutoff:
        hits.popleft()


def _retry_after(oldest: float, window_seconds: float, now: float) -> int:
    return max(1, math.ceil(oldest + window_seconds - now))


class SlidingWindowRateLimiter:
    """Thread-safe: FastAPI runs these sync dependencies in its threadpool."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._global_hits: dict[str, deque[float]] = {}
        self._client_hits: dict[tuple[str, str], deque[float]] = {}
        self._last_sweep = clock()

    def hit(self, bucket: str, client_key: str, rule: RateLimitRule) -> None:
        """Records one request, or raises `RateLimitExceededError` without
        recording anything."""
        with self._lock:
            now = self._clock()
            cutoff = now - rule.window_seconds
            global_hits = self._global_hits.setdefault(bucket, deque())
            _prune(global_hits, cutoff)
            client_hits = self._client_hits.get((bucket, client_key))
            if client_hits is not None:
                _prune(client_hits, cutoff)

            if client_hits and len(client_hits) >= rule.per_client:
                raise RateLimitExceededError(
                    bucket, "client", _retry_after(client_hits[0], rule.window_seconds, now)
                )
            if len(global_hits) >= rule.global_limit:
                raise RateLimitExceededError(
                    bucket, "global", _retry_after(global_hits[0], rule.window_seconds, now)
                )

            global_hits.append(now)
            if client_hits is None:
                client_hits = self._client_hits[(bucket, client_key)] = deque()
            client_hits.append(now)

            if now - self._last_sweep >= _SWEEP_INTERVAL_SECONDS:
                self._sweep(cutoff)
                self._last_sweep = now

    def _sweep(self, cutoff: float) -> None:
        # One window for every bucket (Settings.rate_limit_window_seconds),
        # so one cutoff applies to all keys.
        for key in [k for k, hits in self._client_hits.items() if not hits or hits[-1] <= cutoff]:
            del self._client_hits[key]

    def tracked_client_keys(self) -> int:
        with self._lock:
            return len(self._client_hits)

    def reset(self) -> None:
        with self._lock:
            self._global_hits.clear()
            self._client_hits.clear()


@lru_cache
def get_rate_limiter() -> SlidingWindowRateLimiter:
    return SlidingWindowRateLimiter()


def rule_for(settings: Settings, bucket: RateLimitBucket) -> RateLimitRule:
    limits = {
        "ingest": (settings.rate_limit_ingest_per_client, settings.rate_limit_ingest_global),
        "pipeline": (settings.rate_limit_pipeline_per_client, settings.rate_limit_pipeline_global),
        "analysis": (settings.rate_limit_analysis_per_client, settings.rate_limit_analysis_global),
        "session": (settings.rate_limit_session_per_client, settings.rate_limit_session_global),
    }
    per_client, global_limit = limits[bucket]
    return RateLimitRule(per_client, global_limit, settings.rate_limit_window_seconds)


class RateLimitDependency:
    """A route dependency enforcing one bucket. A class rather than a closure
    so the bucket is inspectable: tests/test_rate_limit.py checks every write
    route in the app against its expected bucket. Settings are read per
    request, so the active mode is always the configured one."""

    def __init__(self, bucket: RateLimitBucket) -> None:
        self.bucket: RateLimitBucket = bucket

    def __call__(self, request: Request) -> None:
        settings = get_settings()
        if not settings.rate_limiting_active:
            return
        client_key = request.client.host if request.client else "unknown"
        bucket = self.bucket
        try:
            get_rate_limiter().hit(bucket, client_key, rule_for(settings, bucket))
        except RateLimitExceededError as exc:
            who = "this client" if exc.scope == "client" else "this deployment"
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "rate_limited",
                    "bucket": exc.bucket,
                    "scope": exc.scope,
                    "retry_after_seconds": exc.retry_after_seconds,
                    "message": (
                        f"Rate limit reached for {exc.bucket} requests from {who}. "
                        f"Try again in {exc.retry_after_seconds} seconds."
                    ),
                },
                headers={"Retry-After": str(exc.retry_after_seconds)},
            ) from exc


def rate_limited(bucket: RateLimitBucket) -> RateLimitDependency:
    return RateLimitDependency(bucket)
