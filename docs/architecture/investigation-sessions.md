# Investigation Sessions & Recents v1

This document is the session contract for `app/investigation/`. It extends
domain-model.md section 10 (which already defines the three tables) with
lifecycle, API, concurrency, and trust-boundary decisions. It does not
redefine anything domain-model.md or database-design.md already settled.

## 1. Forensic current-state audit (summary)

Before any code changed, the actual filesystem was read fresh:

- `InvestigationSession`/`SessionMessage`/`SessionContextRef` already exist
  in `app/models/investigation.py`, already migrated in `alembic/versions/
  0001_initial_schema.py`, and already covered by `tests/db/
  test_session_context.py` and `tests/test_schema_static.py`. No prior
  phase had built a service or API layer on top of them — this phase is the
  first to do so.
- `InvestigationSession` already has `status` (`active`/`archived`),
  `started_at_revision_id`/`current_revision_id` (nullable, plain FKs with
  an application-level same-repository `@validates` backstop — the same
  pattern `AnalysisRun.parent_revision_id` uses), and `last_active_at`
  (already the exact "explicit recency timestamp" Task 9 asks for).
- `SessionMessage` already has `sequence_number` with
  `UniqueConstraint(session_id, sequence_number)` — Task 6's "robust
  ordering mechanism" already exists; it did not need to be added.
- `SessionContextRef` is already typed/bounded (one-hot `ref_type` +
  per-type nullable FK columns + a CHECK constraint enforcing exactly one
  populated), already includes a `finding_id` column beyond the original
  domain document (a documented gap-fill), and was missing only one thing:
  a database-level duplicate-reference guard (added this phase, section 7
  below).
- `Repository.current_revision_id` already exists and is the one place a
  session's revision default is read from at creation time.
- domain-model.md section 15, point 7, had **already settled** the
  single hardest open question in this brief: *"Existing investigation
  sessions are not force-migrated. A session's `current_revision_id` only
  moves when the user/session explicitly advances it — new commits landing
  never silently change what an in-progress investigation is looking at."*
  Section 3 below preserves this decision rather than inventing a second,
  competing "unpinned / follow-latest" revision mode.

No schema was redesigned. The only schema change this phase makes is
additive: four partial unique indexes on `session_context_refs` (section 7).

## 2. The session contract

An `InvestigationSession` belongs to exactly one `Repository` (`repository_id`,
`ON DELETE CASCADE` — deleting a repository deletes its sessions, messages,
and context refs in one cascade sweep).

A session *may* be associated with a specific `RepositoryRevision`
(`current_revision_id`, nullable). A session is resumable: `GET .../sessions/
{id}` reconstructs repository identity, revision context, a bounded message
page, and context-ref breadcrumbs from durable rows alone — no cache, no
session-affinity, no server-side conversation state outside Postgres.

A session is **not** itself an analysis result. `AnalysisRun` remains the
only durable record of deterministic analysis; `SessionContextRef.
analysis_run_id` references it by id and is never mutated by this module
(`add_context_ref` never writes to `AnalysisRun`).

`SessionContextRef` may reference `SourceFile`, `Symbol`, `AnalysisRun`, or
`SecurityFinding` — each by id only. It never duplicates their facts (no
copied title/content/severity columns).

## 3. Revision semantics

Preserving the existing, already-documented design (domain-model.md section
15, point 7) rather than introducing a second mode:

- `started_at_revision_id` is set once at creation and never changes again.
  It is a historical fact: "what revision existed when this investigation
  began."
- `current_revision_id` is nullable and moves **only** via an explicit
  `PATCH .../sessions/{id}` call with a `revision_id` field. Nothing else in
  this codebase — not `GET`, not appending a message, not adding context —
  ever writes to it.
- At creation, if the caller does not supply `revision_id` explicitly,
  `current_revision_id` defaults to `Repository.current_revision_id` at that
  moment (a one-time read, not an ongoing sync). If the repository has no
  current revision yet (nothing ingested), both columns stay `NULL` —
  "this session has no revision context yet," not an error.
- `Repository.current_revision_id` advancing to R2 **never** touches any
  existing session's `current_revision_id`. Reopening a session (`GET`)
  never mutates it. A new message never mutates it. An `AnalysisRun`
  reference from R1 remains a valid, permanent breadcrumb regardless of
  what the repository's current revision becomes later — it is never
  silently reinterpreted as "about R2."
- An explicit revision switch (`PATCH` with `revision_id`) is validated
  against the target repository via `_resolve_revision_for_repository`
  (404 if the revision doesn't exist at all, 409 if it exists but belongs
  to a different repository) before the column is written.

This directly answers every bullet in the hardening brief's revision
scenario list: creating against R1, the repository moving to R2, reopening,
appending a message, referencing an R1 analysis run, and explicitly
switching to R2 are all deterministic and none of them silently rewrites
history.

## 4. Message model and ordering

`SessionMessage.sequence_number` (already existed) plus
`UniqueConstraint(session_id, sequence_number)` (already existed) is the
ordering mechanism — never timestamp equality. `append_message` assigns the
next sequence number under a `SELECT ... FOR UPDATE` row lock on the parent
`InvestigationSession` row (an ordinary Postgres row lock, not a
distributed lock — see section 8), with the unique constraint as the
unconditional backstop regardless of locking behavior.

Roles are constrained to `user | assistant | system` by the pre-existing
CHECK constraint; the service layer re-validates before insert
(`InvalidRoleError` → 400) as a second, independent check for callers that
bypass the API.

Context refs returned in a session snapshot are capped at
`MAX_CONTEXT_REFS_IN_SNAPSHOT` (500, ordered by `created_at, id`) as a
defensive ceiling against a pathological outlier session — normal usage
never approaches it, so this is not a paginated endpoint the way messages
are.

Message retrieval is always paginated (Task 12): `GET .../sessions/{id}`
takes `message_limit` (default 50, max 200) and `message_before_sequence`
(a cursor — the sequence number to page backward from). The response
always includes `has_more` and `next_before_sequence`; there is no
unbounded message query anywhere in this module.

## 5. Context references

The existing one-hot `ref_type` + CHECK-constrained columns are preserved
exactly as audited — no polymorphic table-name column, no JSON blob
standing in for relational integrity.

**Repository consistency (hard, enforced, tested):** `add_context_ref`
loads the referenced `SourceFile`/`Symbol`/`AnalysisRun`/`SecurityFinding`
row and compares its own `repository_id` column against the session's
`repository_id` *before* constructing the reference row.
`CrossRepositoryReferenceError` → 409 if they differ. The schema cannot
express this at the database layer (no composite FK target exists linking
`session_context_refs` to each referenced table's repository), so this is
documented, application-layer-enforced, and tested
(`tests/investigation/test_context_refs_db.py`).

**Revision consistency:** deliberately *not* re-litigated. The existing,
already-passing test `tests/db/test_session_context.py::
test_context_ref_may_point_at_an_older_revision_than_the_sessions_current_one`
establishes that a breadcrumb pointing at an older revision than the
session's current one is the *intended* behavior, not a gap — investigations
routinely span multiple revisions as commits land. Since every
revision-scoped entity's `revision_id` belongs to exactly one repository,
the repository check above *is* the full revision-consistency check
available for a historical breadcrumb; there is no additional "must match
the session's current revision" rule layered on top; adding one would
contradict the already-established, already-tested design.

**Historical breadcrumbs across re-parsing (intentional safety
behavior).** A breadcrumb keeps pointing at the exact row it was created
for; nothing ever re-points it at a newer revision or a re-created row.
Two things keep that sound when a revision is re-analyzed:

- Re-parsing a revision keeps the `Symbol.id` of every symbol it
  reproduces (`source-intelligence.md` sections 8 and 15), so a
  `selected_symbol` breadcrumb into that revision stays valid and resolves
  to the same symbol after the re-parse. Session messages, other refs and
  recency are untouched by parsing.
- If a re-parse would delete a symbol that a breadcrumb references (only
  possible when the parser's output changes for the same immutable source),
  parsing refuses: it raises `HistoricalReferenceError`, records the parsing
  run as `failed`, and changes nothing — the symbol, its graph edges and the
  breadcrumb all stay as they were. Nulling the reference is impossible by
  design (`symbol_id` is `ON DELETE SET NULL`, but the one-hot CHECK above
  requires it to stay populated), and silently deleting the breadcrumb is
  not done. No placeholder or tombstone mechanism exists; preserving the
  historical reference takes priority over that re-parse.

`selected_file`, `analysis_reference` and `finding_reference` breadcrumbs
are unaffected by re-parsing, which never deletes source files, analysis
runs or findings. Deleting the whole repository deletes its sessions (and
their refs) along with everything else.

**Duplicate prevention (DB-enforced, Task 14):** four partial unique
indexes on `session_context_refs` — `(session_id, file_id)` WHERE
`ref_type = 'selected_file'`, and one each for `symbol_id`/
`analysis_run_id`/`finding_id` — added in migration `0008`, mirroring the
exact pattern `graph_edges`/`analysis_runs` already use for their own
partial dedup indexes (a plain UNIQUE constraint over a nullable column
would never fire, since Postgres treats every NULL as distinct). Adding the
same context reference twice is **idempotent**: `add_context_ref` uses
`INSERT ... ON CONFLICT DO NOTHING` targeting the matching index, and
returns the existing row rather than erroring or duplicating.
`graph_focus` rows are deliberately excluded from deduplication — repeated
camera-state snapshots at different points in an investigation are
distinct breadcrumbs, not duplicates.

## 6. Analysis-run references

Section 8's four validations, as actually implemented in `add_context_ref`:

1. **Repository matches** — enforced (section 5 above).
2. **Revision semantics are valid** — automatically true once repository
   matches: `AnalysisRun`'s own composite FK already guarantees its
   `revision_id` belongs to its `repository_id`, so there is nothing further
   to independently check.
3. **Referenced run exists** — `db.get(AnalysisRun, analysis_run_id)` is
   `None` → `ReferencedEntityNotFoundError` → 404.
4. **Analyzer identity remains intact** — trivially true: this module never
   writes to any `AnalysisRun` column. `add_context_ref` only ever inserts
   into `session_context_refs`.

## 7. Recents

"Recent" = `ORDER BY last_active_at DESC, id DESC` — the `id` tie-break
makes ordering deterministic even when two sessions share a timestamp
(microsecond-precision `timestamptz`, but never relied upon to be unique).
The existing index `ix_investigation_sessions_repository_id_last_active_at`
already supports this ordering; no new index was needed.

`last_active_at` is bumped by: appending a message, adding a context ref,
removing a context ref, renaming, reactivating (`status` → `active`), and
switching revision. It is **not** bumped by `GET` (reading a session is not
a mutation) and **not** bumped by archiving (`status` → `archived` — putting
an investigation away should not bubble it back to the top of "recent"；
this is a deliberate, documented choice, not an oversight).

`list_recent_sessions` issues exactly two queries regardless of how many
sessions are returned: one `COUNT` for `total_count`, and one query with an
outer join against a `GROUP BY` subquery for per-session message counts —
never one count query per session (Task 9's explicit N+1 warning).

## 8. Concurrency / idempotency

- **Message append:** `SELECT ... FOR UPDATE` on the parent session row
  serializes concurrent appenders to the *same* session before computing
  `MAX(sequence_number) + 1`; the `UniqueConstraint(session_id,
  sequence_number)` remains the real backstop regardless. This is an
  ordinary Postgres row lock scoped to one transaction — not a distributed
  lock.
- **Context-ref creation:** `INSERT ... ON CONFLICT DO NOTHING` against a
  real partial unique index (section 5) — correctness comes from the
  database, not a Python check-then-act.
- **Context-ref removal:** idempotent at the row level — the context ref is
  looked up by `(session_id, id)` immediately before deletion within the
  same transaction; a concurrently-already-deleted ref surfaces as a
  deterministic 404, never a crash.

## 9. Security / trust boundary

Message content, session titles, and `graph_camera_state` are untrusted
user-controlled data. They are never interpolated into SQL (every query in
`app/investigation/service.py` is parameterized through SQLAlchemy Core/ORM
constructs — no f-string-built SQL anywhere), never executed, and never
permitted to alter `repository_id`/`revision_id`/any FK column (those are
always separate, independently-typed parameters, never derived from
message/title content). Sizes are bounded: titles ≤ 200 characters, message
content ≤ 20,000 characters, both enforced at the Pydantic request-model
layer (automatic 422) and re-checked in the service layer
(`InvalidUpdateError` → 400) for callers that bypass the API.

**No LLM call exists anywhere in this phase.** Any future prompt that
includes `SessionMessage.content` must treat it as untrusted input — this
module makes no claim about prompt-injection safety beyond "this phase
never sends session content anywhere that interprets it as instructions."

## 10. What this phase does not do

No frontend. No authentication/user/tenant identity (sessions are
repository-scoped only, consistent with the rest of this codebase's v1
scope). No hard-delete endpoint for a whole session (archiving via `PATCH
status=archived` is the only supported lifecycle "removal"; the existing
`ON DELETE CASCADE` FKs mean a hard delete, if ever added, already has
well-defined cascade behavior for free — but no such endpoint is exposed in
v1). No event-sourcing system — `SessionMessage` stays a flat, append-only
log with an integer sequence, nothing more general.
