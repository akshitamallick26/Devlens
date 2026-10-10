# RAG / Codebase Q&A v1

> Retrieval finds evidence. The LLM explains retrieved evidence.

This document is written before implementation. It covers v1 only:
deterministic, graph-aware retrieval over the already-persisted repository
graph plus a bounded, citation-validated LLM synthesis layer on top —
mirroring the exact deterministic/AI separation already proven in
`impact-analysis.md` and `impact-intelligence.md`. No chat UI, no
autonomous repository modification, no external vector database.

## 0. Task 1 audit result

Read `app/models/{rag,symbol,source,graph,analysis,repository,investigation}.py`,
`app/{ingestion,parsing,graph,impact}/*.py`, `app/core/config.py`,
`alembic/versions/*`, and `docs/architecture/{domain-model,database-design,
graph-construction,impact-analysis,impact-intelligence}.md` directly —
not assumed from the docs.

**`CodeChunk`/`Embedding` already exist** (from the database-implementation
phase, unused until now):

- `CodeChunk`: `repository_id`, `revision_id` (composite FK to
  `repository_revisions`), `file_id` (composite FK to `source_files`),
  `symbol_id` (nullable, same-revision validated), `chunk_kind` (`symbol_body
  | doc_comment | sliding_window | file_summary`), `start_line`/`end_line`,
  `cached_content_hash` (nullable FK to `source_blobs`), `analysis_run_id`.
- `Embedding`: `chunk_id`, `provider`/`model_name`/`model_version`/
  `dimension` (explicit columns, not one opaque string),
  `vector: Vector(EMBEDDING_DIMENSION)`, unique on
  `(chunk_id, provider, model_name, model_version)` — multiple models can
  coexist per chunk without ambiguity, and changing models can never
  silently overwrite a different model's vector. `EMBEDDING_DIMENSION =
  1536` (a provisional placeholder per its own docstring, never revisited
  until a real model was chosen — see section 7 for why `1536` turns out
  to already be the right number for the model this phase picks).

**Discrepancy found and resolved without a schema change**: Task 3 lists
`text` as a required `CodeChunk` provenance field. The actual persisted
model deliberately does **not** store chunk text — its own docstring says
so explicitly: "a chunk's authoritative text is `(file_id, start_line,
end_line)` sliced from that file's content blob — not an independently
stored copy." This is not an oversight; storing a second copy of source
text would create a staleness/duplication risk the rest of this schema
(`SourceBlob` as the one content-addressed source of truth) was built to
avoid. It satisfies Task 3's *functional* need (retrieval must be able to
show chunk text) via a read-time derivation function
(`app/rag/persistence.py::load_chunk_text`), exactly the same pattern
`app/impact/source.py` already established for symbol source extraction.
**No schema change needed for this.**

**Schema gap found and resolved with one additive migration**: no vector
index existed on `embeddings.vector` — `domain-model.md` section 14
explicitly anticipated this ("a real tuning concern to resolve at
implementation time, not here"). Migration `0003` adds an HNSW index
(`vector_cosine_ops`) — see section 6 for why HNSW and the revision-
isolation interaction this creates.

**Everything else needed already exists and is unchanged**: `GraphEdge`
(unmodified — graph expansion in section 9 only reads it), the ingestion/
parsing/graph-construction/impact-analysis pipelines (reused as-is for
symbol/source data), `app/ai/provider.py`'s `AIProvider` abstraction
(reused for QA synthesis), `app/core/config.py`'s `Settings` convention
(extended, not replaced), `InvestigationSession`/`SessionMessage` (read
for the Task 18 seam discussion in section 13, not modified).

## 1. The architecture boundary (unchanged invariant, now with retrieval added)

```text
Repository source (SourceBlob, immutable)
        ↓
Parsed symbols (Symbol, from app/parsing/ — unmodified)
        ↓
Graph relationships (GraphEdge, from app/graph/ — unmodified)
        ↓
Code chunks (CodeChunk — section 2)
        ↓
Embeddings (Embedding) + lexical representation (ad-hoc, section 5)
        ↓
Hybrid retrieval (section 7)
        ↓
Graph expansion (section 9, reusing GraphEdge — never rebuilding it)
        ↓
Bounded evidence/context package (section 10)
        ↓
LLM (app/ai/provider.py — unmodified abstraction)
        ↓
Grounded answer + validated citations (section 11)
```

**The LLM never determines whether a symbol or relationship exists.**
Retrieval and graph expansion are 100% deterministic, driven entirely by
already-persisted `Symbol`/`GraphEdge`/`CodeChunk`/`Embedding` rows and
plain SQL (lexical ranking, vector distance, graph traversal) — the LLM
receives a closed, pre-built evidence set and is structurally incapable of
adding to it (its structured response has no field that could introduce a
new symbol, file, or relationship — see section 11, the same enforcement
mechanism `impact-intelligence.md` section 1 already established).

## 2. Chunking model

Primary unit: **semantic code**, not fixed-size windows.

```text
chunk_kind = "symbol_body"      # one Symbol's full source span — the default, preferred unit
chunk_kind = "sliding_window"   # a deterministic sub-span of an oversized symbol
chunk_kind = "file_summary"     # module-level code with no covering Symbol
```

**Every `Symbol` in a revision becomes exactly one `symbol_body` chunk**
(module symbols included — a module's chunk spans its own file, giving
file-level context even when no specific function is the right match).
`chunk_kind = "doc_comment"` from the pre-existing `CHUNK_KINDS` enum is
**not produced by v1** — Python docstrings are already part of a symbol's
own body span (the parser's `start_line`/`end_line` include the
docstring), so a separate doc-comment chunk would just duplicate text
already covered by `symbol_body`. Reserved for a future phase/language
where comments aren't part of a symbol's own span.

**Module-level code with no covering Symbol** (e.g. top-level statements
outside any function/class, or a file the parser skipped) is represented
as one `file_summary` chunk per `SourceFile`, spanning the *whole file* —
this is the only chunk kind allowed to exceed normal size bounds, since it
exists specifically to not lose file-level content that symbol-level
chunking has no symbol to attach to. `symbol_id` is `NULL` for these.

**Oversized symbols** (a single function/class body beyond
`MAX_CHUNK_LINES`): deterministic sub-chunking by line count, into
multiple `sliding_window` chunks that together cover the symbol's exact
span with **no gap and no overlap** — never silently truncated. Each
sub-chunk's provenance still points at the *same* `symbol_id` (so "this
chunk belongs to `calculate_total`" is always answerable) and carries an
explicit `chunk_index`/`chunk_count` pair (encoded in `CodeChunk`'s
`start_line`/`end_line` plus ordering by `start_line` — no new column
needed, since start_line ordering within a symbol_id is already a total,
deterministic order).

**Determinism**: chunk generation is a pure function of
`(symbol table, source file contents)` for one revision — same revision,
same chunks, always, including chunk ordering (section 3) and content
hashing. No randomness, no wall-clock dependency, no filesystem iteration
order dependency (explicit sort by `(file path, start_line)` throughout).

## 3. Chunk persistence (no schema change — section 0)

`app/rag/service.py::run_chunk_generation` mirrors
`app/graph/service.py::run_graph_construction`'s exact lifecycle:
`AnalysisRun(analysis_type=...)` created and committed early, delete-and-
rebuild for idempotency (`DELETE FROM code_chunks WHERE revision_id = X`
before inserting the fresh set — identical strategy to
`app/graph/service.py`'s `GraphEdge` rebuild, for the identical reason:
this phase always reconstructs the full chunk set for a revision, so a
blanket revision-scoped delete is simpler and more obviously correct than
an upsert). `ON DELETE CASCADE` on `code_chunks.repository_id/revision_id`
already guarantees a revision's chunks are destroyed with it; the explicit
delete-and-rebuild is about *idempotent regeneration*, not cleanup.

`AnalysisRun(analysis_type = "rag_indexing")` — already a reserved value
in `ANALYSIS_TYPES`, unused until now (the exact same pre-anticipation
pattern `impact-analysis.md` found for `"impact_analysis"`).

## 4. Embedding architecture

**Provider: OpenAI, model `text-embedding-3-small` (1536 dimensions).**
Chosen for one concrete, practical reason: it is the only realistic
embedding option whose *default* output width already matches this
schema's existing, pre-committed `EMBEDDING_DIMENSION = 1536` — choosing
anything else (e.g. Voyage AI's default 1024-dim output) would force a
real schema migration (dropping and rebuilding the vector column and any
index on it) for a dimension choice that was already provisional and
arbitrary. No other reason drove this choice; it is explicitly a "use
what the existing schema already committed to, since changing it is not
free" decision, documented rather than silently made. A future phase
re-evaluating embedding quality can migrate deliberately, with the
dimension-change cost made explicit by `database-design.md` section 8.

**Deterministic input text**: the exact same text `app/rag/persistence.py`
derives for a chunk (the sliced source span) — never a paraphrase, never
including volatile metadata (timestamps, UUIDs) that would make the same
code produce a different embedding input run to run.

**Batching**: bounded batches (`EMBEDDING_BATCH_SIZE`, a fixed v1 constant)
per HTTP request — never one chunk per request (same "not one round-trip
per unit of work" principle `impact-analysis.md` section 9 already
established for graph traversal) and never one unbounded request for an
entire revision's chunks.

**Failure/rate-limit/retry/timeout**: identical shape to
`app/ai/provider.py`'s `AnthropicProvider` — typed exceptions
(`AIProviderUnavailableError`/`AIResponseValidationError`, reused from
`app/ai/models.py`, not a second incompatible hierarchy), explicit bounded
`httpx.Timeout`, zero retries (same v1 rationale as Impact Intelligence:
simplest correct behavior for an already-optional, already-gracefully-
degrading code path). A failed batch fails that batch's chunks only — it
does not corrupt or partially-commit a chunk's embedding row.

**Provider-optional operation**: ingestion, parsing, graph construction,
and chunk generation **never** depend on the embedding provider. Embedding
generation is its own derived-analysis step
(`AnalysisRun(analysis_type = "rag_indexing")`, a second run distinct from
chunk generation's — embeddings can be (re)computed independently of
chunking). If no API key is configured, embedding generation fails
explicitly (an `AIConfigurationError`-shaped shape, same convention as
`get_provider()`) — chunks persist regardless, and lexical-only retrieval
remains fully functional.

**Persistence semantics**: one `Embedding` row per
`(chunk_id, provider, model_name, model_version)`, enforced by the
existing unique constraint. Re-running with the same model is idempotent
via upsert-by-that-key (not delete-and-rebuild — unlike chunks, embeddings
are expensive to regenerate and the unique key already prevents
duplication safely). Switching models adds new rows under a new
`model_name`/`model_version` key; it never overwrites a different model's
vector, by construction.

## 5. Lexical retrieval (no new column, no new index)

`app/rag/lexical.py`, DB-touching, revision-scoped. Two independent
signals, combined by rank (not raw score; see section 7).

**Query normalization (`app/rag/query.py`, RAG Q&A v1.1).** The question
is never passed to Postgres as-is. It is truncated to 2,000 characters and
tokenized into Unicode word tokens (`\w+`), lowercased and de-duplicated in
first-seen order, at most 32 full-text terms. Identifier terms are the
dotted-name-aware tokens of at least 3 characters that are not question
filler words ("what", "does", "how"...), at most 8. Tokenization removes
every character that tsquery or LIKE syntax treats as an operator, and every
term is a bound parameter.

**v1.1 fix: any useful term matches.** v1 used `plainto_tsquery(question)`,
which ANDs every non-stop-word lexeme: "How is authentication retried?"
matched only files containing both "authentication" and "retried", so
natural questions often retrieved nothing when no embedding provider was
configured. Now:

1. **Full-text search over whole-file content**:
   `to_tsvector('english', source_blob.inline_content) @@
   (plainto_tsquery('english', :t1) || plainto_tsquery('english', :t2) ||
   ...)`, one `plainto_tsquery` per term combined with OR. A stop-word term
   produces an empty tsquery, which the OR ignores, so a stop-word-only
   question matches nothing. Ranked by `ts_rank_cd`, which over an OR query
   rewards files covering more of the question's terms. Represented
   by that file's `file_summary` chunk. Deliberately **file-grained, not
   chunk-grained**: `CodeChunk` doesn't store its own text (section 0), and
   computing `to_tsvector` per chunk by re-deriving each chunk's sliced
   text would mean one query per chunk — exactly the N+1 pattern
   `app/rag/embeddings.py`'s own hardening removed. Searching the whole
   file's text once and pointing at its `file_summary` chunk is the only
   way to make docstrings/comments/natural-language mentions searchable
   without that cost. No persisted/indexed `tsvector` column either:
   corpus size for one revision's files is bounded, and scoping by
   revision first keeps an ad hoc `to_tsvector` acceptable at v1 scale (a
   future phase can add a generated/indexed column if real corpus sizes
   make this a genuine bottleneck — explicitly deferred, not assumed
   away). **Known imprecision**: for a file large enough to need more than
   one `file_summary` piece (section 2), ranking is still over the whole
   blob, and the match is always attributed to the first piece — there is
   no principled way to attribute a `ts_rank_cd` score to one piece over
   another without per-piece indexing.
2. **Exact/substring identifier matching** against `Symbol.qualified_name`
   (joined via `CodeChunk.symbol_id`): catches "what calls
   `apply_discount`"-style literal lookups that English-stemming full-text
   search handles poorly for snake_case/camelCase identifiers, and gives
   the precise, symbol-level chunk (not just "somewhere in this file") that
   signal 1 can't. v1.1: matches any identifier term
   (`qualified_name ILIKE '%term%'` with `%`, `_` and `\` in the term
   escaped), ranked by how many terms match, then by shorter name. v1
   matched the *whole question* as one substring, so a natural-language
   question never matched a symbol, and an unescaped `_` or `%` in it acted
   as a wildcard.

Both are revision-scoped (`WHERE code_chunks.revision_id = X`) and bounded
(`LIMIT` applied before returning candidates) — never a full unscoped
table scan returned to the caller, never cross-revision, never
cross-repository (enforced by the same `WHERE` clause, revision IDs being
globally unique UUIDs makes repository-scoping implicit through the
revision filter, but the query also filters `repository_id` explicitly as
defense in depth, matching `app/impact/service.py`'s own convention).

## 6. Vector retrieval

`app/rag/vector.py`, DB-touching, revision-scoped, bounded top-k.

**The pgvector + revision-filter interaction** (flagged but not resolved
by `domain-model.md` section 14): `embeddings` has no `revision_id` column
of its own — only its parent `CodeChunk` does — so the HNSW index
(migration `0003`) is necessarily global across every revision ever
embedded, not revision-scoped. A naive `ORDER BY vector <=> query LIMIT k`
query joined to a `WHERE code_chunks.revision_id = X` filter risks the
index returning its k nearest neighbors *before* that filter is applied,
potentially yielding fewer than k results — or zero — after filtering, if
the true nearest neighbors for this revision aren't among the global top-k.

**Mitigation, v1**: over-fetch a bounded multiple of the requested
`top_k` (`VECTOR_OVERFETCH_MULTIPLIER`, a fixed constant) from the index,
*then* filter by revision and truncate to `top_k`. This is the standard,
documented mitigation for this exact pgvector limitation — deterministic,
bounded (the overfetch multiplier is fixed, not unbounded), and correct
for realistic corpus sizes. It is not a perfect guarantee for a
pathological case (a revision with vanishingly few chunks relative to the
rest of the indexed corpus) — documented as a known v1 limitation
(section 17), not silently assumed away.

**Missing embeddings**: a chunk with no `Embedding` row (not yet embedded,
or embedding generation failed/was never run) is simply absent from
vector-retrieval results — never fabricated, never treated as a
zero-vector or default match. **Provider-unavailable** state (no API key
configured) means vector retrieval over *new* queries still needs to embed
the *query text* itself — if that fails, vector retrieval returns no
results (not fabricated ones) and hybrid retrieval (section 7) degrades to
lexical-only, explicitly flagged in the result (section 10).

## 7. Hybrid retrieval

**Reciprocal Rank Fusion (RRF)**, not raw score averaging — lexical
`ts_rank_cd` and vector cosine distance are on incomparable scales, so
combining them by score would be exactly the "blindly averaging arbitrary
scores" this phase's own constraints prohibit. RRF instead combines by
**rank**:

```text
score(chunk) = sum over retrievers r in {lexical, vector} of
               1 / (k + rank_r(chunk))
```

`k = 60` — the standard constant from the original RRF paper (Cormack et
al.), chosen because it's a well-known, externally-justified default
rather than a tuned-for-this-dataset magic number. A chunk retrieved by
only one signal still gets a score (using only that signal's term); a
chunk retrieved by both gets the sum of both terms, naturally rewarding
agreement without any special-casing. Final ordering: RRF score
descending, then a stable deterministic tie-break
(`chunk_id` ascending) — ties are expected and must never depend on
dict/set iteration order.

## 8. Graph-aware retrieval (expansion, not rebuilding)

`app/rag/graph_expansion.py` — a **new, separate, bounded** reader of the
existing `GraphEdge` table. Deliberately **not** a reuse of
`app/impact/traversal.py::compute_impact`, whose shape (single target,
full BFS impact set, depth-labeled) doesn't fit this task's actual need
(multiple retrieved seed symbols, shallow bounded expansion, both
directions at once). Reusing it would mean contorting a different
algorithm's output shape rather than writing the small, purpose-fit one
this needs — and the hard constraint is "do not modify deterministic
impact traversal," not "every graph read must go through it."

For each retrieved symbol, optionally include (respecting the unmodified
canonical direction, `source = dependent, target = dependency`):

```text
callers / importers / dependents  = reverse traversal (target_symbol_id == seed)
callees / imports / dependencies  = forward traversal (source_symbol_id == seed)
subclasses                        = reverse "inherits" traversal
base classes                      = forward "inherits" traversal
```

**Bounded**: `GRAPH_EXPANSION_MAX_DEPTH` (default 1 hop — "show me what
directly touches this," not a deep impact analysis) and
`GRAPH_EXPANSION_MAX_SYMBOLS` (a hard cap on how many expanded symbols one
query can add, applied deterministically — shortest-reach-first, same
sort key family as `impact-analysis.md`'s ordering). **Cycle-safe**: a
visited set seeded with the original retrieved-seed symbol IDs, so
expansion can never loop back and re-add a seed as if it were newly
discovered — the same design principle as `traversal.py`'s own
cycle-safety, freshly implemented here since this function's shape
(multiple seeds, both directions, shallow) genuinely differs. **Revision-
scoped**: identical `WHERE revision_id = X` discipline as every other
query in this phase.

Every expanded symbol's evidence entry records **why** it was included —
`relationship_type` and `direction` (`"callers"`, `"callees"`, etc.) —
never expansion without an attributable reason.

## 9. Evidence model and context budget

```text
Evidence
    evidence_id          # "Q1", "Q2", ... — a distinct prefix from Impact
                          # Intelligence's "E1", "E2" (app/impact/evidence.py),
                          # deliberately: a mixed future context should never
                          # have to guess which subsystem an id came from.
    repository_id
    revision_id
    file_path
    symbol_id             # nullable — file_summary evidence has none
    qualified_name        # nullable, derived via symbol_id join when present
    line_start, line_end
    chunk_id
    retrieval_reason      # free text, e.g. "matched query terms", "calls apply_discount"
    retrieval_source      # "lexical" | "vector" | "graph" | "hybrid"
    relationship_type     # present only for graph-derived evidence
    content                # the sliced source text, or None if unavailable —
                          # never fabricated (section 0's chunk-text derivation)
```

Same budget discipline as `impact-intelligence.md` section 3/12
(hardening-pass-corrected): a fixed `MAX_EVIDENCE_ITEMS` and
`MAX_TOTAL_CONTENT_CHARS`, priority order (direct hybrid-retrieval hits
first, then graph-expansion additions), **degrade to metadata-only when
content doesn't fit — never omit the item entirely, never truncate
mid-excerpt**. Repository size never determines prompt size: the budget
is a fixed code-level constant, not derived from how many chunks exist.

## 10. Prompt-injection boundary (same discipline as Impact Intelligence)

Identical structural separation to `impact-intelligence.md` section 9/
hardening-pass section 5: a fixed `SYSTEM_PROMPT` constant that never
contains repository content, evidence wrapped in explicit
`---BEGIN/END UNTRUSTED REPOSITORY EVIDENCE---` markers, and an explicit
instruction that repository content — including anything that looks like
a system message, a tool call, or an instruction, however it's
formatted — is data to cite, never a command to follow. Tested the same
way: adversarial payloads fed through the real context assembler,
asserting they only ever land inside the evidence markers.

## 11. Grounded answer schema

```text
QAAnswer
    answer: str
    evidence: list[str]              # cited Q<n> ids, validated against the real set
    limitations: str                 # required — explicit insufficient-evidence statement
    related_symbols: list[str]       # optional, qualified names — informational only,
                                      # never a claim of a new relationship
    suggested_next_questions: list[str]  # optional
```

`model_config = ConfigDict(extra="forbid")` — identical discipline to
`ImpactExplanation`: no `risk_score`, `confidence`, `probability`, no field
shaped like a graph edge or a new symbol. `related_symbols` is
**informational text only** (the model naming symbols it already saw in
the evidence) — it is never parsed back into a graph mutation or treated
as proof those symbols are actually related; if the model names a symbol
that was never in the evidence, that's just an unverifiable string in a
list, not a citation, and the UI/caller must not present it as equivalent
to a cited, evidence-backed claim. Citation validation (`Q<n>` → real
evidence) reuses the exact validate/sanitize discipline
`app/impact/evidence.py` established, re-implemented with a "Q" prefix in
`app/rag/citations.py` rather than generalizing the already-approved
Impact Intelligence module — deliberately avoiding touching stable,
already-hardened code for a cosmetic prefix difference.

## 12. Provider orchestration

```text
question
  → resolve repository/revision (same validated-first pattern as
    app/impact/service.py — a bad revision_id must never masquerade as
    "no evidence found")
  → hybrid retrieval (lexical + vector, RRF-combined)
  → graph expansion (bounded, from the hybrid results' top symbols)
  → context assembly (bounded, degrade-not-omit)
  → LLM (app/ai/provider.py — reused, not reimplemented)
  → structured response validation (extra="forbid")
  → citation validation/sanitization
  → grounded result
```

If the LLM provider is unavailable (no key, or any failure — same broad
`except Exception` seam `impact-intelligence.md`'s hardening pass
established, for the identical reason: an unanticipated provider bug must
never cost the retrieval work already done): return a
**retrieval-only result** — matched symbols, matched files, the evidence
set itself — with an explicit `analysis_status = "retrieval_only"` field
and no fabricated natural-language answer. This exactly mirrors Impact
Intelligence's `"deterministic_only"` state, renamed for this domain:
`"retrieval_only" | "answered"`.

## 13. API and the session-history seam

`POST /repositories/{repository_id}/qa` — same thin-route convention as
`app/api/routes/impact.py`: accepts `revision_id`, `question`, and
bounded optional retrieval parameters (e.g. `top_k`, capped server-side
regardless of what's requested); never accepts raw repository text as a
retrieval shortcut. Returns the typed, serialized result — no ORM object.

**`app/rag/qa.py::run_qa(...)` is a plain function**, independent of
FastAPI — callable directly with `(db, repository_id, revision_id,
question)`, returning the same structured result the route serializes.
This is the seam for future `InvestigationSession`/`SessionMessage`
integration (`MESSAGE_ROLES = ("user", "assistant", "system")` already
exists): a future phase can call `run_qa` from a session-message handler
and persist its `answer`/`evidence` into a new `SessionMessage` row
without this phase's orchestration code knowing anything about sessions.
**No session schema change is made in this phase** — the seam is the
function boundary, not a new column.

## 14. Why RAG's scope stops here

No embeddings beyond the one chosen provider/model. No external vector
database (Pinecone/Qdrant/Weaviate/Elasticsearch) — pgvector is already
provisioned and sufficient for v1 scale. No repository-wide chat session,
no autonomous modification, no arbitrary agent/tool loop — `run_qa` is one
deterministic pipeline per question, not a multi-step agent making its own
tool-call decisions.

## 15. Known v1 limitations (stated plainly, not discovered later)

- The vector-index revision-filter interaction (section 6) is mitigated by
  over-fetching, not perfectly solved — a revision with very few chunks
  relative to the global corpus could, in principle, miss a true nearest
  neighbor. Acceptable for v1; a per-revision partition or a different
  indexing strategy would be the real fix if this becomes material.
- Lexical search computes `to_tsvector` ad hoc, unindexed — fine at v1
  corpus sizes, a genuine bottleneck candidate at much larger scale.
- Graph expansion is 1-hop by default — deeper expansion is possible by
  raising `GRAPH_EXPANSION_MAX_DEPTH`, not by a different algorithm, but
  v1 ships the conservative default.
- `storage_key`-backed (externally stored) source content has no
  retrieval path, identical to every prior phase's documented limitation —
  chunk text and embedding input are both unavailable for such files,
  never fabricated.
