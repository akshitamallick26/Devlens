# Deployment safety: public-demo mode and rate limiting

Status: implemented (`app/core/config.py`, `app/api/rate_limit.py`,
`app/api/routes/capabilities.py`, `app/ai/provider.py`,
`app/ai/embedding_provider.py`, `app/testing/sandbox.py`,
`app/api/routes/testing.py`).

## 1. Problem

The API has no authentication. Every write endpoint is reachable by anyone
who can reach the API. Three consequences matter for a public deployment:

- Ingestion clones any public github.com repository (`url_policy.py` keeps
  it to HTTPS github.com, without credentials), and the pipeline parses and
  stores it. Both cost CPU, time and database storage.
- With `AI_API_KEY` or `EMBEDDING_API_KEY` set, any visitor can spend
  provider credit.
- With `AI_API_KEY` set, `POST /repositories/{id}/testing/generate` with
  `execute: true` runs model-generated pytest code, which imports code from
  the ingested repository, as a subprocess of the API host.
  `app/testing/sandbox.py` is explicit that this is **not** an OS-level
  sandbox: there is no container, network namespace or filesystem
  isolation.

This phase does not add authentication or real isolation. It makes the
dangerous capabilities opt-in, enforces that server-side, and bounds the
expensive endpoints.

## 2. Deployment modes

`DEPLOYMENT_MODE` is `public_demo` (the default) or `development`.

| | `public_demo` | `development` |
|---|---|---|
| AI explanations and embeddings | off unless `PUBLIC_DEMO_AI_ENABLED=true` (and a key is set) | on whenever a key is set (unchanged) |
| Generated-test execution | impossible | only with `TEST_EXECUTION_ENABLED=true` |
| Rate limiting | always on | off unless `RATE_LIMIT_ENABLED=true` |

Fail-closed rules, all enforced by `Settings` at startup:

- The default mode is the restrictive one. A deployment that forgets the
  variable gets `public_demo`. Local development opts out in
  `apps/api/.env.example`, and the test suite opts out in
  `tests/conftest.py`.
- `DEPLOYMENT_MODE` is a `Literal`. Any other value (`production`,
  `Public_Demo`, an empty string) is a validation error. The API refuses to
  start rather than falling back to either mode.
- `TEST_EXECUTION_ENABLED=true` or `RATE_LIMIT_ENABLED=false` together with
  `public_demo` is a startup error. They are not ignored silently, so the
  operator learns that the configuration is not what they think it is.
- Booleans are parsed strictly. `PUBLIC_DEMO_AI_ENABLED=maybe` is an error,
  not a truthy value.
- Every numeric limit (the rate limits and the existing ingestion, testing
  and security limits) must be positive. Before this phase, a zero or
  negative value was accepted, and its effect depended on the comparison
  it fed. For example, `INGESTION_MAX_FILES=0` refused every repository.

### Where each rule is enforced

- **AI:** `get_provider()` and `get_embedding_provider()` return `None` when
  `Settings.ai_features_allowed` is false. Every AI call in the API gets
  its provider from one of these two factories. Callers already handle
  `None` as "not configured", so they return their existing typed
  deterministic-only results without any contract change:
  - impact: `analysis_status: "deterministic_only"`
  - Q&A: `"retrieval_only"`
  - test generation: `"no_provider"`
  - the embedding stage: `"unavailable"`
  - documentation and security explanations: their existing no-provider
    results
- **Execution, twice:**
  - The testing route rejects `execute: true` with
    `403 {"code": "capability_disabled", "capability": "test_execution", "message": ...}`.
    It does so before looking up a provider or writing anything.
  - `sandbox.run_generated_test` raises `ExecutionDisabledError` before
    creating a directory or starting a process. No code path can reach
    execution without passing the policy check, whatever its caller is.
  - This is a refusal, not containment. When execution is enabled in
    development, it is exactly as unisolated as before.
- **Visibility:** `GET /capabilities` reports the mode and, for AI,
  embeddings and execution, whether each is `enabled`, `disabled` (by
  policy) or `not_configured` (no key). It also reports the active rate
  limits. It never returns a key. It exists because a `no_provider` result
  cannot by itself say whether AI is off by policy or simply has no key.

## 3. Rate limiting

`app/api/rate_limit.py` attaches a route dependency to **every** state-
mutating route. The API has 13 POST, PATCH and DELETE routes, and no PUT:

| Bucket | Routes | Default per client / global, per hour |
|---|---|---|
| `ingest` | `POST /repositories/ingest` | 5 / 30 |
| `pipeline` | `POST .../revisions/{revision_id}/analyze`, `POST .../automation/analyze` (both rebuild parsing, graph and chunks) | 10 / 60 |
| `analysis` | `POST .../impact`, `.../qa`, `.../testing/generate`, `.../documentation/generate`, `.../security/analyze` | 60 / 600 |
| `session` | `POST .../sessions`, `PATCH .../sessions/{session_id}`, `POST .../sessions/{session_id}/messages`, `POST .../sessions/{session_id}/context`, `DELETE .../sessions/{session_id}/context/{context_ref_id}` | 120 / 1200 |

Investigation-session writes get their own bucket rather than sharing
`analysis`:
- They cost almost nothing to compute. What needs bounding is the volume of
  visitor-supplied content they persist.
- In a shared bucket, saving notes would use up analysis capacity, and the
  reverse.
- Their defaults leave room for interactive note-taking. They still bound
  the worst case. For example, at most 1,200 messages of up to 20,000
  characters each per hour across all clients.

No route is unlimited by omission. `tests/test_rate_limit.py` compares every
write operation in the OpenAPI schema, and every route's limiter, against
the table above. The tests fail if a write route is added, removed or
rewired without being classified there.

Intentionally unlimited, because they mutate nothing:
- every GET route;
- `/health` and `/health/db`;
- `/capabilities`.

Design:

- **A sliding window per bucket, with two ceilings.**
  - The per-client ceiling is keyed by the client address (section 4).
  - The global ceiling covers all clients together. No choice of client key
    can get around it, so it is the real bound on clones, pipeline runs,
    provider spend and session writes per window.
- **A rejected request is not recorded.** Hammering the API while limited
  does not extend the lockout, and the number of tracked client keys is
  bounded by the global ceilings. Stale keys are swept once a minute.
- **An admitted request counts even if the handler then rejects it.** The
  limiter runs before request validation, so an invalid ingest URL that
  returns 422 still uses one of the client's ingest requests.
- **The 429 response is typed and identical for every bucket:**
  `{"code": "rate_limited", "bucket", "scope": "client" | "global", "retry_after_seconds", "message"}`,
  plus a `Retry-After` header with the same number of seconds. The
  frontend's `describeDetail` already renders `message`.
- **In process, not Redis.** The recommended deployment runs one API
  instance, where a lock-protected dict is exact. Redis would add a network
  dependency, a failure mode and a paid service for a multi-instance problem
  this deployment does not have.
  - With several instances or uvicorn workers, each process enforces its
    own window, so the effective ceilings multiply by the process count.
  - Counters also reset on restart.
  - Both are accepted limitations for a single-instance demo. A shared
    store becomes necessary when either one stops being acceptable.

## 4. Client identity

**What the application uses.** The per-client key is `request.client.host`
and nothing else. That is the ASGI `scope["client"]` address the server
hands to the application.

**What the application never reads.** The application never reads
`X-Forwarded-For`, `X-Real-IP`, `Forwarded`, `X-Client-IP` or any other
header to identify a client, and it adds no proxy-header middleware of its
own. A test sends requests that differ only in those headers and checks
that they share one per-client bucket. Whether a forwarded address is
believed is decided by the ASGI server alone, from its configuration.

**What uvicorn puts in `scope["client"]`.** Checked against the installed
uvicorn 0.54 source (`uvicorn/config.py`, `uvicorn/middleware/proxy_headers.py`):

- **Proxy-header handling is on by default.** `--proxy-headers` defaults to
  true, so no flag is needed.
- **Which proxies it trusts.** It trusts the TCP peer only if the peer is
  listed in `--forwarded-allow-ips`. That defaults to the `FORWARDED_ALLOW_IPS`
  environment variable, or `127.0.0.1,::1` when that is unset.
- **Untrusted peer (any connection not from those addresses).**
  `X-Forwarded-For` is ignored and the key is the TCP peer address.
- **Trusted peer.** The key becomes the rightmost `X-Forwarded-For` entry
  that is not itself a trusted proxy. Each proxy appends to that header, so
  this is the address the outermost trusted proxy saw.
- **With `*`, every peer and every entry is trusted.** uvicorn then uses
  the **leftmost** entry, which the client wrote itself. A client can then
  choose its key and rotate per-client buckets.

**Consequences for a deployment.** Setting the proxy addresses is a
deployment-time concern; nothing in the repository sets them.

- **Default settings behind a platform proxy.** The proxy's address is not
  trusted, so every visitor shares the proxy's address as their key. The
  per-client ceiling then acts as a second global ceiling: more
  restrictive, never less.
- **The intended configuration.** Set `FORWARDED_ALLOW_IPS` (or
  `--forwarded-allow-ips`) to the platform proxy's addresses or network.
  Each visitor then gets their own per-client bucket.
- **`*`.** Use it only if the API cannot be reached except through the
  platform proxy, and accept that per-client keys can be spoofed.
- **The global ceilings hold in every case.** They are the protection that
  does not depend on client identity.

## 5. Resource limits

The existing configurable limits stay as they are, but each must now be
positive (section 2):
- the ingestion file count, per-file and total bytes;
- the clone and git command timeouts;
- the test-execution timeout and output cap;
- the scanner timeouts.

The defaults are unchanged so that development behavior is unchanged.
`apps/api/.env.example` documents lower ingestion limits for a small
managed database, because ingested file content is stored in Postgres.

Write requests are bounded per field. Session titles are limited to 200
characters and messages to 20,000. `graph_camera_state` used to be the one
field persisted as visitor-supplied JSON of any size. It is now capped at
4,096 bytes (`MAX_GRAPH_CAMERA_STATE_BYTES`). The workstation writes four
short fields, about 200 bytes. A larger value is rejected with 422 before
the handler runs.

Two existing settings are still not read by any code path, as before this
phase:
- `security_max_blast_radius_*`. Request ceilings are the route constants
  in `app/api/routes/security.py`.
- `testing_max_tests_per_request`. The ceiling is `MAX_TESTS_PER_REQUEST`.

Wiring them in would change request validation, so it is left as a separate
decision.

## 6. The public_demo boundary

With `DEPLOYMENT_MODE=public_demo` (or unset):

| Capability | Status | Enforced by |
|---|---|---|
| Generated-test execution | impossible; no setting re-enables it | startup validation, the testing route's 403, `sandbox.run_generated_test` |
| AI calls | none, unless `PUBLIC_DEMO_AI_ENABLED=true`. Even then they are bounded by the `analysis` (and `pipeline`, for embeddings) ceilings | both provider factories |
| Repository ingestion | bounded: 5 per client, 30 per hour globally; each one limited by the ingestion file and byte limits and the clone timeout; HTTPS github.com only | `ingest` bucket, `Settings`, `url_policy.py` |
| Expensive analysis | bounded per hour by the `pipeline` and `analysis` ceilings; each request is bounded by its request-model ceilings | rate limiter, route models |
| Investigation and session writes | bounded per hour by the `session` ceilings; each request by the field bounds above | rate limiter, route models |
| Rate limiting itself | cannot be turned off | startup validation |

"Bounded" means bounded per window, not bounded in total. Within the
ceilings, data still accumulates in the database over time. Nothing expires
or deletes ingested repositories or sessions.

## 7. What this does not do

- **No authentication.** Anyone can ingest, analyze and write sessions
  within the rate limits. Sessions are effectively public per repository:
  - Ingesting a GitHub repository that is already stored returns the same
    repository id (`_get_or_create_repository`).
  - `GET /repositories/{repository_id}/sessions` lists that repository's
    sessions.

  So any visitor can read, rename and add to another visitor's session, or
  remove its context references. A public demo should treat sessions as
  shared scratch space, not private notes.
- **No isolation for generated-test execution.** Public deployments lose the
  capability instead.
- **Per-process limits,** not distributed ones (section 3).
- **No request-body size limit** beyond the per-field bounds in the request
  models.
- **No retention policy** for stored data.

## 8. Tests

- `tests/test_deployment_safety.py`:
  - defaults and fail-closed validation;
  - provider gating;
  - the typed 403 before any generation;
  - the sandbox refusing before touching the filesystem;
  - the `graph_camera_state` size bound;
  - `/health` and `/capabilities` in both modes.
- `tests/test_rate_limit.py`:
  - the limiter against a fake clock: per-client and global ceilings,
    sliding, no capacity consumed by rejections, sweeping, concurrency;
  - the write-route inventory, checked against both OpenAPI and the route
    dependencies;
  - every write route returning the same typed 429 in `public_demo`;
  - session writes sharing their own bucket and global ceiling;
  - client identity ignoring forwarding headers;
  - reads never limited;
  - development unlimited by default for every write route.
- `tests/pipeline/test_public_demo_db.py`: end to end against Postgres with
  keys configured but AI disabled by policy. With `httpx.post` made to fail
  if called:
  - pipeline, impact analysis and lexical Q&A work over HTTP;
  - execution is refused;
  - every session write works within the limits.
