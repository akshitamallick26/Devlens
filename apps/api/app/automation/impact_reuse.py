"""Change-impact reuse (Task 5). Calls `app.impact.traversal.compute_impact`
directly — the actual, approved impact-traversal primitive — once per
changed symbol, aggregating results across all of them. Deliberately does
NOT go through `app.impact.service.run_impact_analysis_by_symbol_id`,
which creates its own `AnalysisRun` row per call: automation records
exactly one `AnalysisRun` for the whole coordinated run (Task 11), not N
nested ones for N changed symbols. The traversal semantics themselves
(direction invariant, cycle safety, depth/ordering) are completely
untouched — this module only loads data once and aggregates
`compute_impact`'s own results; it contains no traversal logic of its own.

Only the *current* revision's graph is ever loaded or traversed (Task 5's
explicit requirement) — deleted symbols have no node in the current graph
at all. Hardening Blocker 3: this is never silently represented as "zero
impact" for a deleted symbol. `compute_deleted_symbol_impacts` is pure (no
DB access, no graph traversal of any kind, parent or current) and simply
labels each deleted symbol's impact as explicitly unavailable.
"""

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.models import DeletedSymbolImpact, ImpactSummary, SymbolSnapshot
from app.impact.models import EdgeView, ImpactedFile, ImpactedSymbol, SymbolView
from app.impact.traversal import compute_impact
from app.models import GraphEdge, SourceFile, Symbol

_DELETED_SYMBOL_REASON = (
    "this symbol was deleted in the current revision and has no node in the current "
    "graph to traverse from; determining its former dependents would require traversing "
    "the parent revision's graph, which this phase's current-revision-only invariant "
    "does not permit"
)


def compute_deleted_symbol_impacts(
    deleted: tuple[SymbolSnapshot, ...],
) -> tuple[DeletedSymbolImpact, ...]:
    """No DB access, no graph traversal (parent or current) — a deleted
    symbol's impact is always reported as `"unavailable_deleted_symbol"`,
    never computed, never zero."""
    return tuple(
        DeletedSymbolImpact(
            symbol=symbol, impact_status="unavailable_deleted_symbol", reason=_DELETED_SYMBOL_REASON
        )
        for symbol in deleted
    )


# Bounded exactly like app/rag/graph_expansion.py and app/security/
# blast_radius.py bound their own traversals — a changeset touching an
# extreme number of symbols in one revision still produces a deterministic,
# bounded summary rather than an unbounded one.
MAX_CHANGED_SYMBOLS_TRAVERSED = 200


def _symbol_view(symbol: Symbol, file_path: str) -> SymbolView:
    return SymbolView(
        id=symbol.id,
        qualified_name=symbol.qualified_name,
        kind=symbol.kind,
        file_id=symbol.file_id,
        file_path=file_path,
        parent_symbol_id=symbol.parent_symbol_id,
    )


def _merge_impacted(bucket: dict[uuid.UUID, ImpactedSymbol], impacted: ImpactedSymbol) -> None:
    """Across multiple changed-symbol seeds, the same dependent can be
    reached at different depths (or via different relationship types) —
    keep the shallowest depth found (consistent with "shortest depth" being
    the documented meaning everywhere else in this codebase), and union the
    relationship types observed at that shallowest depth."""
    existing = bucket.get(impacted.symbol_id)
    if existing is None or impacted.depth < existing.depth:
        bucket[impacted.symbol_id] = impacted
    elif impacted.depth == existing.depth:
        merged_types = tuple(
            sorted(set(existing.relationship_types) | set(impacted.relationship_types))
        )
        bucket[impacted.symbol_id] = ImpactedSymbol(
            symbol_id=existing.symbol_id,
            qualified_name=existing.qualified_name,
            symbol_kind=existing.symbol_kind,
            file_id=existing.file_id,
            file_path=existing.file_path,
            depth=existing.depth,
            relationship_types=merged_types,
            parent_symbol_id=existing.parent_symbol_id,
        )


def _files_from_symbols(symbols: Iterable[ImpactedSymbol]) -> tuple[ImpactedFile, ...]:
    by_file: dict[uuid.UUID, list[ImpactedSymbol]] = {}
    for symbol in symbols:
        by_file.setdefault(symbol.file_id, []).append(symbol)
    files = [
        ImpactedFile(
            file_id=file_id,
            path=symbols_in_file[0].file_path,
            symbol_count=len(symbols_in_file),
            minimum_depth=min(s.depth for s in symbols_in_file),
        )
        for file_id, symbols_in_file in by_file.items()
    ]
    files.sort(key=lambda f: (f.minimum_depth, f.path, f.file_id))
    return tuple(files)


def compute_change_impact(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    changed_symbol_ids: set[uuid.UUID],
    deleted_symbols: tuple[SymbolSnapshot, ...] = (),
) -> ImpactSummary:
    deleted_symbol_impacts = compute_deleted_symbol_impacts(deleted_symbols)

    if not changed_symbol_ids:
        return ImpactSummary(
            changed_symbol_count=0,
            direct=(),
            transitive=(),
            affected_files=(),
            max_depth=0,
            truncated=False,
            deleted_symbol_impacts=deleted_symbol_impacts,
        )

    bounded_ids = sorted(changed_symbol_ids)[:MAX_CHANGED_SYMBOLS_TRAVERSED]
    truncated = len(changed_symbol_ids) > MAX_CHANGED_SYMBOLS_TRAVERSED

    rows = db.execute(
        select(Symbol, SourceFile.path)
        .join(SourceFile, Symbol.file_id == SourceFile.id)
        .where(Symbol.repository_id == repository_id)
        .where(Symbol.revision_id == revision_id)
    ).all()
    symbols_by_id = {symbol.id: _symbol_view(symbol, path) for symbol, path in rows}

    edge_rows = (
        db.execute(
            select(GraphEdge)
            .where(GraphEdge.repository_id == repository_id)
            .where(GraphEdge.revision_id == revision_id)
            .where(GraphEdge.target_symbol_id.is_not(None))
        )
        .scalars()
        .all()
    )
    edges = [
        EdgeView(
            source_symbol_id=edge.source_symbol_id,
            target_symbol_id=edge.target_symbol_id,
            relationship_type=edge.relationship_type,
        )
        for edge in edge_rows
        if edge.target_symbol_id is not None
    ]

    direct_by_id: dict[uuid.UUID, ImpactedSymbol] = {}
    transitive_by_id: dict[uuid.UUID, ImpactedSymbol] = {}
    max_depth = 0

    for symbol_id in bounded_ids:
        target = symbols_by_id.get(symbol_id)
        if target is None:
            continue  # changed symbol not present in the current graph — nothing to traverse from
        result = compute_impact(revision_id, target, edges, symbols_by_id)
        max_depth = max(max_depth, result.max_depth)
        for impacted in result.direct_impacts:
            _merge_impacted(direct_by_id, impacted)
        for impacted in result.transitive_impacts:
            _merge_impacted(transitive_by_id, impacted)

    # A symbol reached directly by one changed seed and only transitively
    # by another keeps its strongest (direct) classification.
    for symbol_id in list(transitive_by_id):
        if symbol_id in direct_by_id:
            del transitive_by_id[symbol_id]

    direct = tuple(
        sorted(
            direct_by_id.values(),
            key=lambda s: (s.depth, s.file_path, s.qualified_name, s.symbol_id),
        )
    )
    transitive = tuple(
        sorted(
            transitive_by_id.values(),
            key=lambda s: (s.depth, s.file_path, s.qualified_name, s.symbol_id),
        )
    )
    affected_files = _files_from_symbols((*direct, *transitive))

    return ImpactSummary(
        changed_symbol_count=len(changed_symbol_ids),
        direct=direct,
        transitive=transitive,
        affected_files=affected_files,
        max_depth=max_depth,
        truncated=truncated,
        deleted_symbol_impacts=deleted_symbol_impacts,
    )
