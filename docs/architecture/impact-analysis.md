# Impact Analysis v1

> Given a code symbol or file that is changing, determine what depends on
> it, how far the impact propagates, and why.

This document is written before implementation, per the phase's own
ordering requirement. It covers the v1 design only: deterministic,
structural, reverse-graph traversal over the already-persisted Python
dependency graph (`docs/architecture/graph-construction.md`). No LLM, no
RAG, no risk scoring, no UI.

## 0. Task 1 audit result

Re-read directly from `app/graph/`, `app/models/graph.py`,
`app/models/analysis.py`, `app/models/symbol.py`, `app/models/source.py`
before writing any code:

- **No adjacency/query primitive exists yet.** `app/graph/service.py` only
  *writes* `GraphEdge` rows (graph construction); nothing in the codebase
  reads them back. This phase is the first reader.
- **Indexes already support efficient traversal in both directions**:
  `ix_graph_edges_target_symbol_id_revision_id` (target_symbol_id,
  revision_id) and `ix_graph_edges_source_symbol_id_revision_id`
  (source_symbol_id, revision_id) — one index per traversal direction,
  already built during the graph-construction hardening pass specifically
  because "impact analysis (backward) and dependency listing (forward) are
  equally common, structurally different query patterns."
- **Revisions**: identified by `RepositoryRevision.id` (uuid), always
  paired with `repository_id` via the project's composite-FK convention.
- **Symbols**: identified by `Symbol.id` (uuid pk). `qualified_name` is
  indexed (`ix_symbols_repository_id_qualified_name`) but **not** unique —
  qualified-name ambiguity within one revision is a real, schema-permitted
  case, not a hypothetical. Target resolution must handle it explicitly
  (section 7).
- **SourceFiles relate to Symbols** via `Symbol.file_id -> SourceFile.id`
  (same-revision composite FK, `source_files.path` unique per revision) —
  this is the join used for file-level aggregation (section 6).
- **`AnalysisRun`** already anticipates this phase:
  `ANALYSIS_TYPES` in `app/models/analysis.py` already includes
  `"impact_analysis"` as an allowed value, unused until now. No second
  run-tracking abstraction is introduced (section 8).
- **`GraphEdge.source_symbol_id` is `NOT NULL`**; only `target_symbol_id`
  is nullable (exactly one of `target_symbol_id`/`target_external_ref` is
  populated, enforced by `ck_graph_edges_exactly_one_target`). This is a
  schema-level guarantee, not just an application convention: an external
  reference can only ever appear as a *target*, never as a *source* — so a
  reverse traversal (which only ever follows `source_symbol_id` backward
  from a matched `target_symbol_id`) can **never** produce an external
  reference as a frontier node. See section 7 of
  `graph-construction.md` for why.

**Conclusion: nothing about the graph needed to change.** This phase adds a
new, pure read/traversal layer on top of the existing schema.

## 1. The fundamental definition

```text
Impact(target):
    start at target symbol
    follow GraphEdge.target_symbol_id == current_symbol_id
    traverse to GraphEdge.source_symbol_id
    repeat until traversal is exhausted
```

```text
changed symbol
      ↓
reverse graph traversal
      ↓
direct dependents
      ↓
transitive dependents
      ↓
full impact set
```

This is **reverse** traversal relative to the graph's own canonical
direction (`source → target` = `dependent → dependency`, established in
`graph-construction.md` section 5 and unchanged here). Reverse traversal is
correct for impact analysis precisely *because* of that direction
convention: `source_symbol_id` is always the dependent, so walking from a
target backward to its edges' sources is walking from "the thing that
changed" to "the things that depend on it" — one hop at a time, in the
direction the question ("what breaks if I change this?") actually asks.

Forward traversal (`source → target`, "what does X depend on") remains a
legitimate, useful query — dependency inspection, the inverse question —
but it is **not** impact analysis and is not this phase's primary
traversal. v1 implements reverse traversal as the primary operation;
forward traversal is a documented possibility for later, not built now
unless a specific need arises.

## 2. Direct vs. transitive impact, and depth

**Direct impact**: a symbol with a single reverse edge from the target.

```text
A → B
Impact(B) = {A}   (depth 1)
```

**Transitive impact**: symbols reached through multiple reverse-edge hops.

```text
A → B
B → C
C → D
Impact(D) = {C: depth 1, B: depth 2, A: depth 3}
```

**Depth** is the *shortest* reverse-traversal distance from the changed
target — never an arbitrary risk number, never a weighted score. When
multiple paths reach the same symbol at different depths, the shortest one
is recorded (section 4's "shortest depth" requirement). Depth is a graph-
distance fact, not a judgment about how likely or how severe an impact is.

## 3. Relationship type is preserved, never weighted

Every traversed edge keeps its `relationship_type` (`calls`, `imports`,
`inherits`, `implements`, `references`). v1 does **not** assign a weight,
risk tier, or severity to any relationship type — "calls = high risk" is
exactly the kind of fabricated judgment this phase explicitly avoids
(mirroring the confidence-schema correction already made at the graph
layer: a number implying precision we don't have is worse than no number).
If a specific symbol is reached via more than one relationship type across
different edges, all of the relationship types actually observed on the
edges leading to it are retained (section 3 of the result model).

v1 answers **"what depends on this?"** — not **"what is probably going to
break?"**. Those are different questions; only the first is answered here.

## 4. Impact path (structural evidence, not prose)

Because the traversal walks real `GraphEdge` rows, every impacted symbol's
*reason* for being impacted is reconstructible as a concrete chain of
edges — e.g.:

```text
checkout_order --calls--> calculate_total --calls--> apply_discount
```

v1 exposes this as structural data (the edges actually walked to reach a
symbol, via `relationship_types` and the underlying per-hop evidence
locations) — not as LLM-generated prose. AI explanation is an explicit
non-goal of this phase (section 13).

## 5. Result model

```text
ImpactAnalysisResult
    revision_id
    target_symbol_id
    target_symbol              # qualified_name, kind, file_id, file_path
    direct_impacts              # depth == 1
    transitive_impacts          # depth > 1
    impacted_files
    max_depth
    analyzed_at
    analysis_run_id
```

Each impacted symbol:

```text
symbol_id
qualified_name
symbol_kind
file_id
file_path
depth
relationship_types          # the distinct relationship_type values of
                             # edges that lead to this symbol at its
                             # recorded (shortest) depth
parent_symbol_id
```

Each impacted file:

```text
file_id
path
symbol_count                 # impacted symbols in this file (section 6)
minimum_depth                 # shortest depth among those symbols
```

**No fake `confidence`/`risk_score`/`probability`/`severity`.** This exact
mistake was just corrected at the graph layer (`GraphEdge.confidence`); it
is not reintroduced here. If a genuinely deterministic, explicitly defined
metric is wanted later (e.g. "number of distinct call sites," which *is*
countable and real), it can be added as a precisely-named field — never a
vague "risk" number.

## 6. Traversal engine design

`app/impact/` — pure traversal logic separate from DB access, mirroring
`app/graph/`'s existing split (`resolver.py`/`python_extractor.py` pure,
`service.py` the only DB-touching module):

- `app/impact/models.py` — frozen dataclasses: the result model above.
- `app/impact/traversal.py` — pure function: `list[GraphEdge-shaped tuples]
  + target_symbol_id -> ImpactAnalysisResult`. No DB session. No
  SQLAlchemy imports.
- `app/impact/service.py` — the only module with a `Session`: resolves a
  target (section 7), loads the revision's edges once (section 9), calls
  `traversal.py`, records an `AnalysisRun` (section 8).

**Revision isolation**: the pure traversal function never sees more than
one revision's edges — `service.py` loads edges filtered to exactly one
`revision_id` before traversal ever begins, the same pattern
`app/graph/resolver.py`'s `RevisionTables` already established. It is
structurally impossible for the traversal itself to cross revisions,
because it is never given data from more than one.

**Cycle safety**: a `visited` set, seeded with the target symbol's own id
*before* traversal starts. This single design choice handles every cycle
case uniformly:
- A multi-node cycle (`A → B → C → A`) terminates because, on reaching
  back to `A`, `A` is already in `visited` and is not re-added or
  re-expanded.
- A self-loop (`A → A`) terminates for the same reason, and — per the
  explicit preference in Task 11.4 — **the starting symbol never appears
  in its own impact set**, since it was seeded into `visited` from the
  start rather than discovered during traversal.

**Shortest depth**: breadth-first traversal (process the current frontier
fully before advancing to the next depth) guarantees the *first* time any
symbol is reached is automatically its shortest reverse-distance from the
target — a symbol already in `visited` is never re-added at a deeper
level, so no post-hoc "take the minimum" step is needed; BFS order makes
the first visit correct by construction.

**Determinism**: traversal of the same revision + target always produces
the same impacted symbols, depths, relationship metadata, and **ordering**.
Stable order, applied as the final sort step over the complete result
(never relied upon as traversal order, since dict/set iteration order is
not treated as meaningful):

```text
depth ASC, then file_path ASC, then qualified_name ASC, then symbol_id ASC
```

`symbol_id` is the final tiebreaker specifically because it's the only
field guaranteed unique — depth/path/qualified_name could theoretically
coincide (e.g. two methods of the same name in different classes, same
file, same depth), and ordering must still be total and stable.

**External targets**: `target_symbol_id IS NULL` on the edges table means
an edge can never be reached as a *new frontier node* during reverse
traversal — the frontier is built exclusively from `source_symbol_id`
values, which are never `NULL` (section 0). External references are
therefore structurally excluded from ever becoming an internal impact
node; no application-level filtering is needed to enforce this; it falls
out of the schema. No `Symbol` is ever fabricated for one.

**Unresolved references**: produce no `GraphEdge` row at all (established
at the graph-construction phase). A symbol with nothing pointing at it via
a persisted edge simply never enters the frontier — there is nothing to
traverse, and nothing is invented.

## 7. Target resolution

Two entry points:

1. **By `Symbol.id` directly** — the common case once a caller already has
   a specific symbol (e.g. from a prior graph query or API response).
2. **By `qualified_name` + `revision_id`** — for a human- or tool-supplied
   name. Looked up via `(Symbol.revision_id, Symbol.qualified_name)`. Three
   possible outcomes, all explicit, none silent:
   - **Not found**: zero matching symbols → an explicit not-found result.
   - **Ambiguous**: more than one matching symbol in the same revision (the
     schema permits this — section 0) → an explicit ambiguity result,
     listing the candidates. Never guesses one.
   - **Found**: exactly one match → proceed.

No fuzzy matching, no embeddings, no LLM-based disambiguation — a plain,
deterministic equality lookup, matching the same "UNKNOWN is better than
WRONG" principle the graph-construction phase was built around.

## 8. AnalysisRun integration

No second run-tracking abstraction. `AnalysisRun` already has
`analysis_type = "impact_analysis"` reserved (section 0) — this phase is
the first to use it, following the exact same lifecycle
`app/graph/service.py::run_graph_construction` already established:
created with `status="running"` and committed immediately (a durable
record even if the analysis itself later fails), then `"completed"` or
`"failed"` with `error_message` on the same row.

**No new persistence table for impact results.** Per the task's own
explicit default: Postgres's `GraphEdge`/`Symbol` tables remain the durable
source of truth; impact traversal is a deterministic computation derived
from them on demand, and its result is a structured service-layer return
value, not a second copy of the graph. Nothing about the domain model
requires persisting impact results independently — they are fully
reconstructible from the same revision's graph at any time, by definition
deterministically (section 6). If a future phase needs to *cache* impact
results (e.g. for an expensive, frequently-repeated query), that is a
distinct, later decision requiring its own justification — not assumed
here.

## 9. Query strategy

**Chosen approach: load the revision's relevant `GraphEdge` rows into
memory once, traverse in memory.** Not one query per visited node (which
would be an N+1 traversal pattern) and not even a batched per-depth-level
query — a single upfront `SELECT` filtered by `revision_id`, building an
in-memory reverse-adjacency map (`target_symbol_id -> list[edges]`) keyed
exactly the way the traversal needs it.

Why this, over batching by BFS frontier (also a valid v1-acceptable
approach per the task): a single codebase's revision-scoped graph is
bounded by that codebase's own size, not by any multi-tenant or
cross-repository scale concern — loading it once is simple, trivially
deterministic (no risk of a batch query racing against concurrent writes
mid-traversal), and removes *all* further round-trips rather than merely
bounding them by graph diameter. The in-memory structure is rebuilt fresh
on every `run_impact_analysis()` call — it is a rebuildable analysis cache,
never a second source of truth, and never persisted or shared across
calls/requests.

If a future repository's revision graph grows large enough that loading
all edges becomes a real cost, the batched-frontier alternative remains
available without changing the traversal algorithm's semantics — only its
data-loading strategy — since `traversal.py` is pure and takes an already-
loaded edge set, independent of how that set was obtained.

## 10. File-level impact

```text
target_symbol
    ↓
impacted symbols (direct + transitive)
    ↓
unique SourceFiles those symbols belong to
```

One `impacted_files` entry per distinct `file_id` actually reached by an
*impacted* symbol — `symbol_count` (how many impacted symbols are in that
file) and `minimum_depth` (the shallowest depth among them). The target's
own file is **not** included unless some other, actually-impacted symbol
also lives there (e.g. a sibling function in the same file that happens to
depend on the target) — the target itself is never counted as its own
dependent (section 6's cycle-safety / self-exclusion applies identically
here).

## 11. API boundary

`POST /repositories/{repository_id}/impact` — mirrors the one existing
convention (`app/api/routes/ingestion.py`): inline Pydantic request/response
models in the route module, a typed-exception-to-`HTTPException` mapping,
`Depends(get_db)` session injection, `response_model=`. Accepts
`revision_id` plus either `symbol_id` or `qualified_name`. Returns the
structured `ImpactAnalysisResult`, serialized — never a raw ORM object.

## 12. Why no LLM, no risk score, in v1

**Impact Analysis is deterministic evidence first. AI explanation comes
later.** Everything in this document is reconstructible, auditable, and
repeatable from the persisted graph alone — no model call, no
non-determinism, no invented confidence. A future phase may add an LLM
layer that *explains* an already-computed, already-correct impact set in
natural language, or a calibrated risk model trained against real
incident/outcome data — but inventing either now, without the evidence or
calibration to back it, would repeat exactly the mistake just corrected in
the graph layer's `confidence` column.
