# Automation Intelligence (v1)

Automation Intelligence answers "what changed in this revision, and what
does that invalidate?" for one revision against its git parent. It performs
**incremental change detection and invalidation planning**, and it runs the
revision-scoped analyzers as **deterministic full-revision rebuilds**: there
is no partial or incremental execution mode for any stage.

Code: `app/automation/` (orchestration in `service.py`), route
`POST /repositories/{repository_id}/automation/analyze`
(`app/api/routes/automation.py`). No LLM, embeddings or fuzzy matching are
used anywhere in automation.

## 1. Pipeline

`run_automation_analysis(db, repository_id, revision_id, requested_parent_revision_id)`:

1. Load the current revision; reject it unless it belongs to the repository
   (`RevisionNotFoundError`).
2. Resolve and verify the parent (section 2). `mismatch` or `not_found`
   raises `ParentVerificationError`.
3. Take the per-revision rebuild lock (section 8), then upsert the run row
   (section 7).
4. File delta (section 3).
5. Parsing, then graph construction, as full-revision rebuilds (section 5).
6. If both succeeded: symbol delta, change impact, testing invalidation, then
   the RAG chunk rebuild (sections 4–6).
7. Security invalidation decision (section 6).
8. Deterministic change summary, limitations, and the run's final status.

## 2. Parent resolution (`parent_resolution.py`)

The parent is a git fact, `RepositoryRevision.parent_sha`, captured at
ingestion. It is never guessed, never "the most recent other revision", and
never an arbitrary pair of UUIDs.

| Situation | Result |
|---|---|
| Current revision is a root commit, no parent requested | `no_parent` |
| Root commit, a parent requested | `mismatch` (no query is issued) |
| Requested parent not found, or belongs to another repository | `not_found` |
| Requested parent exists in this repository but is not the git parent | `mismatch` |
| Requested parent is the git parent | `resolved` |
| No parent requested; the parent commit was ingested in this repository | `resolved` (looked up by `repository_id` + `commit_sha`) |
| No parent requested; the parent commit was never ingested here | `no_parent` |

A revision with the same commit in a *different* repository is never used.
`no_parent` is not an error: every file is then reported as added.

## 3. File delta (`revision_delta.py`)

Reads only `SourceFile` identity columns (`path`, `content_hash`) for the
two revisions, scoped by `repository_id` and `revision_id`. Paths only in
the current revision are **added**, only in the parent **deleted**; common
paths are **modified** when `content_hash` differs, otherwise **unchanged**.

Renames are not detected: ingestion records no git rename signal, so a
renamed file appears as one delete plus one add, and
`rename_detection` is always `"unsupported"`.

## 4. Symbol delta (`symbol_delta.py`)

Bounded to the added, deleted and modified files only; symbols of unchanged
files are never loaded. Symbols are compared across the two revisions by
identity `(file_path, kind, qualified_name)`, never by `Symbol.id` (each
revision has its own rows). Equal identities within one revision are grouped
and paired deterministically.

Content comparison uses `signature` plus the symbol's body text, so
decorator, signature, docstring and body changes count as **modified**,
while a pure line shift with identical content stays **unchanged**. If
either side's source text is unavailable (storage-key-backed content), the
symbol is reported as modified, never proven unchanged.

## 5. Parsing, graph and impact

**Parsing and graph construction** call the existing, unmodified
`app.parsing.service.run_parsing` and `app.graph.service.run_graph_construction`.
Both rebuild the whole revision. Each result carries a
`StageExecutionPlan` whose `invalidated` is the real change-detection
decision (did any file change) and whose `execution_strategy` is always
`"full_revision_rebuild"` (the only value of `EXECUTION_STRATEGIES`), so
"no files changed" is never mistaken for "this stage was skipped". If
parsing fails, graph construction is skipped; if either fails, the symbol
delta, impact, testing invalidation and RAG rebuild are skipped.

**Change impact (`impact_reuse.py`)** reuses
`app.impact.traversal.compute_impact` directly, once per added or modified
symbol, over the **current revision's graph only**. It does not create
impact `AnalysisRun` rows. Results are merged across seeds: a symbol keeps
its shallowest depth, and one reached directly by any seed stays direct.
At most `MAX_CHANGED_SYMBOLS_TRAVERSED = 200` changed symbols are traversed;
beyond that the summary is marked `truncated`.

**Deleted symbols** have no node in the current graph. Their impact is
reported per symbol as `impact_status = "unavailable_deleted_symbol"`, with
a reason, never computed and never zero; the run's limitations say so.
Traversing the parent revision's graph is not done.

## 6. Invalidation decisions

| Subsystem | What automation does | What it does not do |
|---|---|---|
| RAG (`rag_invalidation.py`) | Rebuilds the revision's chunks with the unmodified `run_chunk_generation` (`status = "rebuilt"` with `chunks_invalidated`, or `"failed"` with detail). | Never regenerates embeddings: chunk ids are new on every rebuild, so parent embeddings cannot be reused. `embeddings_require_regeneration` is always `true`; embedding is a separate, explicit request. |
| Testing (`testing_invalidation.py`) | Read-only. Lists the parent revision's `GeneratedTest` ids whose target symbol was deleted or modified (stale), and the current symbols that were added or modified (`changed_target_symbol_ids`). With no parent there is nothing stale. | Never regenerates tests, and never moves a test to the current revision. |
| Security (`security_invalidation.py`) | Pure decision `rerun_required` with a reason: always true with no parent; true when a supported dependency manifest or Python source changed, or other files changed (stated as "neither scanner's input is known to be affected"); false only when no file changed. | Never runs a scanner. Observations are revision-scoped, so even with no changes the current revision has none until a scan is run. |
| Documentation | Nothing. | Automation does **not** invalidate, regenerate or report on documentation artifacts. They are revision-scoped (`documentation-intelligence.md`), so a new revision simply has none until documentation is generated for it. |

## 7. AnalysisRun orchestration and idempotency (`persistence.py`)

Automation records exactly **one** `AnalysisRun` (`analysis_type =
'automation_analysis'`) per logical identity
`(repository_id, revision_id, parent_revision_id)`, unlike every other
analyzer's append-only history. `start_or_reuse_run` is a single atomic
`INSERT ... ON CONFLICT DO UPDATE` (no pending ORM object is added first)
against one of two partial unique indexes from migration `0007`:

- `uq_analysis_runs_automation_with_parent` — `(repository_id, revision_id,
  parent_revision_id, analysis_type)` WHERE `analysis_type =
  'automation_analysis' AND parent_revision_id IS NOT NULL`;
- `uq_analysis_runs_automation_without_parent` — `(repository_id,
  revision_id, analysis_type)` WHERE `analysis_type = 'automation_analysis'
  AND parent_revision_id IS NULL`.

Repeated calls, including retries after a `failed` or `partial` run, reset
that same row to `running` and recompute everything; other analyzer types
never match these indexes. The parsing, graph and chunking runs that
automation triggers are those services' own append-only runs.

Final status: `failed` if parsing failed; `partial` if graph construction
or the RAG rebuild failed; otherwise `completed`. An unexpected exception
marks the run `failed` (`AutomationError`, 502). `finish_run` persists
counts only in `config` (`parent_revision_id`, changed file/symbol counts,
impacted symbol and affected file counts, limitations). The execution
strategy and per-subsystem invalidation decisions are returned in the
response but **not persisted** (the repository overview reports only the
persisted counts).

**Deleting the parent revision** deletes the automation runs computed
against it (`parent_revision_id` FK is `ON DELETE CASCADE`, migration
`0011`), instead of rewriting them into a different, possibly already-taken,
no-parent identity. Deleting the repository deletes everything.

## 8. Concurrency

Automation takes the same per-revision rebuild lock as the analysis
pipeline (`app/db/revision_lock.py`, see `analysis-pipeline.md`) **before**
touching its run row. A concurrent automation or pipeline request for the
same revision gets `RevisionBusyError` (409) and nothing runs. Concurrent
identical requests therefore yield one logical run and one symbol set.

## 9. API

`POST /repositories/{repository_id}/automation/analyze` with
`{revision_id, parent_revision_id?}`:

| Case | Status |
|---|---|
| Invalid UUID | 422 |
| Unknown revision, or a revision of another repository | 404 |
| Parent `not_found` or `mismatch` | 409 |
| Revision currently being rebuilt | 409 |
| Unexpected failure | 502 |

The response carries the file and symbol deltas (each list capped at
`MAX_DELTA_ITEMS_IN_RESPONSE = 200`, with `truncated`), the three stage
execution plans, the impact summary (including deleted-symbol impacts),
the RAG, testing and security plans, a deterministic count summary, the
limitations, and a fixed `execution_note` restating the
full-revision-rebuild strategy.

## 10. Limitations

- No partial or incremental execution: parsing, graph construction and
  chunking always rebuild the whole revision.
- Renames are not detected (delete + add).
- Impact of deleted symbols is unavailable, never computed.
- Change impact traverses at most 200 changed symbols.
- Embeddings, tests and security scans are never run by automation; only
  their invalidation is decided.
- Documentation is not invalidated or regenerated.
- Execution strategy and invalidation decisions are not persisted.
