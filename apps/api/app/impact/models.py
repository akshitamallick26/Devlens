"""Pure result model for Impact Analysis — no SQLAlchemy, no DB. See
docs/architecture/impact-analysis.md section 5. `EdgeView`/`SymbolView` are
the DB-agnostic shapes `app/impact/service.py` builds from real
`GraphEdge`/`Symbol`/`SourceFile` rows before handing them to
`app/impact/traversal.py`, which never imports SQLAlchemy.
"""

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class EdgeView:
    """One internal-to-internal `GraphEdge` row, relevant to reverse
    traversal. External-ref edges (`target_symbol_id IS NULL`) are never
    represented here — see section 6's "external targets" note: they can
    never be reached as a traversal frontier node in the first place, so
    there is nothing for this view to carry for them."""

    source_symbol_id: uuid.UUID
    target_symbol_id: uuid.UUID
    relationship_type: str


@dataclass(frozen=True)
class SymbolView:
    id: uuid.UUID
    qualified_name: str
    kind: str
    file_id: uuid.UUID
    file_path: str
    parent_symbol_id: uuid.UUID | None


@dataclass(frozen=True)
class TargetResolution:
    """The outcome of resolving a target by qualified name (or confirming
    one resolved by id). Three explicit outcomes, never a guess — section 7."""

    status: str  # "found" | "not_found" | "ambiguous"
    symbol: SymbolView | None = None
    candidates: tuple[SymbolView, ...] = ()


@dataclass(frozen=True)
class ImpactedSymbol:
    symbol_id: uuid.UUID
    qualified_name: str
    symbol_kind: str
    file_id: uuid.UUID
    file_path: str
    depth: int
    relationship_types: tuple[str, ...]
    parent_symbol_id: uuid.UUID | None


@dataclass(frozen=True)
class ImpactedFile:
    file_id: uuid.UUID
    path: str
    symbol_count: int
    minimum_depth: int


@dataclass(frozen=True)
class ImpactAnalysisResult:
    revision_id: uuid.UUID
    target_symbol_id: uuid.UUID
    target_symbol: SymbolView
    direct_impacts: tuple[ImpactedSymbol, ...]
    transitive_impacts: tuple[ImpactedSymbol, ...]
    impacted_files: tuple[ImpactedFile, ...]
    max_depth: int
