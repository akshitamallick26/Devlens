# Source Intelligence (Phase 4, v1)

This document covers turning persisted `SourceFile` records into
deterministic, revision-scoped `Symbol` records — the first real parsing
pipeline. It implements `docs/architecture/domain-model.md` and
`docs/architecture/database-design.md`; it does not change them. No call
graph, dependency graph, RAG, LLM calls, or security scanning exist yet.

## 1. Scope

`SourceFile -> language detection -> parser -> syntax validation -> symbol
extraction -> import extraction -> persisted Symbol records -> parsing
status`, for files already ingested by `app/ingestion/`. Deterministic,
revision-scoped, reproducible, synchronous (no background workers).

## 2. Supported language

**Python only.** TypeScript/JavaScript and everything else is explicitly
deferred — not stubbed, not half-implemented. A file whose
`SourceFile.language` isn't `"python"` gets `parsing_status = 'skipped'`,
the same honest non-outcome as an unsupported file type elsewhere in this
system.

## 3. Parser technology and rationale

The standard library `ast` module. No tree-sitter. Rationale: `ast` is
already part of the Python runtime (zero new dependency), its output is a
precise, deterministic representation of exactly what the interpreter would
execute, it carries real source-location metadata
(`lineno`/`col_offset`/`end_lineno`/`end_col_offset`) since Python 3.8, and
`ast.unparse()` (3.9+) gives a faithful, deterministic textual
reconstruction of any sub-expression — used here to render signatures
without inventing a second code-formatting layer. Tree-sitter would matter
once a second language needs a *shared* parsing abstraction across languages
with inconsistent native ASTs, or incremental re-parsing of edited buffers —
neither applies yet (this phase always parses a file in full, see section
15), so introducing it now would be exactly the kind of speculative
infrastructure this phase was told not to build.

## 4. Parser boundary

`app/parsing/parser.py`'s `parse_source_file(content: str, repo_relative_path:
str, language: str | None) -> ParseResult` is the entire boundary. It is a
pure function: no SQLAlchemy import, no `Session`, no filesystem access, no
network access, no global mutable state. `app/parsing/python_parser.py`
implements the Python case; the dispatch function itself is a two-branch
`if`, not a registry or factory — adding a second language means one more
branch and one more `<language>_parser.py` module.

## 5. ParseResult shape

```python
ParseResult:
    status: str                      # "parsed" | "failed" | "skipped"
    language: str | None
    symbols: list[ParsedSymbol]
    imports: list[ParsedImport]
    syntax_error: ParseSyntaxError | None

ParsedSymbol:
    kind, name, qualified_name: str
    start_line: int;  start_column: int | None
    end_line: int;    end_column: int | None
    signature: str | None
    parent_qualified_name: str | None   # resolved to a real parent_symbol_id
                                         # only by the persistence layer

ParsedImport:
    module: str | None            # None only for a bare relative `from . import x`
    imported_name: str | None     # None for plain `import x` forms
    alias: str | None
    level: int                    # 0 = absolute, 1+ = leading-dot count
    is_wildcard: bool
    line: int

ParseSyntaxError:
    message: str
    line, column, end_line, end_column: int | None
```

`ParsedSymbol`/`ParsedImport` are pure data — there is no field anywhere in
this shape that could record a resolved call target, a resolved import
target, or a resolved base class. That is not an oversight; see section 18.

## 6. Symbol kinds extracted

`module` (one per successfully parsed file), `class`, `function` (module-level
or nested-in-a-function), `method` (a `def`/`async def` whose *immediate*
enclosing container is a class body). Plain variable/constant assignments are
**not** extracted this phase — an explicit scope boundary, not a gap; the
schema's `variable`/`constant` kinds remain available for a later phase.
There is no separate `async_function` kind — see section 16's schema
observations.

## 7. Qualified-name rules

Built from actual AST nesting, never from names alone — the extractor
(`_Extractor` in `python_parser.py`) tracks the enclosing qualified-name
prefix as it walks, descending generically into every compound statement
(`if`/`for`/`while`/`try`/`with`, including `except` handler bodies) so a
`def` or `class` nested inside control flow is still found at the correct
nesting depth.

A module's own qualified name is its dotted path, derived from the
repository-relative file path following standard Python packaging
convention: `a/b/c.py` -> `a.b.c`; `a/b/__init__.py` -> `a.b` (a package's
`__init__.py` represents the package itself, not a submodule literally named
`__init__`). Every other symbol's qualified name is
`{module_dotted_path}.{local_nesting_path}` — e.g.
`payments.charge.PaymentService.charge.inner` for a function nested inside a
method inside a class inside `payments/charge.py`. Including the module path
in every qualified name (not just the local nesting) is deliberate: it keeps
`qualified_name` meaningful for cross-file search within a revision (its
documented purpose — domain-model.md section 4), since two files can
otherwise both have a top-level function with the same local name.

## 8. Deterministic symbol identity

Per the explicit design-audit requirement, two distinct notions:

1. **Database primary key**: `Symbol.id`, a client-generated UUID. A new
   symbol gets a fresh `uuid4`; a symbol that a re-parse of the same
   revision reproduces **keeps its existing id** (section 15). Many records
   hold a `Symbol.id` as a lasting reference (investigation-session
   breadcrumbs, generated tests, documentation artifacts, chunk and
   security-observation links), so a revision's source being immutable is
   matched by its symbol ids being stable across re-parses.
2. **Deterministic semantic identity**: the tuple
   `(revision_id, source_file_path, kind, qualified_name)`. Two parses of the
   same revision produce identical values for this tuple — that's what
   "deterministic" means here, and it's exactly what
   `test_repeated_parsing_is_idempotent` (pure) and the DB-level idempotency
   tests in `tests/parsing/test_service.py` and
   `tests/parsing/test_reparse_lifecycle_db.py` check.

The tuple is also how a re-parse matches new parser output to existing
rows (`app/parsing/service.py::_assign_symbol_ids`): within one revision, a
symbol is matched by its `SourceFile` row, `kind` and `qualified_name`.
`qualified_name` is not unique within a file (a property getter and its
setter, or a redefined function, share one), so equal keys are paired in
source-position order (`start_line`, then `start_column`).

This tuple is **not** persisted as a column (no schema change was made) —
it's a documented formula, not a stored value. Cross-*revision* symbol
identity (e.g. "is this the same logical function after a rename") is
explicitly **not** solved here — each revision's symbols are independent
rows, per `domain-model.md` section 16's own list of open questions.

## 9. Source-location semantics

Lines are 1-indexed, columns are 0-indexed — CPython's own `ast` convention,
used as-is, never renumbered. A module symbol's span is `1` to
`content.count("\n") + 1`, computed from the source text directly rather
than from the AST, because `ast.Module` nodes carry no position attributes
in CPython (they represent the whole file, not a positioned construct) —
the one deliberate exception to "use the AST's actual location metadata."
Every other symbol's `start_line`/`start_column`/`end_line`/`end_column`
comes directly from `node.lineno`/`node.col_offset`/`node.end_lineno`/`node.end_col_offset`.
Verified by test for: one-line functions, multiline (parenthesized
parameter list spanning several lines) functions, a decorator that does
**not** shift the decorated function's own `start_line` (the decorator line
is separate), nested functions, nested classes, and indentation-dependent
column offsets.

## 10. Syntax-error handling

`ast.parse()` raising `SyntaxError` is caught and converted to a
`ParseSyntaxError` (message, line, column, end_line, end_column) — never
re-raised, never silently discarded, and the source is never "repaired" or
guessed at. Two additional, less obvious failure modes are handled the same
way rather than being allowed to crash the whole run: `ValueError` (raised
by `ast.parse` for a small set of malformed-source cases, e.g. an embedded
NUL byte) and `RecursionError` (pathologically deep nesting can exceed
Python's own recursion limit during parsing *or* during this module's own
AST walk — both are covered by one `try` block). Parsing is an
untrusted-input boundary, same posture as
`docs/architecture/repository-ingestion.md` section 3.

## 11. Import extraction semantics

All forms are extracted deterministically, anywhere in the file (not only
module-level — a `def` containing a lazy `import` is still captured):
`import x`, `import x.y`, `import x as y`, `from x import y`,
`from x import y as z`, relative imports (`level` = number of leading dots;
`module` is `None` only for a bare `from . import x`), and wildcard imports
(`from x import *`, `is_wildcard=True`, `imported_name=None`).

**Not persisted anywhere yet — a deliberate, documented gap, not an
oversight.** `Symbol` represents definitions, not import bindings, so
recording `import os` as a `module`-kind `Symbol` would conflate two
different concepts the domain model keeps separate. `GraphEdge` is the
natural eventual home for "this file imports that module," but creating
`GraphEdge` rows is explicitly out of scope for this phase. The structured
`ParsedImport` data exists in every `ParseResult` — fully extracted, fully
tested — ready for the graph phase to consume; it's simply discarded by the
persistence layer (`app/parsing/service.py`) rather than invented a
premature home for it.

## 12. Persistence boundary

`app/parsing/service.py` is the only module that touches a `Session`.
`run_parsing(db, repository_id, revision_id)`:

1. Looks up the `RepositoryRevision` (raises `RevisionNotFoundError` if
   it doesn't belong to that repository).
2. Creates an `AnalysisRun(analysis_type='parsing', status='running')` and
   commits it immediately — durable independent of what follows, same
   pattern as `app/ingestion/service.py`'s revision-then-work split.
3. Reads the revision's existing symbol ids by identity (section 8), then
   re-parses every `SourceFile` for the revision (ordered by path) and builds
   the new `Symbol` rows in memory, reusing the id of every symbol it
   reproduces — `app/parsing/python_parser.py` is never called with a
   database object in scope.
4. Writes the rebuild (section 15) and commits once, all together: inserted,
   updated and deleted `Symbol` rows, every `SourceFile.parsing_status`
   update, the `AnalysisRun` completion, `RepositoryRevision.latest_analysis_status`.
   On any unexpected exception, rolls back and records failure in a
   separate, small commit (mirroring the ingestion service's established
   pattern exactly — not a new design).

A `SourceFile` whose content lives in external storage
(`SourceBlob.storage_key` rather than `inline_content`) is treated as
`skipped`, not `failed` — external storage isn't implemented yet (per
`database-design.md`), so there's genuinely no content for this phase's
parser to read, which is a "not supported," not an error.

## 13. Parsing-status transitions

Uses the existing `SourceFile.parsing_status` enum as-is —
`pending -> parsed`, `pending -> failed`, `pending -> skipped`. No new state
machine, no schema change. There is **no** persisted "parsing" (in-progress)
state: since this phase runs synchronously within one `run_parsing()` call,
no other observer could ever see a file paused mid-parse, so persisting a
transient value nothing ever reads would be pure overhead. A per-file syntax
error only fails that file — `RepositoryRevision`/`Repository` are never
marked failed because one Python file has invalid syntax (explicit
instruction; verified by `test_source_files_are_parsed_and_the_invalid_file_fails_cleanly`
and `test_a_failed_file_has_no_partial_symbols`).

## 14. Analysis-run behavior

One `AnalysisRun(analysis_type='parsing', analyzer_name='devlens-python-ast-parser',
analyzer_version='python-ast@<interpreter version>')` per `run_parsing()`
call, with `started_at`/`finished_at`/`status` set exactly as
`database-design.md` section 11 specifies (real columns, not buried in
`config`). Per-file syntax-error details are aggregated onto
`AnalysisRun.error_detail` as `{"failed_files": {path: {message, line,
column}}}` when any occurred — `SourceFile` has no column of its own for
"why did parsing fail" (see section 16), so the run that discovered the
failures is the natural place for the detail, per `error_detail`'s own
documented purpose as structured context, not necessarily evidence of a
failed run.

## 15. Idempotency strategy

**Identity-preserving rebuild, scoped to one revision.** Every parse
re-parses every file in the revision (no incremental logic — see below),
then `_write_symbols` reconciles the result with the revision's existing
rows by identity (section 8):

1. symbols the new parse did not have before are **inserted** with new ids;
2. symbols it reproduces are **updated in place**, keeping their id
   (including `analysis_run_id`, which moves to the latest parse);
3. only symbols the new parse no longer produces are **deleted**, in one
   revision-scoped statement.

In that order every `parent_symbol_id` already exists when written, and no
kept row is ever deleted, so nothing that references a kept symbol is
cascaded or set to NULL. Graph edges of deleted symbols cascade away; the
pipeline reports the graph outdated after any re-parse and rebuilds it
(`analysis-pipeline.md`). No unique constraint was added to `Symbol` (the
schema wasn't modified for this).

Re-parsing an unchanged revision with the same parser therefore changes no
symbol id, and the same symbol count, qualified names and ids result every
time, with a new `AnalysisRun` row per parse (full history is preserved —
only derived `Symbol` rows are rebuilt, not the audit trail of how many
times parsing ran).

**Historical references refuse a deleting re-parse.** An investigation
session breadcrumb (`SessionContextRef`, `ref_type = 'selected_symbol'`) is
a historical record: its `symbol_id` FK is `ON DELETE SET NULL`, and its
one-hot CHECK requires that column to stay populated, so the database would
reject deleting a referenced symbol anyway. Before any write, if a symbol
the new parse no longer produces is still referenced by a breadcrumb,
parsing raises `HistoricalReferenceError` (a `ParsingError`) instead: the
run is recorded `failed` with that message, `latest_analysis_status`
becomes `failed`, and every existing `Symbol`, graph edge and breadcrumb is
left unchanged. A symbol only vanishes this way when the parser's output
changes for the same immutable source (for example a different Python
version); an unchanged parser reproduces every symbol and never hits this.

## 16. Known limitations (including schema observations from the Task 1 audit)

- **No `async_function` kind**: `Symbol.kind`'s existing allow-list has no
  slot for "this function is async." Async-ness is preserved losslessly in
  `signature` (`"async def ..."`, from `ast.unparse`) instead of being a
  separately queryable kind. Not a blocking issue; flagged, not fixed, since
  fixing it would mean modifying the schema.
- **No per-file error-detail column** on `SourceFile`: syntax-error detail
  for a failed file lives on the `AnalysisRun` instead (section 14), since
  there's no column on `SourceFile` itself for it.
- **No persistence location for import metadata** (section 11) — captured
  in `ParseResult`, discarded by the persistence layer, not invented a home.
- **No variable/constant extraction.**
- **No cross-revision symbol identity** (section 8) — each revision's
  symbols are independent.
- **No incremental re-parsing** — see next section.
- **A re-parse that would delete a breadcrumb-referenced symbol is refused**
  (section 15), rather than turning the breadcrumb into a placeholder; no
  placeholder/tombstone mechanism exists.

## 17. Future TypeScript/tree-sitter extension point

A second language means: a new `<language>_parser.py` implementing the same
pure `content, path -> ParseResult` shape, one more branch in
`parser.py`'s dispatch, and (when TS/JS's native AST proves insufficient or
a shared cross-language abstraction becomes genuinely necessary) swapping
that one module's internals for tree-sitter without touching this phase's
persistence layer, `ParseResult` shape, or anything in `app/ingestion/`.
Nothing here assumes Python-only data shapes beyond `python_parser.py` and
`parser.py` themselves.

## 18. Call-graph resolution is explicitly NOT implemented

No call expression is ever matched to the symbol it might call. No import is
ever matched to the file/symbol it might refer to. No class's base-class
name is ever matched to another class definition, in this file or any other.
Identical names in different scopes are never conflated into "the same
symbol." `tests/parsing/test_no_semantic_resolution.py` tests this directly:
a call to a same-named local function produces zero relationship data, two
identically-named methods in different classes remain fully distinct rows,
`ParsedSymbol`/`ParsedImport` have no field that could even represent a
resolved target, and a structural guard test confirms nothing in
`app/parsing/` imports `GraphEdge`. This distinction is deliberate and load-bearing:
the future impact-analysis feature's correctness depends entirely on the
graph never claiming more than it can prove, and this phase extracts
structure, not semantics.
