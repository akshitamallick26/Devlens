"""DB-touching, bounded graph expansion over the existing, unmodified
GraphEdge table. Deliberately NOT a reuse of app/impact/traversal.py — see
docs/architecture/rag-codebase-qa.md section 8 for why this is a
separate, purpose-fit, shallow multi-seed expansion rather than the
single-target deep-BFS impact algorithm. Revision-scoped, cycle-safe,
depth-limited.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GraphEdge, SourceFile, Symbol
from app.rag.models import GraphExpandedSymbol

DEFAULT_MAX_DEPTH = 1
DEFAULT_MAX_SYMBOLS = 20

# (relationship_type, direction label, which column is the "seed" side)
_REVERSE_SPECS = (("calls", "callers"), ("imports", "importers"), ("inherits", "subclasses"))
_FORWARD_SPECS = (("calls", "callees"), ("imports", "imports"), ("inherits", "base_classes"))


@dataclass(frozen=True)
class _EdgeRow:
    source_symbol_id: uuid.UUID
    target_symbol_id: uuid.UUID
    relationship_type: str


def expand_graph(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    seed_symbol_ids: list[uuid.UUID],
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
) -> list[GraphExpandedSymbol]:
    """For each seed symbol: reverse traversal (`target_symbol_id == seed`)
    finds callers/importers/subclasses; forward traversal
    (`source_symbol_id == seed`) finds callees/imports/base classes —
    respecting the unmodified canonical direction (`source = dependent,
    target = dependency`). Bounded by `max_depth` (hop count) and
    `max_symbols` (total expanded symbols returned, shortest-depth-first).
    Cycle-safe: `visited` is seeded with every seed symbol id before
    expansion starts, so expansion can never loop back and re-add a seed
    as if it were newly discovered."""
    if not seed_symbol_ids:
        return []

    edges = db.execute(
        select(GraphEdge.source_symbol_id, GraphEdge.target_symbol_id, GraphEdge.relationship_type)
        .where(GraphEdge.repository_id == repository_id)
        .where(GraphEdge.revision_id == revision_id)
        .where(GraphEdge.target_symbol_id.is_not(None))
        # Explicit ordering, not left to the database's arbitrary row
        # order: when expansion discovers more than max_symbols candidates,
        # which ones get truncated must be deterministic, not dependent on
        # physical storage order (the same "same inputs, same output"
        # requirement every other query in this codebase follows).
        .order_by(
            GraphEdge.source_symbol_id, GraphEdge.target_symbol_id, GraphEdge.relationship_type
        )
    ).all()
    reverse_adjacency: dict[uuid.UUID, list[_EdgeRow]] = {}
    forward_adjacency: dict[uuid.UUID, list[_EdgeRow]] = {}
    for source_id, target_id, relationship_type in edges:
        if target_id is None:
            continue  # excluded by the query filter above; narrows the type for mypy
        edge = _EdgeRow(source_id, target_id, relationship_type)
        reverse_adjacency.setdefault(target_id, []).append(edge)
        forward_adjacency.setdefault(source_id, []).append(edge)

    visited: set[uuid.UUID] = set(seed_symbol_ids)
    discovered: dict[
        uuid.UUID, tuple[uuid.UUID, str, str]
    ] = {}  # symbol_id -> (seed, rel_type, direction)

    frontier = list(seed_symbol_ids)
    depth = 0
    while frontier and depth < max_depth and len(discovered) < max_symbols:
        depth += 1
        next_frontier: list[uuid.UUID] = []
        for current_id in frontier:
            for edge in reverse_adjacency.get(current_id, []):
                _consider(
                    discovered,
                    visited,
                    next_frontier,
                    edge.source_symbol_id,
                    current_id,
                    edge.relationship_type,
                    _direction_for(edge.relationship_type, reverse=True),
                )
            for edge in forward_adjacency.get(current_id, []):
                _consider(
                    discovered,
                    visited,
                    next_frontier,
                    edge.target_symbol_id,
                    current_id,
                    edge.relationship_type,
                    _direction_for(edge.relationship_type, reverse=False),
                )
        frontier = next_frontier

    if not discovered:
        return []

    symbol_ids = list(discovered.keys())[:max_symbols]
    rows = db.execute(
        select(Symbol, SourceFile.path)
        .join(SourceFile, Symbol.file_id == SourceFile.id)
        .where(Symbol.repository_id == repository_id)
        .where(Symbol.revision_id == revision_id)
        .where(Symbol.id.in_(symbol_ids))
    ).all()

    results = []
    for symbol, path in rows:
        seed_id, relationship_type, direction = discovered[symbol.id]
        results.append(
            GraphExpandedSymbol(
                symbol_id=symbol.id,
                qualified_name=symbol.qualified_name,
                kind=symbol.kind,
                file_id=symbol.file_id,
                file_path=path,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                relationship_type=relationship_type,
                direction=direction,
                seed_symbol_id=seed_id,
            )
        )
    results.sort(key=lambda r: (r.qualified_name, r.symbol_id))
    return results


def _direction_for(relationship_type: str, *, reverse: bool) -> str:
    specs = _REVERSE_SPECS if reverse else _FORWARD_SPECS
    for rel, direction in specs:
        if rel == relationship_type:
            return direction
    return "related"  # relationship_type not in our known set (e.g. "references")


def _consider(
    discovered: dict[uuid.UUID, tuple[uuid.UUID, str, str]],
    visited: set[uuid.UUID],
    next_frontier: list[uuid.UUID],
    candidate_id: uuid.UUID,
    seed_id: uuid.UUID,
    relationship_type: str,
    direction: str,
) -> None:
    if candidate_id in visited:
        return
    visited.add(candidate_id)
    discovered[candidate_id] = (seed_id, relationship_type, direction)
    next_frontier.append(candidate_id)
