"""Pure data shapes for graph construction — no SQLAlchemy, no database
session. See docs/architecture/graph-construction.md.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NameChainCandidate:
    """A dotted reference found in source: `charge` -> ("charge",);
    `self.charge` -> ("self", "charge"); `payments.charge` ->
    ("payments", "charge"). `None` chains (dynamic receivers — a call on the
    result of another call, a subscript, etc.) never become a candidate at
    all; see graph-construction.md section 11."""

    scope_qualified_name: str  # resolution starts here — see call vs. inherits below
    name_chain: tuple[str, ...]
    line: int
    column: int | None
    end_line: int | None
    end_column: int | None


@dataclass(frozen=True)
class CallCandidate(NameChainCandidate):
    """`scope_qualified_name` doubles as both the edge's source (the
    enclosing function/method/module) and where resolution starts, since for
    a call those are the same scope."""


@dataclass(frozen=True)
class InheritanceCandidate:
    """Unlike a call, the edge source (`subclass_qualified_name`) and where
    resolution starts (`scope_qualified_name`, the subclass's *parent*
    scope — a base class name is resolved in the enclosing scope, not inside
    the class body being defined) are different, so this isn't a
    NameChainCandidate."""

    subclass_qualified_name: str
    scope_qualified_name: str
    name_chain: tuple[str, ...]
    line: int
    column: int | None
    end_line: int | None
    end_column: int | None


@dataclass(frozen=True)
class GraphExtractionResult:
    """Output of the graph-specific AST pass for one file — see
    app/graph/python_extractor.py. `status` mirrors ParseResult's outcomes
    for the same file but is a separate type: extraction is a distinct
    concern from parsing (Task 3), even though both operate on the same
    source text."""

    status: str  # "extracted" | "failed" | "skipped"
    calls: list[CallCandidate] = field(default_factory=list)
    inheritances: list[InheritanceCandidate] = field(default_factory=list)
    # scope_qualified_name -> plain-binding names (assignment targets,
    # parameters, for/with targets) directly in that scope. Deliberately
    # excludes import bindings — those are already fully captured by
    # app.parsing.python_parser's ParseResult.imports and resolved
    # separately, as a file-wide fallback, not scope-chain-walked (see
    # graph-construction.md section 13's note on function-local imports).
    # Used only for shadowing checks during resolution.
    locally_bound_names: dict[str, frozenset[str]] = field(default_factory=dict)
    # function/method scope_qualified_name -> its first parameter's name (or
    # None if it has none). Used to recognize a `self`-like receiver without
    # hardcoding the literal name "self" — see graph-construction.md's
    # method-call resolution rule.
    first_parameter_by_scope: dict[str, str | None] = field(default_factory=dict)
