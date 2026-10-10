# DevLens Database Design

This document is the implementation of `docs/architecture/domain-model.md` in
PostgreSQL/SQLAlchemy terms. Where this document makes a decision the domain
model left open, or corrects something in it, that's called out explicitly —
this document is the one to trust for what's actually implemented.

Code: `apps/api/app/models/` (SQLAlchemy 2.x typed declarative models),
`apps/api/app/db/` (engine/session, naming convention), `apps/api/alembic/`
(migrations). When this schema was first written nothing beyond a
`/health/db` readiness check used it; today the subsystem services under
`apps/api/app/` (ingestion, parsing, graph, impact, rag, testing, security,
automation, documentation, investigation, overview, pipeline) read and write
these models.

## Status of validation

**Current status.** The schema has since been validated against a live
PostgreSQL 16 instance with pgvector: all migrations apply from an empty
database to a single head, a full downgrade to base and re-upgrade succeed,
a `pg_dump` of the migrated schema matches the models' `create_all` schema
apart from column order, and the database test suites in `apps/api/tests/`
run against it (they skip only when no database is reachable). The rest of
this section records the original status when the schema was first written.

**Original status.** No live PostgreSQL instance was available in the environment this was built
in (Docker is not installed on this machine — same gap noted in the Phase 0
report). Two independent kinds of validation were possible without one, and
were both run:

1. **Static DDL compilation**: `sqlalchemy.create_mock_engine` compiles the
   *entire* schema (tables, composite FKs, CHECK constraints, partial unique
   indexes, the pgvector column) against the real PostgreSQL dialect, with no
   connection — this catches syntax errors, type errors, and malformed
   constraint definitions.
2. **Alembic offline mode**: `alembic upgrade head --sql` actually executes
   the hand-written migration file's `upgrade()` function and renders literal
   SQL, again with no connection. This exercises the migration file itself,
   not just the ORM models, and was diffed against (1) to confirm every
   table, index, and constraint name matches byte-for-byte.

**Not validated**: an actual `CREATE EXTENSION vector`, an actual `alembic
upgrade head` against a running Postgres, actual INSERT/UPDATE/DELETE
behavior (does `ON DELETE CASCADE` really cascade the way intended, does a
CHECK constraint really reject what it's supposed to). The test suite in
`apps/api/tests/db/` is written to validate exactly that — real inserts
against real constraints — but every test in it currently **skips** with an
explicit reason ("PostgreSQL not reachable") rather than silently passing.
Run `make infra-up && uv run pytest` once Docker is available to get real
answers from those 25 tests.

The static validation did catch one real bug before it reached a migration
file: a naming double-prefix (see "CHECK constraint naming" below).

## 1. Mapping: domain entities → tables

| Domain entity | Table | Category |
|---|---|---|
| Repository | `repositories` | source truth |
| RepositoryRevision | `repository_revisions` | source truth |
| SourceBlob | `source_blobs` | source truth |
| SourceFile | `source_files` | source truth (+ one derived cache field) |
| Symbol | `symbols` | derived |
| GraphEdge | `graph_edges` | derived |
| AnalysisRun | `analysis_runs` | connective (produced-by record) |
| SecurityFinding | `security_findings` | derived, mutable identity |
| SecurityFindingObservation | `security_finding_observations` | derived, immutable history |
| CodeChunk | `code_chunks` | derived |
| Embedding | `embeddings` | derived |
| GeneratedTest | `generated_tests` | derived |
| TestExecution | `test_executions` | derived |
| GeneratedArtifact | `generated_artifacts` | derived |
| InvestigationSession | `investigation_sessions` | investigation state |
| SessionMessage | `session_messages` | investigation state |
| SessionContextRef | `session_context_refs` | investigation state |

17 tables, matching the domain model's entity count exactly — no table was
added for product reasons, only for schema-correctness reasons (see §2's two
additions).

## 2. Deliberate additions beyond the domain document

Two gaps in the domain model needed filling to be implementable, both purely
structural, not new product behavior:

1. **`SessionContextRef.finding_id`**: the domain doc named `'finding_reference'`
   as a `ref_type` but gave the table no column to back it. Added
   `finding_id` (FK → `security_findings.id`, `ON DELETE SET NULL`).
2. **`SecurityFindingObservation.repository_id`**: the domain doc didn't
   carry `repository_id` on this table, but design-audit item 5 (repository/
   revision consistency) requires it to build the composite FK described
   below. Added as a denormalized column, same convention as everywhere else.

## 3. Repository/revision integrity: the composite-FK pattern

**The invariant**: wherever a row carries both `repository_id` and
`revision_id`, those two values must refer to the same repository — the
database must reject a row claiming to belong to repository A's revision but
tagged with repository B's id.

**The mechanism**: `repository_revisions` carries a unique constraint on
`(repository_id, id)` *in addition to* its primary key on `id` alone. Every
child table with both columns (`source_files`, `symbols`, `graph_edges`,
`analysis_runs`, `code_chunks`, `generated_artifacts`, `generated_tests`,
`security_finding_observations`) then declares a **composite foreign key**
`(repository_id, revision_id) → repository_revisions(repository_id, id)`
instead of a plain FK on `revision_id` alone. If `repository_id` doesn't
match the revision's actual owner, no row in `repository_revisions` satisfies
both columns at once, and the insert is rejected — enforced by Postgres, not
application code. `tests/db/test_graph_edge.py::test_edge_repository_must_match_revisions_repository`
exercises this directly (once Postgres is available to run it).

**Extended beyond the literal ask**: the same mechanism is also applied one
level deeper, to prevent a different class of bug the domain model's own
immutability requirement implies but didn't spell out — a `Symbol` pointing
at a `SourceFile` from the *wrong revision*, or a `GraphEdge` pointing at a
`Symbol` from the wrong revision. `source_files` carries `unique(revision_id, id)`,
and `symbols.file_id`/`graph_edges.source_symbol_id`/`graph_edges.target_symbol_id`
are FK'd as `(revision_id, file_id) → source_files(revision_id, id)` and
`(revision_id, source_symbol_id) → symbols(revision_id, id)` respectively.
This is the same pattern, same reasoning, just applied to every
revision-scoped parent/child pair, not only the top-level repository/revision
one.

### Where the pattern stops, and why

`Repository.current_revision_id`, `CodeChunk.symbol_id`,
`InvestigationSession.current_revision_id`/
`started_at_revision_id`, and all of `GeneratedArtifact`'s `related_*`
columns are **plain, single-column foreign keys**, not composite — even
though composing them against revision/repository would seem consistent with
everything above. The reason is a genuine Postgres mechanical constraint, not
an oversight:

> A composite FK's `ON DELETE SET NULL` action nulls **every** local column
> in that FK when the referenced row disappears — not just the nullable one.
> Every place above where I'd want composite-FK + SET NULL, one of the two
> local columns is `repository_id` or `revision_id`, which is `NOT NULL` and
> load-bearing. Postgres would try to null a `NOT NULL` column and the
> operation would be invalid.

`ON DELETE CASCADE` doesn't have this problem (the whole row disappears, no
column is selectively nulled), which is exactly why `graph_edges.target_symbol_id`
*is* composited with `CASCADE` even though it's nullable — deleting the
target symbol legitimately should delete the edge that asserted a dependency
on it. For every column in the list above, `CASCADE` would be semantically
wrong (deleting a class must not cascade-delete the artifacts/chunks that
merely *reference* it) and `SET NULL` is mechanically unsafe, so they stay
plain FKs — the invariant is left as an application-enforced boundary rather
than a DB-enforced one. **§3a documents every one of these, field by field,
including which ones now have an ORM-level `@validates` backstop added
during the hardening pass.**

**`Symbol.parent_symbol_id` is the one exception that *does* get full DB
enforcement**, via `ON DELETE RESTRICT` rather than `SET NULL` or `CASCADE`:
RESTRICT never writes to a row, so it never hits the "would null a NOT NULL
column" problem in the first place, and because a revision's parent and
child symbols are always deleted together in one cascade sweep (via the
`repository_revisions → symbols` CASCADE), RESTRICT never actually fires on
a legitimate deletion — only if something tried to delete one symbol while a
same-revision child still pointed at it, which is exactly what should be
rejected. This only works because it's a single-table self-reference; see
§3a for why the same trick doesn't generalize to the cross-table cases.

**One more instance of this**: `repositories.current_revision_id` is the
circular-reference case (see §6) and is a plain FK for this exact reason —
the alternative composite would pair it with `repositories.id`, which is the
primary key and can never be nulled.

## 3a. Revision-scoped reference enforcement

This section is the product of a dedicated audit pass (a design-audit
follow-up) across every FK involving a revision-scoped entity. The core
invariant being checked: **a revision-scoped derived record must not
accidentally reference a file/symbol/analysis object belonging to another
repository or revision, unless that relationship is explicitly designed to
be cross-revision.**

Three classifications are used:

- **A — DB-enforced same-revision**: a composite FK (§3's pattern) makes the
  wrong-revision case physically impossible to insert.
- **B — intentionally cross-revision**: there is no "wrong revision" here by
  design; enforcing same-revision would break a legitimate use case.
- **C — application-enforced**: the DB alone would accept a mismatched
  reference (composite FK ruled out — see §3's "where the pattern stops"),
  so an ORM-level `@validates` hook in `app/models/_validation.py` checks it
  instead, raising `ValueError` before the write ever reaches the database.

No relationship in this schema is left genuinely undefended: every
category-C relationship identified below now has a validator. The
distinction from category A is real, though — a validator only fires for
plain attribute assignment through the ORM session that added it; it does
**not** fire for bulk inserts, raw SQL, or a reference that was never routed
through `@validates` (e.g. set via `bulk_insert_mappings`). Treat it as a
strong development-time guardrail, not a guarantee with the same force as a
database constraint.

| Relationship | Class | Mechanism |
|---|---|---|
| `*.repository_id` + `*.revision_id` → `RepositoryRevision` (all 9 tables that carry both) | **A** | Composite FK, `CASCADE` |
| `Symbol.file_id` → `SourceFile` | **A** | Composite FK, `CASCADE` |
| `Symbol.parent_symbol_id` → `Symbol` | **A** | Composite FK, `RESTRICT` (added this pass — see §3) |
| `GraphEdge.source_symbol_id` / `target_symbol_id` → `Symbol` | **A** | Composite FK, `CASCADE` |
| `GraphEdge.evidence_file_id` → `SourceFile` | **A** | Composite FK, `CASCADE` |
| `CodeChunk.file_id` → `SourceFile` | **A** | Composite FK, `CASCADE` |
| `GeneratedTest.target_symbol_id` → `Symbol` | **A** | Composite FK, `CASCADE` (column is `NOT NULL`) |
| `TestExecution.repository_id` + `.revision_id` → `RepositoryRevision` | **A** | Composite FK, `CASCADE`, plus a CHECK that the pair is set together (added this pass — see §9) |
| `SessionContextRef.file_id` / `.symbol_id` / `.analysis_run_id` / `.finding_id` | **B** | None needed — the table has no revision_id of its own; a breadcrumb records what was true *at the time*, routinely from an older revision than the session's current pointer (see §10 and the model's docstring) |
| `Symbol.analysis_run_id` → `AnalysisRun` | **C** | `@validates`, compares `revision_id` |
| `GraphEdge.analysis_run_id` → `AnalysisRun` | **C** | `@validates`, compares `revision_id` |
| `CodeChunk.symbol_id` → `Symbol` | **C** | `@validates`, compares `revision_id` |
| `CodeChunk.analysis_run_id` → `AnalysisRun` | **C** | `@validates`, compares `revision_id` |
| `GeneratedArtifact.related_file_id` → `SourceFile` | **C** | `@validates`, compares `revision_id` |
| `GeneratedArtifact.related_symbol_id` → `Symbol` | **C** | `@validates`, compares `revision_id` |
| `GeneratedArtifact.analysis_run_id` → `AnalysisRun` | **C** | `@validates`, compares `revision_id` |
| `GeneratedTest.analysis_run_id` → `AnalysisRun` | **C** | `@validates`, compares `revision_id` |
| `TestExecution.file_id` → `SourceFile` | **C** | `@validates`, compares `revision_id` — stays plain FK even after the repo/revision upgrade above, because `TestExecution` and `SourceFile` are sibling cascades off `repository_revisions`; a composite `RESTRICT` between them could race against the sibling cascade during a bulk delete (see below) |
| `TestExecution.analysis_run_id` → `AnalysisRun` | **C** | `@validates`, compares `revision_id` |
| `SecurityFindingObservation.file_id` → `SourceFile` | **C** | `@validates`, compares `revision_id` |
| `SecurityFindingObservation.symbol_id` → `Symbol` | **C** | `@validates`, compares `revision_id` |
| `SecurityFindingObservation.analysis_run_id` → `AnalysisRun` | **C** | `@validates`, compares `revision_id` |
| `SecurityFinding.first_seen_run_id` / `.last_seen_run_id` → `AnalysisRun` | **C** | `@validates`, compares `repository_id` (this table has no `revision_id` at all — it's repo-scoped, see §7) |
| `Embedding.analysis_run_id` → `AnalysisRun` | **C** | `@validates`, a **two-hop** check (`Embedding` has no `revision_id` of its own — only its parent `CodeChunk` does), looks up both and compares their revisions |
| `Repository.current_revision_id` → `RepositoryRevision` | **C** (special) | `@validates`, compares `repository_id` — the strongest mechanism available for this one circular pointer (see §6); a DB composite here would try to null `id`, the primary key, which is impossible |
| `InvestigationSession.current_revision_id` / `.started_at_revision_id` → `RepositoryRevision` | **C** | `@validates`, compares `repository_id` |

### Why RESTRICT doesn't generalize beyond the self-referential case

§3's `Symbol.parent_symbol_id` fix works because the "referencing" and
"referenced" rows (parent and child symbol) are deleted in the *same*
single-table cascade sweep — there's only one path from
`repository_revisions` down to `symbols`, so there's no ordering ambiguity
for Postgres to get wrong. Every category-C relationship above where the
referenced row lives in a **different table** (e.g. `CodeChunk.symbol_id` →
`Symbol`) doesn't have that guarantee: `CodeChunk` and `Symbol` are **sibling
cascades**, both descending from `repository_revisions` via their own
independent composite FK. Postgres's default FK constraint mode checks
immediately per-row, not deferred to end-of-statement, so a `RESTRICT`
between two sibling cascade paths could fire a false-positive block if
Postgres happens to process the deletes in one order rather than the other —
a real, known class of bug, not a hypothetical one. Fixing it properly would
require marking those FKs `DEFERRABLE INITIALLY DEFERRED`, which changes
constraint-checking semantics transaction-wide and is exactly the kind of
"disproportionate complexity for theoretical perfection" this hardening pass
was told not to chase. The `@validates` backstop was judged the
better-targeted fix.

## 4. GraphEdge semantics

**Canonical invariant** (unchanged from the domain model, now enforced by a
CHECK constraint, not just documentation): `source_symbol_id` is the
dependent (caller/importer/subtype); `target_symbol_id`/`target_external_ref`
is the dependency (callee/imported/supertype). Impact analysis is backward
traversal: find edges where the target matches, walk to their source,
repeat.

**Exactly one target, enforced**: `ck_graph_edges_exactly_one_target` —
`(target_symbol_id IS NOT NULL AND target_external_ref IS NULL) OR
(target_symbol_id IS NULL AND target_external_ref IS NOT NULL)`.

**`relationship_type` allow-list**: `imports`, `calls`, `inherits`,
`implements`, `references`. **`contains` was deliberately removed** from the
domain model's original list — containment of a symbol in a file is already
`Symbol.file_id`; containment of a method in a class is already
`Symbol.parent_symbol_id`. A `contains` edge would just restate one of those
two facts in a second place. No generic graph-node abstraction was
introduced either — every edge endpoint is a `Symbol` row, full stop.

**`confidence` is nullable** (`ck_graph_edges_confidence_range` unchanged:
`confidence >= 0.0 AND confidence <= 1.0`, a plain `CHECK`, which already
tolerates `NULL` under standard SQL three-valued logic — no constraint
rewrite was needed for this half). Added by migration `0002` (originally
`NOT NULL` in `0001`). `NULL` means "not calibrated" — the v1 Python graph
builder writes `NULL` for every edge, deliberately, rather than a fabricated
numeric placeholder. See `domain-model.md`'s field table and
`graph-construction.md` section 7 for why a constant like `1.0` would itself
have been a fabricated score, not an absence of one.

**`resolution_method` allowed values** (`ck_graph_edges_resolution_method_allowed`,
a plain `CHECK` against a `Text` column — not a native Postgres `ENUM` type,
so extending the allow-list is a constraint swap, not a type migration):
`syntactic`, `semantic`, `ast_resolution` (added by migration `0002`). Only
`ast_resolution` is used by any analyzer today — see `domain-model.md`'s
field table for what each value means and why neither pre-existing value
accurately described the v1 Python resolver's actual technique.

**Migration `0002`** (`graph_edges: confidence nullable, resolution_method
gains ast_resolution`): the only schema change since `0001`. Touches
exactly two things on `graph_edges` — `confidence`'s nullability and
`resolution_method`'s CHECK allow-list — verified via offline SQL generation
to contain no other DDL. Both the model (`app/models/graph.py`) and the
combined `0001`+`0002` migration output were compared directly (compiled
`CREATE TABLE` DDL vs. `alembic upgrade base:head --sql`) and agree exactly.
Not run against a live PostgreSQL instance when authored — same caveat as
`0001`'s own header.

**Deduplication**: identical evidence (same revision, relationship type,
source, target, and evidence location) must produce one logical edge, not
one per analysis run that happens to re-detect it. This is **not** a single
multi-column `UNIQUE` constraint, for a specific, easy-to-miss reason:
Postgres's standard `UNIQUE` constraint treats `NULL` as "not equal to
anything, including another NULL" — so a constraint spanning both
`target_symbol_id` and `target_external_ref` would **never** catch
duplicates on the external-ref branch, because `target_symbol_id` is always
`NULL` there, and a `NULL` column makes the whole row "unique" by Postgres's
definition regardless of what the other columns contain. The fix is two
**partial unique indexes**, one per branch:

```sql
CREATE UNIQUE INDEX uq_graph_edges_dedup_internal ON graph_edges
  (revision_id, relationship_type, source_symbol_id, target_symbol_id,
   evidence_file_id, evidence_start_line)
  WHERE target_symbol_id IS NOT NULL;

CREATE UNIQUE INDEX uq_graph_edges_dedup_external ON graph_edges
  (revision_id, relationship_type, source_symbol_id, target_external_ref,
   evidence_file_id, evidence_start_line)
  WHERE target_symbol_id IS NULL;
```

Every column inside each partial index is `NOT NULL` within that partition
(enforced by the XOR CHECK above plus `evidence_file_id`/`evidence_start_line`
being `NOT NULL` columns), so the NULL-distinctness gotcha doesn't recur.
`evidence_start_line` was tightened to `NOT NULL` in the implementation
(the domain doc had it nullable) specifically so it could anchor this key —
an edge with literally no known line is not useful evidence anyway.

**Indexes for traversal** (not automatic — Postgres does not index the
referencing side of a foreign key by default):
`(target_symbol_id, revision_id)` for backward/impact traversal,
`(source_symbol_id, revision_id)` for forward/dependency traversal, kept as
two separate indexes because they serve two different, equally common query
shapes — one index with columns in either order cannot serve both
efficiently as the leading predicate changes.

## 5. Immutability expectations

`repository_revisions`, `source_files`, `symbols`, `graph_edges`,
`code_chunks`, `embeddings`, `generated_artifacts`, `generated_tests` rows
are never updated to point at a different revision — a new revision always
means new rows, never repointing old ones. The schema doesn't have a
technical mechanism forcing this (Postgres can't enforce "this row is never
UPDATEd" by itself); it's an application-layer discipline the schema is
*shaped* to support (every row's revision is fixed at INSERT time via the
composite FKs above) but can't fully guarantee by itself. This is worth
stating plainly rather than overclaiming DB-level enforcement that doesn't
exist.

The two exceptions, both already called out in the domain model and
unchanged here:

- **`security_findings`** mutates (`status`, `updated_at`) because a finding
  represents an ongoing condition, not a fact about one commit.
- **`investigation_sessions`** mutates (`title`, `current_revision_id`,
  `status`, `last_active_at`) because a session is an evolving conversation,
  not a snapshot.

## 6. Repository ↔ RepositoryRevision circular reference

`repositories.current_revision_id` references `repository_revisions.id`;
`repository_revisions.repository_id` references `repositories.id`. Handled
two ways:

1. **Schema**: the FK on `repositories.current_revision_id` is declared with
   SQLAlchemy's `use_alter=True`. Both the ORM metadata and the hand-written
   migration create `repositories` *without* that FK first, create
   `repository_revisions` next, and only then `ALTER TABLE repositories ADD
   CONSTRAINT ...` — confirmed in both the mock-engine DDL dump and the
   Alembic offline SQL output; both end with that exact `ALTER TABLE`
   statement as the very last operation.
2. **Application creation order**: insert `Repository` with
   `current_revision_id = NULL` → insert its `RepositoryRevision` row(s) →
   `UPDATE repositories SET current_revision_id = ...`. This ordering never
   violates either FK at any point, so no `DEFERRABLE` constraint trickery
   is needed.

Deletion: `repository_revisions.repository_id` is `ON DELETE CASCADE`
(deleting a repository deletes its revisions, and everything cascades from
there); `repositories.current_revision_id` is `ON DELETE SET NULL` (deleting
one specific revision row, independent of deleting the whole repository,
just clears the pointer rather than failing). As discussed in §3, this
pointer could not be composite-FK'd against `repository_id` for self-matching
without trying to null the repository's own primary key — accepted as an
application-level invariant instead.

## 7. SecurityFinding identity vs. SecurityFindingObservation

Unchanged from the domain model, restated because it's the one deliberate
exception to "revision-scoped and immutable" everywhere else: **`SecurityFinding`
is the deduplicated, mutable identity** (`unique(repository_id, fingerprint)`,
`status` can move from `open` to `resolved`/`ignored`/`false_positive`).
**`SecurityFindingObservation` is the immutable per-scan record** (one row
every time a scan re-detects that fingerprint, carrying its own
`revision_id`, `analysis_run_id`, and point-in-time evidence). Findings don't
get one row per scan — they get dedup for the UI and full, reproducible
history for free, from two tables instead of trying to force one.

## 8. Embedding versioning and the pgvector dimension limitation

`embeddings` is a separate table from `code_chunks`, unique on `(chunk_id,
provider, model_name, model_version)`, so re-embedding a chunk with a newer
model is an additional row, not a destructive overwrite, and a RAG query
stays model-consistent by filtering on those columns.

**Explicit embedding identity, not one opaque string**: `provider`,
`model_name`, `model_version`, and `dimension` are separate, real columns —
not a single `model` string to parse. `model_version` is `NOT NULL` with an
empty-string convention for providers that don't expose one, specifically
because a plain `UNIQUE` constraint treats `NULL` as distinct from `NULL`
(the same gotcha as GraphEdge's dedup index, §4) — a nullable
`model_version` would let duplicate `(chunk, provider, model_name)` rows
through unnoticed.

**`EMBEDDING_DIMENSION = 1536` is a PROVISIONAL SCHEMA CONSTANT, not a
product decision.** No embedding model has been selected for DevLens yet —
this exists only to make the initial schema executable. It **must** be
revisited before Phase 7 writes any real embeddings.

**pgvector limitation, stated plainly**: a pgvector column has a *fixed*
dimension declared at table-creation time (`vector(1536)` here), because an
ANN index (ivfflat/hnsw) requires one. This schema supports **exactly one
embedding dimension for v1**. The `dimension` column records, per row, what
dimension that row's vector actually is — but **this is metadata, not a
mechanism**: it does not and cannot make the `vector` column itself
dynamically sized. If a future embedding model's actual output width differs
from `EMBEDDING_DIMENSION`, pgvector itself rejects the insert outright;
nothing about the `dimension` column changes that. If/when a model with a
different dimension is selected, the real options are: a schema migration to
widen/change the column (breaking any ANN index built on it), a second
embeddings table/column for the new dimension class, or projecting the new
model's vectors down to this width before storage. **None of this is
implemented, and this pass deliberately did not build multiple vector
columns or a polymorphic embedding-storage system to pre-empt it** — that
would be solving a problem before the actual model choice exists to inform
the right design.

## 9. Test execution identity

`generated_tests` is unchanged from the domain model. `test_executions` can
represent either a generated test's execution (`generated_test_id` set) or
an *existing* repository test's execution (`generated_test_id` NULL,
`file_id` + `test_identifier` set instead — e.g. a pytest node id). A CHECK
constraint (`ck_test_executions_exactly_one_test_identity`) enforces exactly
one identity mode per row.

**Tightened during the hardening pass**: `repository_id`/`revision_id` are
now a composite FK to `repository_revisions` (`ON DELETE CASCADE`), same
pattern as everywhere else in §3 — this is safe because both columns are
`NOT NULL`-or-`NULL`-*together* for the existing-test branch, enforced by a
second CHECK constraint (`ck_test_executions_repository_and_revision_set_together`,
`(repository_id IS NULL) = (revision_id IS NULL)`). That CHECK exists
because Postgres's composite-FK `MATCH SIMPLE` semantics treat the FK as
satisfied whenever *either* column is `NULL` — without the CHECK, a row
could set `repository_id` alone and the composite FK would silently never
even check it. `file_id` stays a plain FK with an `@validates` backstop (see
§3a) rather than joining the composite, since `TestExecution` and
`SourceFile` are sibling cascades off `repository_revisions` — this remains
the loosest-coupled part of the schema, deliberately: it's execution-log-shaped
data, not core structural graph data, and the instruction was explicit not
to build a CI/test-management subsystem around it.

## 10. SessionContextRef validation

`ref_type` is one of `selected_file`, `selected_symbol`, `graph_focus`,
`analysis_reference`, `finding_reference`. A single CHECK constraint
(`ck_session_context_refs_ref_type_matches_populated_column`) enumerates all
five as mutually exclusive, one-hot cases: the column matching `ref_type`
must be non-null, and every *other* reference column (`file_id`, `symbol_id`,
`analysis_run_id`, `finding_id`) must be null. `graph_focus` requires
`graph_camera_state` (JSONB) to be non-null instead of one of the FK columns.
This rejects both "nothing populated" and "two references populated at
once" — a row claiming `ref_type = 'selected_file'` with `symbol_id` also
set is just as invalid as one with neither set, and both are rejected by the
same constraint. Exercised in `tests/db/test_session_context.py`.

## 11. AnalysisRun: analyzer identity convention

To avoid `config` (JSONB) becoming "a dumping ground for fields that need
querying, identity, or lifecycle semantics" (the explicit design-audit
concern): **`analyzer_name` and `analyzer_version` are real, indexed text
columns**, not keys inside `config`. "Which runs used analyzer X version Y"
is a query this system will actually make (to decide what needs re-running
after an analyzer upgrade); burying that in JSONB would make it
unindexable and easy to get subtly inconsistent across rows. `config` stays
reserved for analyzer-specific tuning parameters that nothing joins or
filters on. `status`/`started_at`/`finished_at`/`error_message`/`error_detail`/
`triggered_by` are likewise all first-class typed columns — the only thing
`config`/`error_detail` hold is genuinely unstructured, per-analyzer data.

## 12. Delete/cascade policy — the general rule

Stated once here because it's applied mechanically everywhere else in this
document rather than re-justified table by table:

- **`ON DELETE CASCADE`** is used wherever the child row has no independent
  meaning without its parent (a revision's files/symbols/edges; a session's
  messages; an edge's required source symbol).
- **`ON DELETE SET NULL`** is used wherever the reference is informational/
  provenance ("which analysis run produced this," "which symbol is this
  chunk near") and the child row should survive the parent's deletion with
  that one fact cleared.
- **`ON DELETE RESTRICT`** is used for every reference into `source_blobs`
  (content is shared/content-addressed; nothing should be able to delete a
  blob that's still referenced by any file, generated test, or artifact).
- **Composite FK + `SET NULL` is never used** when one of the composited
  columns is `NOT NULL` — see §3's explanation. In every such case the
  schema falls back to a plain single-column FK with `SET NULL`, and the
  cross-column consistency for that one pointer is an accepted,
  documented, application-level invariant instead of a DB-enforced one.

## 13. Indexes and why they exist

Beyond what's already covered in §4 and §11, the recurring shape is: every
revision-scoped table is indexed on `(revision_id, ...)` or
`(repository_id, ...)` for its dominant query ("everything in this
revision," "everything in this repo"), because that's the access pattern
every feature from impact analysis to the Files tab actually uses. The full,
per-table list with rationale is in the model docstrings in
`apps/api/app/models/`; repeating it here would just be restating the code
in prose. The ones worth calling out specifically because they're easy to
get wrong or skip:

- `uq_repository_revisions_repository_id_commit_sha` — the single most
  important constraint in the schema; prevents duplicate ingestion of a
  commit.
- `uq_security_findings_repository_id_fingerprint` — the mechanism that
  makes cross-scan dedup possible at all.
- `ix_graph_edges_target_symbol_id_revision_id` /
  `ix_graph_edges_source_symbol_id_revision_id` — see §4; two indexes, not
  one, because the query shapes genuinely differ.

## 14. CHECK constraint naming — a real bug caught by static validation

Worth recording because it's a non-obvious SQLAlchemy/Alembic behavior and
explains a line of defense in this process actually paying off: the first
draft of the hand-written migration passed fully-qualified names directly to
`CheckConstraint(..., name="ck_analysis_runs_status_allowed")`. Alembic's
migration context picks up `target_metadata.naming_convention` (configured
on `Base.metadata` in `app/db/base.py`) and — specifically for `CheckConstraint`,
unlike `ForeignKeyConstraint`/`UniqueConstraint` — re-applies that convention
*around* whatever `name=` is given, rather than treating it as final. The
result was constraint names like `ck_analysis_runs_ck_analysis_runs_status_allowed`
— doubled, not broken, so it would have silently shipped an ugly-but-working
schema. Caught by diffing the Alembic offline SQL output against the
mock-engine DDL dump (§ "Status of validation") and fixed by passing the bare
suffix (`name="status_allowed"`) everywhere, matching the convention used in
the ORM models themselves.

## 15. Remaining unresolved questions

Per design-audit item 13, these are explicitly **not** solved here, only
where this implementation touches them is noted:

- Inline-vs-object-storage threshold for `SourceBlob` — the CHECK constraint
  makes the two modes mutually exclusive and explicit, but *which* threshold
  picks one is left as an application/config decision, as instructed.
- Cross-revision symbol identity, partitioning/scale ceiling, multi-parent
  Git history, analyzer migration policy, embedding retention policy — all
  untouched; nothing in this schema forecloses any particular future answer
  to them, but none is implemented.
- The pgvector single-dimension limitation (§8) is new information this
  implementation surfaced, not present in the original domain document.
