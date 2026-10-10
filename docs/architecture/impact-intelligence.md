# Impact Intelligence v1

> Deterministic analysis establishes facts. The LLM explains those facts.

This document is written before implementation. It covers v1 only: an
optional natural-language explanation layer over an already-computed,
already-correct `ImpactAnalysisResult` (`docs/architecture/impact-analysis.md`).
No RAG, no embeddings, no risk scoring, no UI.

## 0. Task 1 audit result

Re-read `app/impact/{models,traversal,service}.py`,
`app/api/routes/impact.py`, `impact-analysis.md`, and the source-storage
models before writing any AI-layer code:

- **The deterministic engine is unmodified and stays that way.** Nothing in
  this phase touches `app/impact/traversal.py`'s algorithm or
  `app/models/graph.py`'s `GraphEdge` schema.
- **What deterministic evidence already exists**: `ImpactAnalysisResult`
  (target symbol, direct/transitive impacted symbols with
  qualified_name/kind/file_path/depth/relationship_types, impacted files).
  `GraphEdge` additionally carries real evidence locations
  (`evidence_file_id`, `evidence_start_line`, `evidence_end_line`) that the
  traversal result itself doesn't surface — this phase re-queries those,
  scoped and bounded (section 3), rather than threading them through
  `compute_impact`.
- **Source storage**: `SourceBlob.inline_content` (text, used by every
  analyzer so far) vs. `SourceBlob.storage_key` (external storage) — exactly
  one populated, enforced by a CHECK constraint.
  **`storage_key`-backed content has no retrieval path anywhere in this
  codebase yet** — `app/parsing/service.py` already established the
  precedent of treating it as unavailable (skipped, not fabricated) rather
  than inventing a fetch. This phase follows the identical precedent
  (section 4) rather than introducing new behavior for it.
- **`Symbol.start_line`/`end_line`** are `NOT NULL`; `start_column`/
  `end_column` are nullable. Line-level extraction is always possible when
  `inline_content` is available; column-level is not relied upon.
- **`AnalysisRun`**: no new `analysis_type` value is needed or added. The
  explanation step is a stateless, on-demand computation layered on top of
  an impact analysis that already gets its own `AnalysisRun` row
  (`analysis_type = "impact_analysis"`); see section 7 for why no new
  persistence is added for the explanation itself.
- **Configuration**: `app/core/config.py`'s `Settings` (pydantic-settings,
  `.env`-backed, `lru_cache`d `get_settings()`) is the single existing
  convention — AI configuration extends it, nothing new (section 11).

## 1. The architecture boundary (the one invariant that must never break)

```text
Deterministic graph
        ↓
Impact Analysis (app/impact/traversal.py — UNCHANGED)
        ↓
evidence / paths / affected symbols / files
        ↓
context assembly (sections 2–4, pure + bounded)
        ↓
LLM reasoning (section 5, an external call)
        ↓
human-readable explanation (section 6, validated before trusting it)
```

**The LLM never determines the impact set, invents graph relationships, or
overrides the traversal.** Concretely, enforced by construction, not just
convention:

- The LLM is never given database access, graph-query ability, or anything
  resembling a tool that could change which symbols count as "impacted."
- Its structured response (section 6) has no field capable of adding,
  removing, or re-scoring an impacted symbol — `direct_impacts`/
  `transitive_impacts`/`depth` are never echoed back as writable; they flow
  one direction only, from the deterministic result into the prompt.
- Every citation the model makes is validated against a closed, pre-built
  evidence set (section 4/8) before being trusted or shown to a caller — an
  unknown citation is sanitized, never surfaced as if it were real.
- If the provider is unavailable or fails, the deterministic result is
  still returned in full (section 9) — nothing about Impact Analysis itself
  depends on AI availability.

## 2. Evidence model

```text
Evidence
    evidence_id         # stable, e.g. "E1", "E2", ... — assigned once per
                         # context package, never re-derived from model output
    type                # "target_source" | "direct_dependent" |
                         # "transitive_dependent" | "impact_path" |
                         # "file_context"
    symbol_id
    file_path
    line_start
    line_end
    relationship_type    # present for direct_dependent / impact_path
    depth
    content               # the actual source excerpt, or None if unavailable
                         # (never fabricated — section 4)
```

Every excerpt handed to the model carries its own citable reference
(`app/payments/service.py:42-57`); the model is told to cite `[E_id]`
markers rather than invent its own line numbers (section 8's citation
discipline is the enforcement mechanism).

## 3. Context assembly: priority and budget

`app/impact/context.py` (pure — no `Session`, no SQLAlchemy; takes an
already-computed `ImpactAnalysisResult` plus already-extracted source text
as input) builds a bounded, deterministic `ContextPackage`. Priority order,
applied until a fixed evidence-count budget is exhausted — never "send
everything":

1. **Target symbol** — its own source span, always included first.
2. **Direct impacts** — each direct dependent's source span, plus the
   specific connecting edge's evidence location (an `impact_path` Evidence
   entry built from the real `GraphEdge.evidence_start_line`/`end_line`
   that proves *why* it's a direct dependent — re-queried, bounded to
   direct impacts only, not threaded through the traversal algorithm
   itself).
3. **Shortest-depth transitive impacts** — metadata only by default
   (symbol/file/depth/relationship_types), ordered by the same
   deterministic ordering `compute_impact` already produces (depth ASC,
   file_path ASC, qualified_name ASC, symbol_id ASC) — source is included
   only up to the remaining budget, shallowest depth first.
4. **File-level summary** — the already-computed `impacted_files`
   aggregation, included as compact metadata (not full file dumps) so the
   model has a sense of overall blast radius without every symbol's source.

**Budget**: a fixed maximum evidence-item count and a fixed maximum total
character count (both configurable constants, not environment-driven for
v1 — this is a code-level policy, not a per-request tuning knob). Once
either limit is hit, lower-priority items are omitted entirely — never
truncated mid-excerpt, which would risk handing the model a syntactically
broken, misleading fragment.

**Determinism**: the same `ImpactAnalysisResult` (and the same extracted
source snapshot) always produces the same `ContextPackage` — same evidence
items, same `evidence_id` assignment, same ordering. No randomness, no
model-dependent branching in the assembly step itself.

**Explicitly not implemented in v1**: full multi-hop path reconstruction
for *transitive* impacts (the specific chain of edges connecting the
target to a depth-3 symbol, with every intermediate hop's evidence
location). `compute_impact` doesn't expose BFS parent pointers (and
wasn't modified to, per this phase's hard constraint), so only *direct*
impacts get a concrete `impact_path` edge excerpt; transitive impacts get
accurate metadata (depth, relationship types) without a reconstructed
path. This is a documented v1 boundary, not an oversight.

## 4. Source extraction

`app/impact/source.py` — the one new DB-touching module for this phase,
alongside `service.py`. Given a revision and a set of `(symbol_id, file_id,
start_line, end_line)` tuples (from already-resolved `Symbol` rows — never
re-deriving locations some other way), it:

1. Loads each referenced `SourceFile`/`SourceBlob` pair.
2. If `inline_content` is `None` (content lives externally via
   `storage_key`), returns no source for that symbol — matching
   `app/parsing/service.py`'s own precedent for the identical condition.
   **Never invents or approximates content.**
3. Slices `inline_content.splitlines()[start_line - 1 : end_line]` — a
   plain line-range extraction. No column-level precision is attempted
   (`Symbol.start_column`/`end_column` are nullable and not relied upon).

Bounded to only the symbols context assembly actually needs (target,
direct impacts, and as many shortest-depth transitive impacts as the
budget allows) — never a whole-file dump when a symbol-level range is
available, and never a repository-wide fetch.

## 5. Provider abstraction

`app/ai/` — a narrow interface, not a general AI platform:

```python
class AIProvider(Protocol):
    def generate_structured(
        self, *, system_prompt: str, user_content: str, response_model: type[T]
    ) -> T: ...
```

One concrete implementation (`AnthropicProvider`, a single HTTP call via
`httpx` — not the vendor SDK, to avoid adding a new heavy dependency for
one call shape) plus a factory, `get_provider()`, that reads
`Settings.ai_api_key`/`ai_model` and returns `None` — not an exception —
when no key is configured. **Never called at import or startup time**: the
factory only constructs a provider object when `app/impact/explanation.py`
actually asks for one, on request, and the backend starts and serves every
deterministic route identically whether or not AI configuration is present.

`app/impact/explanation.py` depends only on the `AIProvider` protocol —
never imports `httpx` or any vendor-specific type directly. Tests use a
fake in-memory provider (section 10); no real network call happens in the
test suite.

## 6. Structured output

```text
ImpactExplanation
    summary: str
    why_it_matters: str
    affected_areas: list[str]
    review_recommendations: list[str]
    test_recommendations: list[str]
    evidence: list[str]          # evidence_ids actually cited, e.g. ["E1","E2"]
    limitations: str             # explicit, required — not optional prose
```

**No numeric field anywhere in this model.** No `risk_score`, no
`confidence`, no `probability`. This mirrors, deliberately, the exact
correction already made to `GraphEdge.confidence` — a number here would
carry the same false precision. `limitations` is a required field
specifically so the model has a designated place to say "evidence is
insufficient to assess X" rather than omitting a caveat silently.

## 7. Citation / evidence discipline

The model is given evidence under stable IDs (`E1`, `E2`, ...) and
instructed to cite them — never to invent its own file/line references
(`docs/prompts/impact-intelligence-v1.md`'s system instruction says this
explicitly). After a response is parsed:

1. The `evidence` field's IDs are checked against the context package's
   actual evidence set.
2. The free-text fields (`summary`, `why_it_matters`, each
   `affected_areas`/`review_recommendations`/`test_recommendations` entry)
   are scanned for inline `[E<n>]`-shaped citation markers.
3. Any ID — in either place — not present in the real evidence set is
   **sanitized**, never shown to a caller as if it resolved to something
   real: the bracket marker is replaced with `[unverified]` in free text,
   and stripped from the `evidence` list. The response as a whole is still
   returned (an unknown citation is a model mistake worth surfacing
   conservatively, not grounds to discard an otherwise-valid explanation).

This makes it structurally impossible for a fabricated `[E47]` to reach a
user and be mistaken for a real `app/foo.py:12-20` reference, since the API
layer resolves `E<n>` → file:line strictly from the evidence set it built
itself, never from anything the model said about where `E<n>` points.

## 8. Fallback behavior

```text
analysis_status = "deterministic_only"    # no explanation attempted/available
analysis_status = "explained"             # explanation present and validated
```

Impact Analysis's deterministic result is **always** returned in full,
regardless of AI availability. Explanation is strictly additive:

- No API key configured → `get_provider()` returns `None` →
  `analysis_status = "deterministic_only"`, no explanation field, no error.
- Provider call raises (network failure, timeout, non-2xx, malformed JSON,
  a structured response that fails Pydantic validation) → caught, logged,
  `analysis_status = "deterministic_only"` — **never** a fabricated
  explanation, never a 500 for what is fundamentally a successful
  deterministic analysis with an unavailable optional enhancement.

## 9. Security boundary

Repository source code is **untrusted input**, handled the same way
`docs/architecture/repository-ingestion.md` already treats arbitrary
cloned content: data, never instructions.
`docs/prompts/impact-intelligence-v1.md`'s system instruction explicitly
tells the model that everything under "UNTRUSTED REPOSITORY EVIDENCE" is
data to analyze, not commands to follow, and the prompt template keeps
system instructions and evidence in physically separate, clearly labeled
sections of the request — never concatenated such that repository text
could be mistaken for a system-level directive. No code is ever executed.
No environment variable, API key, or credential is ever included in
anything sent to the model (the context assembler only ever sources
`Evidence.content` from `SourceBlob.inline_content` slices and symbol
metadata — nothing from `app.core.config.Settings` is reachable from
there).

## 10. Testing strategy (no live LLM calls)

Every test uses a fake, in-memory `AIProvider` implementation — the real
`AnthropicProvider`'s HTTP call is never exercised in the automated suite.
Pure tests (context assembly, evidence/citation validation, structured-
output parsing, deterministic-vs-explained branching) need no database.
DB-dependent tests (source extraction, full-pipeline explanation) follow
the same explicit-skip-without-Postgres convention as every prior phase.

## 11. Provider configuration

Added to `Settings`: `ai_provider` (default `"anthropic"`), `ai_model`
(default pinned to a specific model string), `ai_api_key` (default `None`
— absence is a normal, fully-supported state, not a startup error). No
secret is ever read by, or exposed to, frontend code — this is a backend-
only `Settings` field, never serialized into any API response.

## 12. API

Extends the existing endpoint rather than adding a new one:
`POST /repositories/{repository_id}/impact` gains an optional
`include_explanation: bool = False` request field. The deterministic
response shape is unchanged when it's absent/false; when true, the
response additionally carries `analysis_status` and (when explained) the
validated `ImpactExplanation` plus a resolved `evidence` map (`"E1" ->
{file_path, line_start, line_end}`) so a caller can show real citations
without re-deriving them. Chosen over a separate endpoint because the
explanation is conceptually "more detail about the same analysis," not a
different resource, and because it keeps the "deterministic result is
always available" guarantee structurally obvious — one response, one
optional additional field, never two round-trips where the first could
succeed and the second silently diverge.

## 13. Why RAG is intentionally deferred

This phase operates only on deterministically-selected evidence (the exact
symbols the graph traversal already proved are impacted, plus their exact
source spans). Nothing here does similarity search, embedding, or
retrieval over the rest of the repository — the context is 100%
graph-derived and bounded. RAG (semantic search for `relevant files Impact
Analysis didn't already identify`) is a different capability with a
different correctness model (similarity, not proof) and is explicitly a
later, separate phase.

## 14. Why risk scoring is intentionally deferred

No calibrated model exists (the same reasoning that led to making
`GraphEdge.confidence` nullable rather than a fabricated constant). This
phase can state deterministic facts ("7 direct dependents," "the affected
caller consumes the return value directly, per the supplied source") but
has no basis for "87% chance of breaking production" or even a
heuristic-free "high/medium/low" tier. Introducing one now would repeat,
at the explanation layer, exactly the mistake already corrected at the
graph layer.

## 15. Adversarial hardening pass (findings and decisions)

A focused adversarial pass over the provider boundary, structured output,
citation validation, and prompt-injection boundary — performed before
approving this phase as a stable foundation. One real bug was found and
fixed; several behaviors were tightened and are now explicitly documented
rather than left implicit.

**Complete failure-boundary trace** (API request → `get_provider()` →
`AIProvider` → HTTP request → provider response → structured parsing →
`ImpactExplanation` validation → citation validation/sanitization → API
response):

| Stage | Failure mode | Outcome |
|---|---|---|
| `get_provider()` | no API key | `None`, silent, normal |
| `get_provider()` | key present, unsupported `ai_provider` | `AIConfigurationError` raised — caught by the route, degrades to `deterministic_only` |
| HTTP request | connection/DNS/timeout failure | `httpx.HTTPError` → `AIProviderUnavailableError` |
| HTTP request | 4xx/5xx status | `httpx.HTTPStatusError` (an `HTTPError` subclass) → `AIProviderUnavailableError` |
| Response parsing | missing `content`/`text` key, empty list | `KeyError`/`IndexError` → `AIResponseValidationError` |
| Response parsing | `text` present but not a string | **bug found and fixed** — previously an un-typed `TypeError` from `json.loads`; now explicitly checked and raised as `AIResponseValidationError` |
| Response parsing | `text` not valid JSON | `json.JSONDecodeError` → `AIResponseValidationError` |
| Structured validation | JSON doesn't match `ImpactExplanation` (missing/wrong-type/extra fields) | `pydantic.ValidationError` → `AIResponseValidationError` |
| Orchestration | *any* exception from `provider.generate_structured(...)`, anticipated or not | caught by a deliberately broad `except Exception` in `generate_impact_explanation` — see below |
| Citation validation | unknown `[E<n>]` reference | sanitized to `[unverified]`, never exposed as real |

**1. Provider failures fail closed, and the orchestration catch is
deliberately broader than the anticipated exception types.** The bug found
above proves why: a provider implementation can misbehave in ways its own
author didn't anticipate. `generate_impact_explanation`'s `try` around
`provider.generate_structured(...)` therefore catches `Exception`, not
just `AIProviderError` — this is the one seam where "the AI provider
misbehaved" must never become "the deterministic impact result is lost."
This broad catch is scoped *only* to that one call; the DB reads above it
(`extract_source_spans`, `extract_direct_impact_edge_evidence`) are not
covered, and a genuine database fault there still propagates as a real
error, exactly like every other DB-touching endpoint in this codebase —
conflating "AI is optional" with "the database is optional" would be
dishonest.

**2. Timeouts are explicit and asymmetric, not one uniform value.**
`httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)` — connect/
write/pool fail fast (10s is generous for reaching a reachable host or
flushing a small request body); read is 60s because model generation for
up to 4096 tokens can legitimately take that long. **Zero retries,
deliberately**: a bounded retry-with-backoff for transient 429/5xx could
be added later if real usage shows a need, but for v1 "fail once, fail
closed, let the existing deterministic-only fallback handle it" is
simpler and avoids retry-timing complexity for a code path that already
degrades gracefully on any single failure.

**3. The exact Anthropic request/response contract is now tested
directly**, not just "some mocked JSON": a test captures the literal
`headers`/`json`/`timeout` kwargs `httpx.post` is called with and asserts
the API key is in `x-api-key`, the model comes from configuration (not
hardcoded), the system instruction and user content are two distinct
fields that never contain each other's text, and the timeout is the
explicit asymmetric object above.

**4. Provider configuration honesty (Task 5, Option A).** Only
`AI_PROVIDER=anthropic` is implemented. This is not silently tolerant of
`AI_PROVIDER=openai` or any other value: if an API key is present (the
user clearly intends to use AI) but the provider name isn't supported,
`get_provider()` raises `AIConfigurationError` — a clear, typed signal of
a configuration mistake, distinct from "AI simply isn't set up" (no key
at all, which remains a silent `None`). The API route still degrades this
to `analysis_status = "deterministic_only"` rather than a 500, per the
same "never lose the deterministic result" principle.

**5. Prompt-injection defenses are now tested with actual adversarial
payloads** — "ignore all previous instructions," "reveal the API key,"
malicious-looking function/file names, README-style content embedding
fake system instructions — fed through the real context assembler and
prompt renderer, confirming the payload only ever appears between
`---BEGIN/END UNTRUSTED REPOSITORY EVIDENCE---` markers in the user
content and never inside the separately-sent, fixed `SYSTEM_PROMPT`
constant.

**6. Citation validation policy, exhaustively tested.** `[E999]` (unknown
numeric id), `[E0]` (never issued — real ids start at `E1`), `[E-1]` and
`[ E1 ]` (not citation-shaped at all — left as literal text, matching
neither a real nor a sanitized form), `[e1]` (recognized as an *attempted*
citation via case-insensitive matching, so it's flagged `[unverified]`
rather than silently ignored as plain text — but never treated as
equivalent to a real `E1`; exact, case-sensitive comparison decides
validity), `[E01]` (leading zero — never equal to `E1`), and
`[E1][E999]` (adjacent citations, both detected independently) are all
explicitly tested. The policy throughout: **unknown citation → explicit
sanitization to `[unverified]`, never silent remapping to a different
evidence item.** A dedicated test confirms sanitization cannot make an
unsupported claim *look* identically supported to one with a real
citation.

**7. Evidence-contract boundary (Task 8), stated precisely rather than
attempted as NLP verification.** The schema enforces exactly one thing
mechanically: the `evidence` field and all inline citations must resolve
to real IDs. It does **not** mechanically enforce "every code-specific
claim must cite something" — that's a prompt-level instruction (`limitations`
exists specifically so the model has a place to admit insufficient
evidence), not a schema-level guarantee, and pretending otherwise would be
exactly the kind of impossible natural-language verification this phase
explicitly avoids. This boundary is deliberate, not an oversight.

**8. Structured output smuggling attempts are explicitly, exhaustively
tested**: `risk_score`, `confidence`, `probability`, `impacted_symbols`,
`new_relationships`, and `depth` are each individually tested and
rejected by `model_config = ConfigDict(extra="forbid")`, alongside
wrong-type and null-for-required-field cases.

**9. API failure-semantics state machine, explicit:**

```text
analysis_status = "deterministic"       # include_explanation=false — provider never even constructed
analysis_status = "deterministic_only"  # requested, but: no key / AIConfigurationError / any provider failure
analysis_status = "explained"           # requested, succeeded, citations validated
```

A genuine database fault during evidence-gathering is a fourth, different
case: it is **not** folded into `deterministic_only` — it propagates as a
real error (ultimately a 500 via the app's existing global exception
handler), because it is not an "AI provider" failure at all.

**10. Secret-handling audit.** Confirmed empirically (not just by
inspection) that `httpx.HTTPStatusError` and `httpx.ConnectError`'s own
`str()` representations never include headers or request bodies — a
dedicated parametrized test asserts a real-looking secret and repository
content never appear in any exception raised across 401/403/429/500 and
connection-failure scenarios. `AI_API_KEY` is a backend-only `Settings`
field, never serialized into any API response, never logged (the one log
call in the hardened code logs only the unsupported provider *name*, a
non-secret string already visible in the user's own configuration).

**11. Context-size behavior, corrected.** The original budget logic
*omitted* an evidence item entirely if its content didn't fit the
remaining character budget — losing even its file/location metadata.
This is now **degrade to metadata-only (`content = None`), never omit the
item, never truncate mid-excerpt**: every candidate up to the item-count
budget still gets an `evidence_id` and a citable location; only whether
its source *text* is included depends on the remaining character budget.
Verified for the "target source alone exceeds the entire budget" case
specifically, and for "an earlier oversized item degrades while a later,
smaller one still fits" — evidence IDs remain sequential and valid
throughout either way.

**12. Unavailable source is now explicitly flagged, not ambiguous.** A
`target_source`/`direct_dependent`/`transitive_dependent` evidence item
with no content (storage_key-backed, or budget-degraded) now renders
`(source text not available for this evidence item)` in the prompt,
rather than a bare header a model could misread as "nothing more was
needed here." `impact_path` items are exempt from this marker — they
never carry source text by design (their evidence *is* the location and
relationship type), so there is nothing "unavailable" to flag.
