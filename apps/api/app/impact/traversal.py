"""Pure reverse-graph traversal — no SQLAlchemy, no DB, no I/O. See
docs/architecture/impact-analysis.md sections 6, 7, 9, and 10.

`compute_impact` receives an already-loaded, already-revision-scoped set of
`EdgeView`s and a `symbols_by_id` lookup (both built by
`app/impact/service.py` from real rows) and returns a deterministic
`ImpactAnalysisResult`. It never queries anything itself — revision
isolation is structural: this function is simply never given more than one
revision's data.
"""

import uuid
from collections import defaultdict

from app.impact.models import (
    EdgeView,
    ImpactAnalysisResult,
    ImpactedFile,
    ImpactedSymbol,
    SymbolView,
    TargetResolution,
)


def resolve_target(candidates: list[SymbolView]) -> TargetResolution:
    """Pure decision over however many `Symbol` rows matched a lookup (by id
    or by qualified name) — section 7. Never guesses among multiple
    matches."""
    if not candidates:
        return TargetResolution(status="not_found")
    if len(candidates) > 1:
        return TargetResolution(status="ambiguous", candidates=tuple(candidates))
    return TargetResolution(status="found", symbol=candidates[0])


def _build_reverse_adjacency(
    edges: list[EdgeView],
) -> dict[uuid.UUID, list[tuple[uuid.UUID, str]]]:
    adjacency: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = defaultdict(list)
    for edge in edges:
        adjacency[edge.target_symbol_id].append((edge.source_symbol_id, edge.relationship_type))
    return adjacency


def compute_impact(
    revision_id: uuid.UUID,
    target: SymbolView,
    edges: list[EdgeView],
    symbols_by_id: dict[uuid.UUID, SymbolView],
) -> ImpactAnalysisResult:
    """Breadth-first reverse traversal from `target`. `visited` is seeded
    with the target's own id *before* traversal starts — this single choice
    makes every cycle (multi-node or a direct self-loop) terminate
    correctly and guarantees the target never appears in its own impact
    set (Task 11.4's explicit preference). BFS order guarantees the first
    time any symbol is reached is its shortest reverse-distance from the
    target, so no symbol is ever revisited at a deeper level — "shortest
    depth" falls out of traversal order, not a post-hoc minimum pass."""
    reverse_adjacency = _build_reverse_adjacency(edges)

    visited: set[uuid.UUID] = {target.id}
    depth_by_symbol: dict[uuid.UUID, int] = {}
    relationship_types_by_symbol: dict[uuid.UUID, set[str]] = defaultdict(set)

    frontier = [target.id]
    depth = 0
    while frontier:
        depth += 1
        newly_reached: dict[uuid.UUID, set[str]] = defaultdict(set)
        for current_id in frontier:
            for source_id, relationship_type in reverse_adjacency.get(current_id, []):
                if source_id in visited:
                    # Already reached at a shallower (or equal, same-level)
                    # depth via a different path — do not re-record or
                    # re-expand (branch-merging / cycle safety).
                    continue
                newly_reached[source_id].add(relationship_type)

        if not newly_reached:
            break

        for symbol_id, relationship_types in newly_reached.items():
            visited.add(symbol_id)
            depth_by_symbol[symbol_id] = depth
            relationship_types_by_symbol[symbol_id] = relationship_types

        frontier = list(newly_reached.keys())

    impacted = [
        ImpactedSymbol(
            symbol_id=symbol_id,
            qualified_name=symbols_by_id[symbol_id].qualified_name,
            symbol_kind=symbols_by_id[symbol_id].kind,
            file_id=symbols_by_id[symbol_id].file_id,
            file_path=symbols_by_id[symbol_id].file_path,
            depth=symbol_depth,
            relationship_types=tuple(sorted(relationship_types_by_symbol[symbol_id])),
            parent_symbol_id=symbols_by_id[symbol_id].parent_symbol_id,
        )
        for symbol_id, symbol_depth in depth_by_symbol.items()
    ]

    # Stable, documented ordering (impact-analysis.md section 6):
    # depth ASC, file_path ASC, qualified_name ASC, symbol_id ASC.
    impacted.sort(key=lambda i: (i.depth, i.file_path, i.qualified_name, i.symbol_id))

    direct_impacts = tuple(i for i in impacted if i.depth == 1)
    transitive_impacts = tuple(i for i in impacted if i.depth > 1)

    files: dict[uuid.UUID, list[ImpactedSymbol]] = defaultdict(list)
    for impacted_symbol in impacted:
        files[impacted_symbol.file_id].append(impacted_symbol)

    impacted_files = [
        ImpactedFile(
            file_id=file_id,
            path=symbols_in_file[0].file_path,
            symbol_count=len(symbols_in_file),
            minimum_depth=min(s.depth for s in symbols_in_file),
        )
        for file_id, symbols_in_file in files.items()
    ]
    # Ordering mirrors the symbol ordering: shallowest impact first.
    impacted_files.sort(key=lambda f: (f.minimum_depth, f.path, f.file_id))

    max_depth = max((i.depth for i in impacted), default=0)

    return ImpactAnalysisResult(
        revision_id=revision_id,
        target_symbol_id=target.id,
        target_symbol=target,
        direct_impacts=direct_impacts,
        transitive_impacts=transitive_impacts,
        impacted_files=tuple(impacted_files),
        max_depth=max_depth,
    )
