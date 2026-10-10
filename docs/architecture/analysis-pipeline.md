# Analysis Pipeline and Code Intelligence Reads (Product Integration v1)

Code: `apps/api/app/pipeline/`, `apps/api/app/codeintel/`, routes
`app/api/routes/pipeline.py` and `app/api/routes/code.py`.

## 1. Workflow

```
POST /repositories/ingest                                  ingested
POST /repositories/{id}/revisions/{rev}/analyze            parse, graph, index
GET  /repositories/{id}/overview?revision_id=              read model
GET  .../revisions/{rev}/files | symbols | graph/neighborhood
POST /repositories/{id}/impact, /qa                        (existing)
```

Ingestion only stores files. Analysis is a separate, explicit request. The UI
labels the two states separately ("ingested" vs. the stage table).

## 2. Pipeline

A thin, **synchronous** orchestrator. The HTTP response arrives after every
requested stage has finished or failed. There is no background job, no
progress percentage and no polling.

| Stage | Service (unchanged) | AnalysisRun |
|---|---|---|
| parsing | `parsing.service.run_parsing` | `parsing` |
| graph | `graph.service.run_graph_construction` | `graph_build` |
| rag_chunking | `rag.persistence.run_chunk_generation` | `rag_indexing`, chunker |
| rag_embedding | `rag.embeddings.run_embedding_generation` | `rag_indexing`, embedder |

The orchestrator never parses, resolves, chunks or embeds anything itself.

### Request

`{run_parsing, run_graph, run_rag, force}`: all default to `true`, except
`force`, which defaults to `false`. An empty body runs the full safe pipeline.

### Stage status

A stage's status is the latest run's `AnalysisRun.status`, or one of:
- `not_run`: no run exists.
- `unavailable`: the stage cannot run in this deployment, for example when no
  embedding provider is configured. Nothing is recorded for it.

`up_to_date` is reported separately, because a `completed` run can be
outdated. Every stage is a revision-scoped rebuild:

- re-parsing keeps the id of every symbol it reproduces, updates those rows in
  place, inserts new symbols and deletes only the symbols it no longer
  produces (`source-intelligence.md` sections 8 and 15). A deleted symbol's
  `GraphEdge` rows cascade away and its `CodeChunk.symbol_id` is set to NULL;
  kept symbols keep theirs. If a symbol it would delete is still referenced by
  an investigation-session breadcrumb, the parse is refused and nothing
  changes (the parsing stage fails);
- graph construction deletes the revision's `GraphEdge` rows and rebuilds
  them;
- re-chunking deletes the revision's `CodeChunk` rows, which cascade-deletes
  their `Embedding` rows.

Each stage is current under these rules:

| Stage | Current when |
|---|---|
| parsing | its latest run completed |
| graph | it completed after the latest parsing run finished |
| rag_chunking | it completed after the latest parsing run finished |
| rag_embedding | it completed after the latest chunking run finished, with the embedding model configured now |

### Overall status

- `completed`: every requested stage is current and none failed in this
  request.
- `partial`: some requested stages succeeded and some did not.
- `failed`: no requested stage succeeded.

Each stage result has an `error` field, set when the stage ran in this request
and failed. A stage that raised before its service recorded a run could leave
an older run looking current, so success is never inferred from
`up_to_date` alone. Any exception from a stage, typed or not, becomes a
stage failure, with the session rolled back; it is never reported as
success.

When no embedding provider is configured, a full request is therefore
`partial`, never `completed`.

### Idempotency

- A requested stage that is current is **reused**: no new AnalysisRun.
- A stage that is failed, outdated or never run is **re-run**, which appends
  a new AnalysisRun. These analyzers are append-only history, so a failed run
  is preserved and never overwritten.
- A stage is also re-run when its upstream was rebuilt in the same request,
  or when `force` is set.
- A stage whose prerequisite is not current is **skipped**. It never runs on
  stale input.

### Concurrency

**Why a lock is needed.** Graph construction and chunking each delete a
revision's rows and re-insert them, and parsing inserts every symbol it has
not seen before. `Symbol` has no natural-key uniqueness, so two concurrent
parses of one revision under READ COMMITTED can each read the same existing
rows and each insert the same new symbols (on a first parse, every symbol),
neither seeing the other's uncommitted inserts. Duplicate symbols then produce
duplicate edges and chunks.

**Both rebuild entry points take one shared lock**
(`app/db/revision_lock.py::revision_rebuild_lock`):
- the analysis pipeline, for the whole stage sequence;
- Automation Intelligence, for its whole run. It acquires the lock before it
  touches its automation run row.

If the revision is held, the second caller gets **409** and nothing runs.

**How the lock is held.**
- It is a PostgreSQL *session-level* advisory lock, keyed by the revision id.
  A transaction-level lock would not work: the stage services commit several
  times, and the first commit would release it.
- It lives on a **dedicated connection**, checked out for the whole protected
  block. The ORM session may return its connection to the pool between stage
  commits, so it can't hold the lock.
- That connection is committed right after the lock is acquired. It then sits
  idle rather than idle-in-transaction, so `idle_in_transaction_session_timeout`
  cannot terminate it (and silently release the lock) mid-rebuild.
- The unlock runs after the protected block has finished, whether it
  succeeded or failed.
- If the lock might still be held when the block exits (for example the
  unlock failed), the connection is **invalidated**, not returned to the
  pool. Ending the server session releases the lock, so it can never leak
  into a pooled connection.

**What it guarantees.** Mutual exclusion between lock holders. It does not
block unrelated writers.

**Reads during a rebuild.** These include the overview, the code reads,
impact and Q&A. They see either the old or the new committed state of each
stage, because each stage rebuild is one transaction. Between the parsing
commit and the graph commit, a revision briefly has the new symbols with only
the previous graph's edges between kept symbols: none for newly added
symbols, and none at all after a first parse. Pipeline status reports the
graph as outdated in that window. This is transient, not corruption.

**Status reads.** `GET .../analysis` reports `analysis_in_progress` by
reading `pg_locks`. It never takes the lock, so a status read cannot cause a
concurrent analyze to be rejected. The endpoint is read-only and creates no
AnalysisRun.

### Revision isolation

Every stage receives the explicit `revision_id`; the pipeline never reads
`Repository.current_revision_id`. Ownership is checked first:
- unknown repository or revision: 404;
- a revision belonging to another repository: 409.

## 3. Read API

All reads are GETs and read-only. Each first verifies that the repository
exists and that the revision belongs to it (404/409), then filters every
query by both `repository_id` and `revision_id`. FastAPI rejects
out-of-range bounds with 422 before any query runs.

| Endpoint | Bounds | Notes |
|---|---|---|
| `GET /repositories/{id}/revisions` | limit 1 to 200 | newest commit first; `is_current` flag |
| `GET .../files?directory=&limit=&offset=` | limit 1 to 200; at most 500 subdirectories | immediate children only; `directory` is a DB path filter (`..`, `.` and empty segments are rejected) and never touches the filesystem |
| `GET .../files/{file_id}` | at most 1000 symbols | metadata, symbols in line order, `source_available` |
| `GET .../files/{file_id}/source?start_line=&end_line=` | at most 1000 lines per request | from the content-addressed blob; externally stored content returns 404, never invented text |
| `GET .../symbols?q=&limit=` | limit 1 to 50, q up to 300 characters | case-insensitive substring match with LIKE wildcards escaped; exact matches first |
| `GET .../symbols/{symbol_id}` | | facts, parent, child count, distinct dependents and dependencies, external reference count. The workstation does not call it yet. |
| `GET .../graph/neighborhood?symbol_id=&direction=&depth=&limit=` | depth 1 to 4, limit 1 to 200 nodes, 2000 edge rows per hop | see section 4 |
| `GET .../security/findings` | limit 1 to 200 | findings of the latest completed `security_scan` of this revision, ordered by severity |
| `GET .../testing/generated-tests` | limit 1 to 200 | each test with its latest execution; no test source |

## 4. Graph neighborhood

A bounded breadth-first walk. Each hop is one grouped SQL query over that
hop's frontier, so there are at most 2 × depth traversal queries plus one
node query and one external-reference query.

**Directions:**
- `dependents` walks edges backwards: who depends on the focus.
- `dependencies` walks forwards: what the focus depends on.
- `both` runs each walk separately from the focus.

**What comes back:**
- Edges keep the stored orientation: **source = dependent, target =
  dependency**. They are collapsed per (source, target, relationship), with an
  `occurrences` count of evidence rows.
- Nodes carry their shortest hop `depth` and `reached_via`, which is one of
  `focus`, `dependents`, `dependencies` or `both`.
- Edges are returned only when both endpoints are returned nodes.
- External (unresolved) targets of the focus come back as names in
  `focus_external_references` and are never turned into nodes.

**Limits:** cycles terminate through the visited set. Reaching the node limit
or the per-hop edge cap sets `truncated`.

## 5. Frontend use

- **Overview and Home:** the analysis panel (GET status; POST only on click).
- **Architecture:** the neighborhood endpoint, drawn as returned. Depth-ring
  rendering remains only on the Impact page, where the data is impact depths
  and not topology.
- **Files:** directory browsing, file detail and 400-line source windows.
- **Q&A:** index state comes from the pipeline status, so outdated chunks
  show as "indexing has not completed".
- **Security and Testing:** persisted lists.
- **Revision switcher:** the revisions listing.
- **Investigations:** "Save to investigation" maps a selection to
  `selected_symbol`, `selected_file`, `graph_focus` (focus stored in
  `graph_camera_state`) or `finding_reference`. Nothing is written until the
  user submits.
- No page load issues a POST. Impact targets and Q&A questions that arrive in
  the URL (from links, the command bar, saved investigations or a reload)
  only pre-fill the form; the request runs on click.
- The details-panel selection is reset when the revision changes. A symbol,
  file or finding id from one revision never stays on screen as the next
  revision's data.

## 6. Known limitations

- **No supporting indexes.** `generated_tests` has no index on
  `(repository_id, revision_id)`, and `test_executions` has none on
  `generated_test_id`. PostgreSQL does not index foreign-key columns
  automatically, so the persisted-tests listing scans those tables. This is
  correct, but slower on large histories, and adding the indexes needs a
  migration.
- **Substring search scans.** Symbol search (`ILIKE '%q%'`) and directory
  listing (path prefix) are bounded per revision, but are not index-assisted
  within the revision.
- **Not executed against PostgreSQL.** The following have run only in tests
  that are skipped without a live server: the advisory-lock server semantics,
  the `pg_locks` status query, and the per-hop graph SQL. Statement
  compilation and per-table scoping are checked without a database by
  `tests/codeintel/test_service_static.py` and
  `tests/overview/test_service_static.py`. The lock's connection lifecycle is
  checked by `tests/db/test_revision_lock.py`.
