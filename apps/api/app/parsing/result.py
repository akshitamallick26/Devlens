"""The parser boundary's output shape — pure data, no SQLAlchemy, no
database session, no I/O. A parser's only job is `source bytes/text + path +
language -> ParseResult`. See docs/architecture/source-intelligence.md
sections 4-5.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ParsedSymbol:
    """Mirrors the persisted `Symbol` columns this phase actually populates
    — see app/models/symbol.py. `parent_qualified_name` is resolved to a
    real `parent_symbol_id` by the persistence layer (service.py), never by
    the parser itself, since the parser never sees a database."""

    kind: str
    name: str
    qualified_name: str
    start_line: int
    start_column: int | None
    end_line: int
    end_column: int | None
    signature: str | None
    parent_qualified_name: str | None


@dataclass(frozen=True)
class ParsedImport:
    """Structural import information only — see
    docs/architecture/source-intelligence.md section 11 for exactly what
    "structural, not resolved" means here, and why this isn't persisted
    anywhere yet (no GraphEdge this phase, and no existing schema location
    for raw import metadata)."""

    module: str | None  # the dotted module path (None only for a bare relative `from . import x`)
    imported_name: (
        str | None
    )  # the specific name after `import`, if any (None for plain `import x`)
    alias: str | None  # the `as` binding, if any
    level: int  # 0 = absolute; 1+ = number of leading dots in a relative import
    is_wildcard: bool
    line: int


@dataclass(frozen=True)
class ParseSyntaxError:
    message: str
    line: int | None
    column: int | None
    end_line: int | None
    end_column: int | None


@dataclass(frozen=True)
class ParseResult:
    """`status` is one of "parsed", "failed", or "skipped" (unsupported
    language) — the same three outcomes `SourceFile.parsing_status` can
    represent for a per-file result (the fourth value, "pending", is a
    pre-parse state the parser itself never produces)."""

    status: str
    language: str | None
    symbols: list[ParsedSymbol] = field(default_factory=list)
    imports: list[ParsedImport] = field(default_factory=list)
    syntax_error: ParseSyntaxError | None = None
