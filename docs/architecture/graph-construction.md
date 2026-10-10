# Graph Construction (Phase 5, v1)

This document covers building `GraphEdge` rows from persisted, revision-scoped
`Symbol`/`SourceFile` data. It implements `docs/architecture/domain-model.md`
and `docs/architecture/database-design.md`'s already-designed `GraphEdge`
schema; **no schema change was made or needed** (see the audit in section 0).
Deterministic Impact Analysis (reverse traversal over this exact graph) now
exists — see `docs/architecture/impact-analysis.md`. Visualization, RAG, and
LLM calls still do not.

**Core principle, stated once and applied everywhere below: UNKNOWN is
better than WRONG.** Every persisted edge must have an explicit, provable
reason. When evidence is ambiguous or absent, the correct outcome is no edge
— that is success, not a gap to be filled later with a guess.

## 0. Task 1 audit result: no blocking schema mismatch

Re-read directly from `app/models/graph.py` before writing any resolution
code:

- `relationship_type IN ('imports', 'calls', 'inherits', 'implements', 'references')`.
  This phase uses exactly two: `imports`, `calls`, `inherits`. (`implements`
  and `references` are defined but unused by this phase — not every allowed
  value needs a producer yet.)
- `source_symbol_id` is `NOT NULL` — **both ends of every edge are `Symbol`
  rows**, there is no file-level or module-level node type. This matters
  directly for import edges: see section 3.
- `target_symbol_id` / `target_external_ref`: exactly one populated (CHECK
  constraint) — the schema already distinguishes resolved-internal from
  confident-external. No change needed.
- `confidence: float, 0.0–1.0` and `resolution_method: 'syntactic' | 'semantic'`
  already existed. **Superseded**: this was wrong on both counts — writing a
  fixed `confidence = 1.0` constant turned out to itself be a fabricated
  score, and neither `'syntactic'` nor `'semantic'` precisely described this
  phase's actual resolution technique. Migration `0002` made `confidence`
  nullable and added `'ast_resolution'` as a third allowed value. See
  section 7 for the corrected, final proof model.
- `evidence_file_id` / `evidence_start_line` (`NOT NULL`) / `evidence_end_line`
  / `evidence_start_column` / `evidence_end_column` already exist and are
  populated from real AST locations — see section 12 (no new location
  columns needed).
- Composite FKs already enforce same-revision for source/target/evidence
  (`(revision_id, source_symbol_id) -> symbols(revision_id, id)`, etc.) —
  this is a **second, independent, database-level layer** behind this
  phase's own revision-scoped resolution tables (section 9). Even a bug in
  this phase's Python code could not produce a cross-revision edge; the
  database would reject it.
- Two partial unique indexes already prevent duplicate edges from identical
  evidence (`uq_graph_edges_dedup_internal` / `_external`).

**Conclusion: the schema supported everything else this phase needs without
modification.** The one exception — `confidence`/`resolution_method`, both
corrected by migration `0002` after initial review missed that a constant
score is itself fabricated — is covered in full in section 7.

## 1. Graph scope

One `RepositoryRevision` at a time. Python source only. Three relationship
kinds: imports, calls, inherits.

## 2. Supported language

Python only — reusing `app/parsing/`'s existing `ast`-based parser output
plus a second, graph-specific AST pass (section 4). No TypeScript/JavaScript,
no cross-language resolution.

## 3. Node identity

Every edge endpoint is a `Symbol` row — no generic graph-node abstraction,
per the domain model. There is no separate "file node": a file's own
`module`-kind `Symbol` (already created by the parsing phase, one per file)
stands in for "this file" wherever an edge needs to reference the file
itself — e.g. the *source* of every `imports` edge is the importing file's
module `Symbol`, never a new kind of node invented for this phase.

## 4. Edge identity

An edge is identified by `(revision_id, relationship_type, source_symbol_id,
target_symbol_id-or-target_external_ref, evidence_file_id, evidence_start_line)`
— exactly the tuple the existing partial unique indexes already enforce.
Constructing two edges with identical evidence is a bug this phase's own
delete-and-rebuild idempotency (section 10) and the database both guard
against.

## 5. Edge direction (the core invariant)

```
IMPORT:    importer_module_symbol  →  imported_symbol_or_module
CALL:      caller_symbol           →  callee_symbol
INHERITS:  subclass_symbol         →  base_class_symbol
```

Forward traversal (`source_symbol_id = X`) answers "what does X depend on."
Reverse traversal (`target_symbol_id = X`) answers "what depends on X" —
impact analysis, later, is built entirely on reverse traversal. Getting this
backwards would silently invert every future impact-analysis result, so it
is tested explicitly (`tests/graph/test_edge_direction.py`) rather than only
documented.

## 6. Edge kinds

| Kind | Source | Target | Target can be external? |
|---|---|---|---|
| `imports` | importing file's module `Symbol` | resolved symbol, or the imported module's `Symbol`, or external | Yes |
| `calls` | enclosing function/method/module `Symbol` at the call site | resolved function/method `Symbol`, or external | Yes (import-mediated only — see section 6b) |
| `inherits` | subclass `Symbol` | resolved base class `Symbol` | **No** — see section 6c |

**6a. Why `imports`' source is always a module symbol, never a more specific
one**: an import statement is a property of the *file*, not of whichever
function happens to textually contain it (imports nested inside a function
body are still attributed to the file's module symbol, not that function) —
this matches how Python actually scopes import bindings (file-wide once
executed) and keeps one consistent, predictable source for every import
edge.

**6b. Why `calls` *can* target external code, deliberately narrowly**: only
when the call's receiver resolves through a **proven import** to something
external (e.g. `import os` then `os.getcwd()`, or `from os import getcwd`
then `getcwd()`) — never for a bare unresolved name with no import evidence
at all. An unresolved bare call (`charge()` with no matching definition and
no import) produces **no edge**, not an external-ref guess — we have no
positive evidence it's external rather than an internal reference we simply
failed to resolve (see section 8's wildcard-import note).

**6c. Why `inherits` never targets external code**: Task 10's explicit
instruction only asks for internal resolution ("handle imported base classes
only when the import resolver can prove the target"). Unlike imports/calls,
there's no concrete worked example asking for an external inheritance edge,
and `database-design.md`'s original design didn't anticipate "depends on an
external base class" as a case worth representing yet. An unresolvable or
external base produces no edge. This is a deliberate, narrower scope than
imports/calls — documented here rather than silently inconsistent.

**Constructing a `Symbol` is never a side effect of graph construction.**
This phase only ever reads existing `Symbol` rows (produced by the parsing
phase) and the module `Symbol`s that already exist for every file — it never
creates a `Symbol` to represent an external dependency or a module, per
Task 4/5's explicit "do not manufacture a Symbol" instruction.

## 7. Confidence / proof model

**v1 proof model, final:**

```text
resolved internal edge:
  target_symbol_id != NULL
  target_external_ref == NULL
  confidence == NULL

external edge:
  target_symbol_id == NULL
  target_external_ref != NULL
  confidence == NULL

unresolved reference:
  no GraphEdge row
```

`confidence` is `NULL` for every edge this phase writes, meaning "not
calibrated" — not "zero confidence," and not a stand-in for any numeric
value. No v1 analyzer has a calibration model (e.g. validated against a
labeled dataset of known-correct/incorrect edges), so writing any number —
`1.0`, `0.9`, `0.5`, anything — would fabricate a precision that doesn't
exist. (An earlier version of this phase wrote a fixed `confidence = 1.0`
and described that as "never fabricating a graduated score"; that reasoning
was wrong — a constant is itself a fabricated score, not an absence of one.)
`GraphEdge.confidence` was made nullable by migration `0002` (see
`database-design.md` section 4); the schema's `CHECK(confidence BETWEEN 0
AND 1)` is unchanged and already tolerates `NULL` under standard SQL
three-valued logic.

**The actual proof of resolution is never `confidence`.** It is, and has
only ever been: whether a `GraphEdge` row exists at all (unresolved
references produce no row — section 8), and whether `target_symbol_id` or
`target_external_ref` is populated (section 6). `confidence` carries no
information distinguishing resolved-internal from external from anything
else, since it is `NULL` identically in both cases. A future analyzer with
an actual calibration model may populate a real score; `NULL` must never be
mistaken for one in the meantime.

`resolution_method` is written as `"ast_resolution"` for this phase's edges
— a precise, dedicated value added by migration `0002`, distinct from the
schema's other two reserved values: `"syntactic"` (tree-sitter/pattern
matching — not yet implemented by any analyzer) and `"semantic"`
(type-checker-backed resolution — also not yet implemented; this is
`domain-model.md`'s original, narrower definition of "semantic"). This
resolver performs deterministic AST-based scope, import, and binding
resolution (real Python name-resolution semantics: lexical scope chains,
`self`-binding, import-graph resolution) — meaningfully more rigorous than
naive text/pattern matching, but it runs no type checker and does no type
inference. Neither `"syntactic"` nor `"semantic"` described that precisely,
so a third value was added rather than overloading either existing one.
`"ast_resolution"` is reserved exclusively for this technique; it must not
be used to describe a future type-inference-backed resolver (which should
use `"semantic"`) or a future naive pattern-matcher (which should use
`"syntactic"`).

## 8. Resolved vs. unresolved references

A reference (a call, an inheritance base, or an import) is **resolved**
only when one of the specific, enumerated rules in sections 9–11 proves a
target. Everything else — ambiguous candidates, out-of-scope names, unknown
receivers, dynamic attribute access, wildcard-import-introduced names,
anything requiring MRO computation or type inference — is **unresolved**,
meaning: no `GraphEdge` row at all. `tests/graph/test_false_positives.py`
tests this directly as the *expected*, correct outcome for each case, not an
incidental gap.

**8a. Five resolution subtleties that affect correctness, not just
completeness** (all caught via testing, not part of the original design
sketch — recorded here so they aren't silently re-broken):

- **A first parameter is only a self/cls receiver inside a real method.**
  The self-detection in `resolve_name_chain` uses "the enclosing scope's own
  first positional parameter name" rather than hardcoding the literal string
  `"self"`, so it also covers `cls` and unconventionally-named receivers.
  But that heuristic alone isn't sufficient: a plain function's or nested
  function's first parameter is just an arbitrary, unproven name, not a
  receiver with any special resolution rights. Before treating `x.foo()` as
  a self-style lookup, the resolver now also confirms the enclosing scope's
  own `Symbol.kind == "method"` *and* its enclosing scope is a `Symbol.kind
  == "class"`. Without that check, `def run(service): service.helper()`
  would incorrectly resolve `service.helper()` against the module's own
  namespace — a same-name coincidence, not a proven reference — purely
  because `service` happens to be `run`'s first parameter.
- **A parameter or local variable shadows a same-named import, including in
  attribute calls.** `import payments` followed by `def run(payments):
  payments.charge()` must not resolve `payments.charge()` against the
  imported module — the parameter shadows the import for the rest of the
  function body. The two-segment branch of `resolve_name_chain` now walks
  the same enclosing lexical scope chain used for one-segment resolution to
  check this before ever consulting the import-binding table.
- **A nested def and a same-named plain rebinding in the identical scope are
  ambiguous, not resolvable by preferring one.** `def charge(): ...`
  followed later by `charge = get_handler()` in the same function body means
  a later `charge()` call actually refers to `get_handler()`'s result, not
  the nested function — but this resolver tracks no statement order, so it
  cannot tell that case apart from the reverse order (where the def would
  correctly win). When a scope has both a `Symbol` and a locally-bound plain
  name for the identical bare name, resolution stops and returns unresolved
  rather than guessing either way.

- **A package directory need not have its own `Symbol`.** A namespace
  package (PEP 420, no `__init__.py`) has no module `Symbol` representing
  the package itself, even though files directly inside it do. Resolving
  `from . import sibling` (or any `from <pkg> import <name>`) therefore
  checks whether the *specific* target (`<pkg>.<name>`) exists as a `Symbol`
  **first**, before falling back to checking whether `<pkg>` itself is a
  known internal module. Gatekeeping on the package's own symbol first would
  wrongly call every namespace-package import "external".
- **A plain dotted `import a.b.c` binds the name `a`, not `a.b.c`.**
  Python's import machinery binds the top-level package in the importer's
  namespace; `.b` and `.c` are reached only as attributes on it. The
  `imports` edge itself still targets the most specific proven symbol
  (`a.b.c`, per Task 5), but the *name-binding* table entry used to resolve
  later bare references like `a.foo()` must resolve against `a`, not `a.b.c`
  — otherwise a later `a.foo()` call could be misdirected into `a.b.c.foo`,
  or worse, silently resolve to an unrelated same-named symbol there. These
  are two different resolutions of the same import statement
  (`ResolvedImport.resolution` for the edge, `.binding_resolution` for the
  name table) and only diverge for this one import form.

## 9. Revision scoping

Every lookup table this phase builds (module-path → module `Symbol`,
qualified-name → `Symbol`) is built **fresh, per revision**, from only that
revision's own `Symbol`/`SourceFile` rows — never a global or cross-revision
cache (`app/graph/resolver.py`'s `RevisionTables`, built once per
`run_graph_construction()` call, holds no state across revisions). The
database's own composite FKs (section 0) are the second, independent layer
behind this: even if this phase's Python code had a bug that leaked a
different revision's `Symbol.id` into a candidate edge, the
`(revision_id, source_symbol_id) -> symbols(revision_id, id)` composite FK
would reject the insert. `tests/graph/test_revision_isolation.py` proves
this with two real revisions sharing a same-named symbol.

## 10. Idempotency

**Delete-and-rebuild, scoped to one revision** — the same strategy
`app/parsing/service.py` already uses for `Symbol` rows, for the same
reason: this phase always reconstructs the full edge set for a revision (no
incremental graph updates yet), so a blanket revision-scoped delete before
inserting the fresh set is simpler and more obviously correct than a
natural-key upsert, and needs no new unique constraint. `AnalysisRun` history
is preserved — each `run_graph_construction()` call creates its own run row;
only derived `GraphEdge` rows are rebuilt.

## 11. Unsupported dynamic behavior (confidence boundaries)

Explicitly out of scope, always resulting in "unresolved" rather than a
guess:

- **Dynamic dispatch / MRO**: `self.charge()` resolves only to a method
  defined *directly* on the enclosing method's own class — never searched up
  an inheritance chain. A method only available via a base class is
  unresolved here (a real limitation, not a bug — computing MRO correctly,
  including diamond inheritance, is out of scope for this phase).
- **Dynamic attribute receivers**: `get_service().charge()`,
  `handlers[name]()`, anything whose receiver isn't a plain dotted
  name-chain — no candidate is even extracted; there is nothing to resolve.
- **Unknown-type receivers**: `service.charge()` where `service` is a
  parameter/local variable — never resolved unless `service` is a name
  *directly* bound by a provable import in this file. Local variable/
  parameter *types* are never inferred.
- **Wildcard imports**: `from x import *` still produces an `imports` edge
  for the module dependency itself (that much is proven), but no name it
  might introduce is ever added to the file's resolvable-import table —
  calls to names that could only have come from a star-import are always
  unresolved.
- **Local-variable shadowing of outer-scope functions**: handled, not
  skipped — see section 13's scope-chain design — but only for the
  mainstream binding forms (`=`, annotated assignment, augmented assignment,
  `for`, `with ... as`, function parameters, plain `import`/`from import`
  bindings). Comprehension-scoped variables and `lambda` parameters are a
  documented, narrower gap (section 13).
- **`global`/`nonlocal` declarations**: not specially handled; see
  section 13.

## 12. Source locations

Real AST locations (`lineno`/`col_offset`/`end_lineno`/`end_col_offset`) are
used for every edge's `evidence_start_line`/`evidence_start_column`/
`evidence_end_line`/`evidence_end_column` — never invented. For `imports`,
the evidence is the import statement's own line. For `calls`, the evidence
is the call expression's location. For `inherits`, the evidence is the
*specific base-class expression's* location (not just the class
definition's line) — more precise, and still a real AST location.

## 13. Scope-chain resolution design (why shadowing is handled, deliberately)

The core principle ("UNKNOWN is better than WRONG") makes a specific, real
risk worth closing even though no task explicitly demanded it: a bare call
like `charge()` could textually match an outer-scope function definition
while actually referring to a local variable or parameter of the same name
in the calling function — resolving to the outer function in that case would
be a **wrong** edge, not just an incomplete one. `app/graph/python_extractor.py`
therefore tracks, per scope, both (a) nested `def`/`class` names directly in
that scope (checked against the already-persisted `Symbol` table by
qualified name) and (b) plain name bindings in that scope: assignment
targets (`Assign`/`AnnAssign`/`AugAssign`), `for`/`async for` targets,
`with`/`async with ... as` targets, and function parameters (positional,
keyword-only, `*args`, `**kwargs`).

Resolution walks the scope chain from the call/base-expression site outward,
one level at a time: at each level, if the name matches a known `Symbol`
there, that's the answer; if it instead matches a plain binding there (and
not a symbol), the name is shadowed at that level and resolution **stops**
with "unresolved" — it does not keep searching further out, because Python
itself wouldn't either. Only if no level of the chain binds the name at all
does resolution fall back to the file's import table (section 6a's imports
are deliberately **not** scope-chain-walked level by level — a function-local
`import` is treated as available file-wide, a narrow, documented
simplification, since nested imports are rare and getting this exactly right
would require tracking statement-execution-order within a scope, which nothing
else in this phase needs).

**Explicitly not modeled** (a function/class name used *inside* one of these
constructs would simply never be found as "locally bound" and could, in a
contrived case, be mis-attributed): comprehension/generator-expression loop
variables and `lambda` parameters don't get their own tracked scope —
calls inside a comprehension or lambda body are attributed to the nearest
enclosing *named* scope. This is a narrower, explicitly accepted gap, not an
oversight: closing it fully would require scope-tracking machinery
disproportionate to how rarely a comprehension/lambda parameter actually
collides with an outer function/class name in practice.

## 14. Future language extension

A second language means: a new `<language>_extractor.py` producing the same
`CallCandidate`/`InheritanceCandidate`/`ParsedImport` shapes (or close
enough that `resolver.py` and `service.py` need only minor adjustment), and
a new import/call-resolution ruleset specific to that language's binding
semantics (which will differ meaningfully from Python's — this phase's
`resolver.py` is not written as a language-agnostic abstraction, since
pretending Python's scoping rules generalize would be exactly the kind of
premature abstraction this project avoids). Cross-language resolution (a
Python file importing a TypeScript module or vice versa) is not designed for
and not attempted.
