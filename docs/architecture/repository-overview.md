# Repository Overview v1

`GET /repositories/{repository_id}/overview[?revision_id=...]`

Code: `apps/api/app/overview/` (`models.py`, `service.py`), route
`apps/api/app/api/routes/overview.py`.

## 1. What it is

A deterministic, read-only, revision-scoped **read model** that aggregates
facts other subsystems already persisted. It answers "what is this
repository, how is it structured, what are its important components, and
what has been analyzed?" It is not a second parser, graph engine, scanner,
test runner, RAG engine, or LLM: it never computes anything those systems
own, never writes, and never calls an AI provider.

Before this phase no overview, dashboard, or statistics aggregation
existed. The only related concept was Documentation Intelligence's
`repository_overview` / `architecture_overview` artifacts, which are
generated, persisted prose. This read model doesn't create, regenerate or
embed them. It only counts them (section 6).

## 2. Computed on demand: not cached, not persisted

Every field is a COUNT/SUM/GROUP BY or a LIMITed ranking over indexed,
revision-scoped tables, so on-demand computation is cheap enough. It
doesn't need a cache, a new table, or a `GeneratedArtifact`. That also
avoids any invalidation problem: the overview can never be staler than the
rows it reads. If caching is added later, its key must include
`(repository_id, revision_id)`.

## 3. Field categories

| Category | Fields |
|---|---|
| **A. Direct facts** | repository identity, revision identity/timestamps, file/byte/symbol/edge/chunk/embedding counts, analysis run statuses and timestamps, finding/test/execution/artifact counts, automation counts copied from the run's persisted config |
| **B. Deterministic derived facts** | language / parsing-status / symbol-kind / relationship distributions, top-level directory and package listings, `connected_node_count`, symbol and module in/out-degree rankings, `is_current` |
| **C. Prose** | none in v1 (no LLM) |

There is no risk score, importance score, health score, or confidence value
anywhere in the response.

## 4. Revision vs. repository scope

All facts come from exactly one resolved `RepositoryRevision`. If
`revision_id` is omitted, `Repository.current_revision_id` is used.

**Revision-local** (every query filters on both `repository_id` and
`revision_id`): statistics, structure, graph, important components,
analysis statuses, security counts (via `security_finding_observations`),
testing, documentation, RAG, automation.

**Repository-current** (reported as such):

- `repository.*`, including `current_revision_id`
- `revision.is_current`, which compares against that pointer at read time
- `security.findings_by_current_triage_status`: `SecurityFinding.status`
  is repository-level mutable triage state
- `sessions.*`: investigation sessions are repository-level

## 5. Definitions

**Statistics**
- `stored_file_count`: `SourceFile` rows. Ingestion stores text files only,
  and the raw git tracked-file count isn't persisted anywhere, so it isn't
  reported.
- `recorded_file_count`: `RepositoryRevision.file_count`.
- `language_file_count`: files with a non-NULL `language` (the ingestion
  extension map). Contents are never inspected.
- `total_source_bytes`: `SUM(size_bytes)`.

**Structure**
- The top-level directory is the first path segment of files that have one.
  Ordered by file count desc, then name asc. At most 50 are returned, with
  `top_level_directory_count` and a `truncated` flag.
- `python_module_count` is the number of parser-emitted `kind="module"`
  symbols.
- A top-level package is a module symbol whose file is exactly
  `<segment>/__init__.py`, ordered by qualified name, at most 50.
- Module names are always the parser's own `qualified_name`
  (`module_dotted_name`). The overview never derives namespaces itself.

**Graph** (canonical invariant: `source = dependent`, `target = dependency`)
- `internal_edge_count`: edges with `target_symbol_id`.
- `external_edge_count`: edges with only `target_external_ref`.
- `node_count`: `Symbol` rows.
- `connected_node_count`: distinct symbols on at least one internal,
  non-self edge.

**Symbol rankings** (non-module symbols, internal non-self edges, any
relationship type)
- `in_degree(s)` is the number of distinct other symbols with an edge to
  `s`: its direct dependents.
- `out_degree(s)` is the number of distinct other symbols `s` has an edge
  to: its direct internal dependencies.
- "Most depended-upon" orders by `in_degree` desc, and "most
  dependency-heavy" by `out_degree` desc. Ties break by qualified name asc,
  then symbol id asc. A symbol only appears in a ranking if its degree is
  at least 1.

**Module rankings** (module = `SourceFile`; only edges between symbols in
different files count)
- `in_degree(f)` is the number of distinct other files with a symbol
  depending on a symbol in `f`.
- `out_degree(f)` is the reverse.
- Ties break by path asc, then file id asc.

Every ranking returns at most 10 entries. Each query fetches 11 rows, so the
`*_truncated` flag is exact.

## 6. Subsystem status

`analysis_statuses` has one entry per `AnalysisRun.ANALYSIS_TYPES`, in that
fixed order. Its status is the latest run's `AnalysisRun.status` (`pending`,
`running`, `completed`, `partial`, `failed` or `cancelled`), or `not_run`
when no run of that type exists for the revision. `not_run` is never
reported as healthy, and no state outside that vocabulary is invented.

"Latest" means `started_at` desc (runs never started sort last), then
`created_at` desc, then id desc. `started_at` comes first because
automation runs are upserted in place.

| Subsystem | What is reported |
|---|---|
| **Security** | The latest `security_scan` run's status (`failed` when no scanner completed; see `domain-model.md` section 7, "Scan outcomes"). Finding counts come from the latest *completed* scan's observations in this revision (`findings_run_id`, null when no scan completed), the same scoping as `security/service.py::_load_findings_for_run`. Severity uses the fixed order `critical…info`, then `unknown` for NULL. Severity is scanner-reported, never classified here, and there is no risk score. |
| **Testing** | `GeneratedTest` counts by status and `TestExecution` counts by status for the revision. Coverage is not reported. |
| **Documentation** | Counts of `GeneratedArtifact(kind="documentation")` rows by type and by generation method, plus the latest `updated_at`. Content is never loaded or regenerated. |
| **RAG** | Chunk count, distinct indexed files, and embedding count (embeddings are joined through this revision's chunks). The chunker and embedder runs are reported separately by their `ANALYZER_NAME`. No retrieval is performed. |
| **Automation** | The latest `automation_analysis` run's status and `parent_revision_id`. Counts are copied from its persisted `config` only when they are real integers; otherwise they are `null` ("not recorded", never 0). Execution strategy and per-subsystem invalidation decisions are not persisted, so they are not reported. |
| **Sessions** | Reuses `investigation/service.py::list_recent_sessions` (limit 5): one count query plus one aggregated query. It never loads message bodies and never updates `last_active_at`. |

## 7. Query plan

At most 31 focused queries, each scoped by `(repository_id, revision_id)`
and served by the existing revision indexes:
- statistics: 4
- structure: 6
- graph: 3
- symbol rankings: 2
- module rankings: 2, plus 1 module-name lookup when a ranking is non-empty
- run statuses: 1 window query
- security: 1 run lookup and 2 aggregates
- testing: 2
- documentation: 3
- RAG: 2
- sessions: 2

`tests/overview/test_service_static.py` records 30 statements on an empty
revision.

No file content is read. The graph is never materialized in Python: the
rankings and `connected_node_count` are single SQL aggregates over the
revision's edges, so they scale linearly with that revision's edge count.
Each loop runs at most once per query, with no N+1.

## 8. Errors

| Condition | Error | HTTP |
|---|---|---|
| Unknown repository | `RepositoryNotFoundError` | 404 |
| Unknown revision, or no current revision when none was requested | `RevisionNotFoundError` | 404 |
| Revision belongs to another repository | `RevisionConflictError` | 409 |
| Malformed id | | 422 |

## 9. API shape

The response model is the frozen `RepositoryOverview` dataclass, which
FastAPI validates and serializes through Pydantic: UUIDs and datetimes
become ISO strings and tuples become lists. No ORM object is ever returned.
The response also includes `limitations`, a fixed list of the gaps
described above.

## 10. Schema

None. The overview is purely a read path.
