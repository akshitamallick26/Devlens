"""Security blast-radius computation. Pure traversal (`compute_blast_radius`)
plus a thin DB-loading wrapper (`compute_security_blast_radius`), mirroring
the pure/DB split app/impact/{traversal,service}.py and
app/rag/graph_expansion.py already established.

Deliberately NOT a reuse of app/impact/traversal.py::compute_impact's
*function* — same reasoning app/rag/graph_expansion.py already documented
for its own, different-shaped traversal: compute_impact has no bounds
(Task 9 requires depth/symbol/file/path bounds) and no predecessor
tracking (needed here for deterministic evidence paths), and modifying it
to add either would be exactly the "silently change Impact Analysis
behavior" this phase's instructions prohibit. What IS reused, verbatim, is
compute_impact's actual *semantics*: the canonical graph direction
invariant (source = dependent, target = dependency; reverse traversal =
blast radius), the cycle-safety idiom (seed the visited set with the seed's
own id before traversal starts), and the stable sort order (depth ASC,
file_path ASC, qualified_name ASC, symbol_id ASC).
"""

import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.impact.models import EdgeView, SymbolView
from app.models import GraphEdge, SourceFile, Symbol
from app.security.models import BlastRadiusFile, BlastRadiusResult, BlastRadiusSymbol, EvidencePath

DEFAULT_MAX_DEPTH = 5
DEFAULT_MAX_SYMBOLS = 50
DEFAULT_MAX_FILES = 50
DEFAULT_MAX_EVIDENCE_PATHS = 5


@dataclass(frozen=True)
class _Reached:
    depth: int
    relationship_types: frozenset[str]
    predecessor_id: uuid.UUID
    predecessor_relationship: str


def _build_reverse_adjacency(edges: list[EdgeView]) -> dict[uuid.UUID, list[tuple[uuid.UUID, str]]]:
    adjacency: dict[uuid.UUID, list[tuple[uuid.UUID, str]]] = defaultdict(list)
    for edge in edges:
        adjacency[edge.target_symbol_id].append((edge.source_symbol_id, edge.relationship_type))
    return adjacency


def compute_blast_radius(
    seed: SymbolView,
    edges: list[EdgeView],
    symbols_by_id: dict[uuid.UUID, SymbolView],
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
    max_files: int = DEFAULT_MAX_FILES,
    max_evidence_paths: int = DEFAULT_MAX_EVIDENCE_PATHS,
) -> BlastRadiusResult:
    reverse_adjacency = _build_reverse_adjacency(edges)

    visited: set[uuid.UUID] = {seed.id}
    reached: dict[uuid.UUID, _Reached] = {}

    frontier = [seed.id]
    depth = 0
    hit_depth_bound = False
    while frontier:
        if depth >= max_depth:
            hit_depth_bound = True
            break
        depth += 1
        newly_reached: dict[uuid.UUID, set[str]] = defaultdict(set)
        newly_reached_predecessor: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
        for current_id in frontier:
            for source_id, relationship_type in reverse_adjacency.get(current_id, []):
                if source_id in visited:
                    continue
                newly_reached[source_id].add(relationship_type)
                if source_id not in newly_reached_predecessor:
                    newly_reached_predecessor[source_id] = (current_id, relationship_type)
        if not newly_reached:
            break
        for symbol_id, relationship_types in newly_reached.items():
            visited.add(symbol_id)
            predecessor_id, predecessor_relationship = newly_reached_predecessor[symbol_id]
            reached[symbol_id] = _Reached(
                depth=depth,
                relationship_types=frozenset(relationship_types),
                predecessor_id=predecessor_id,
                predecessor_relationship=predecessor_relationship,
            )
        frontier = list(newly_reached.keys())

    impacted = [
        BlastRadiusSymbol(
            symbol_id=symbol_id,
            qualified_name=symbols_by_id[symbol_id].qualified_name,
            kind=symbols_by_id[symbol_id].kind,
            file_id=symbols_by_id[symbol_id].file_id,
            file_path=symbols_by_id[symbol_id].file_path,
            depth=info.depth,
            relationship_types=tuple(sorted(info.relationship_types)),
        )
        for symbol_id, info in reached.items()
    ]
    impacted.sort(key=lambda s: (s.depth, s.file_path, s.qualified_name, s.symbol_id))

    hit_symbol_bound = len(impacted) > max_symbols
    impacted = impacted[:max_symbols]

    direct_dependents = tuple(s for s in impacted if s.depth == 1)
    transitive_dependents = tuple(s for s in impacted if s.depth > 1)

    files: dict[uuid.UUID, list[BlastRadiusSymbol]] = defaultdict(list)
    for symbol in impacted:
        files[symbol.file_id].append(symbol)
    all_impacted_files = [
        BlastRadiusFile(
            file_id=file_id,
            path=symbols_in_file[0].file_path,
            symbol_count=len(symbols_in_file),
            minimum_depth=min(s.depth for s in symbols_in_file),
        )
        for file_id, symbols_in_file in files.items()
    ]
    all_impacted_files.sort(key=lambda f: (f.minimum_depth, f.path, f.file_id))
    hit_file_bound = len(all_impacted_files) > max_files
    impacted_files = tuple(all_impacted_files[:max_files])

    evidence_paths = []
    for symbol in impacted[:max_evidence_paths]:
        chain: list[uuid.UUID] = [symbol.symbol_id]
        relationship_chain: list[str] = []
        current = symbol.symbol_id
        while current != seed.id:
            info = reached.get(current)
            if info is None:
                break  # defensive: should be unreachable given how `reached` is built
            relationship_chain.append(info.predecessor_relationship)
            current = info.predecessor_id
            chain.append(current)
        chain.reverse()
        relationship_chain.reverse()
        evidence_paths.append(
            EvidencePath(symbol_ids=tuple(chain), relationship_types=tuple(relationship_chain))
        )

    max_depth_reached = max((s.depth for s in impacted), default=0)
    truncated = hit_depth_bound or hit_symbol_bound or hit_file_bound

    return BlastRadiusResult(
        seed_symbol_id=seed.id,
        direct_dependents=direct_dependents,
        transitive_dependents=transitive_dependents,
        impacted_files=impacted_files,
        max_depth_reached=max_depth_reached,
        truncated=truncated,
        evidence_paths=tuple(evidence_paths),
    )


def _symbol_view(symbol: Symbol, file_path: str) -> SymbolView:
    return SymbolView(
        id=symbol.id,
        qualified_name=symbol.qualified_name,
        kind=symbol.kind,
        file_id=symbol.file_id,
        file_path=file_path,
        parent_symbol_id=symbol.parent_symbol_id,
    )


def compute_security_blast_radius(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    seed_symbol_id: uuid.UUID,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
    max_files: int = DEFAULT_MAX_FILES,
    max_evidence_paths: int = DEFAULT_MAX_EVIDENCE_PATHS,
) -> BlastRadiusResult | None:
    """Returns `None` if `seed_symbol_id` does not exist in this exact
    repository/revision — the caller decides what that means (e.g. an
    unresolved finding has no seed to compute from in the first place)."""
    rows = db.execute(
        select(Symbol, SourceFile.path)
        .join(SourceFile, Symbol.file_id == SourceFile.id)
        .where(Symbol.repository_id == repository_id)
        .where(Symbol.revision_id == revision_id)
    ).all()
    symbols_by_id = {symbol.id: _symbol_view(symbol, path) for symbol, path in rows}
    seed = symbols_by_id.get(seed_symbol_id)
    if seed is None:
        return None

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

    return compute_blast_radius(
        seed,
        edges,
        symbols_by_id,
        max_depth=max_depth,
        max_symbols=max_symbols,
        max_files=max_files,
        max_evidence_paths=max_evidence_paths,
    )
