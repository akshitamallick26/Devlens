"""Deterministic test-target discovery. DB-touching (reads already-
persisted Symbol/GraphEdge/SourceFile rows); this module never computes
graph impact itself — `impacted_symbol_ids`/`impacted_file_ids`/
`impact_depths` are optional inputs the caller already derived (e.g. from
app/impact/service.py), kept as a strict separation of responsibility (the
testing phase's core invariant: deterministic analysis decides impact, not
this module and never the LLM).
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GraphEdge, SourceFile, Symbol
from app.testing.existing_tests import find_existing_tests_for_targets
from app.testing.models import RelatedSymbol, TargetDiscoveryResult, TestTarget

# Not every Symbol kind is a sensible thing to generate a test for — a
# module, a constant, or a bare variable has no behavior to exercise.
TARGETABLE_SYMBOL_KINDS = ("function", "method", "class", "property")

MAX_TARGETS = 20
MAX_RELATED_SYMBOLS = 10

_SELECTION_PRIORITY = {"explicit_target": 0, "impacted_symbol": 1, "impacted_file": 2}


def _select_candidates(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    *,
    target_symbol_id: uuid.UUID | None,
    impacted_symbol_ids: set[uuid.UUID] | None,
    impacted_file_ids: set[uuid.UUID] | None,
) -> dict[uuid.UUID, str]:
    """Priority, lowest overwrites highest: impacted_file < impacted_symbol
    < explicit_target. A symbol matching more than one input keeps only the
    strongest reason it was selected for."""
    reasons: dict[uuid.UUID, str] = {}

    if impacted_file_ids:
        rows = db.execute(
            select(Symbol.id)
            .where(Symbol.repository_id == repository_id)
            .where(Symbol.revision_id == revision_id)
            .where(Symbol.file_id.in_(impacted_file_ids))
            .where(Symbol.kind.in_(TARGETABLE_SYMBOL_KINDS))
        ).all()
        for (symbol_id,) in rows:
            reasons[symbol_id] = "impacted_file"

    if impacted_symbol_ids:
        for symbol_id in impacted_symbol_ids:
            reasons[symbol_id] = "impacted_symbol"

    if target_symbol_id is not None:
        reasons[target_symbol_id] = "explicit_target"

    return reasons


def _related_symbols(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, symbol_ids: set[uuid.UUID]
) -> tuple[dict[uuid.UUID, list[RelatedSymbol]], dict[uuid.UUID, list[RelatedSymbol]]]:
    """Returns (callers_by_target, callees_by_target), each bounded to
    MAX_RELATED_SYMBOLS per target and deterministically ordered — mirrors
    app/rag/graph_expansion.py's explicit-ORDER-BY discipline."""
    if not symbol_ids:
        return {}, {}

    edges = db.execute(
        select(
            GraphEdge.source_symbol_id,
            GraphEdge.target_symbol_id,
            GraphEdge.relationship_type,
        )
        .where(GraphEdge.repository_id == repository_id)
        .where(GraphEdge.revision_id == revision_id)
        .where(
            GraphEdge.source_symbol_id.in_(symbol_ids) | GraphEdge.target_symbol_id.in_(symbol_ids)
        )
        .order_by(
            GraphEdge.source_symbol_id, GraphEdge.target_symbol_id, GraphEdge.relationship_type
        )
    ).all()

    related_ids: set[uuid.UUID] = set()
    caller_links: list[tuple[uuid.UUID, uuid.UUID, str]] = []  # (target, caller, rel)
    callee_links: list[tuple[uuid.UUID, uuid.UUID, str]] = []  # (target, callee, rel)
    for source_id, target_id, relationship_type in edges:
        if target_id is not None and target_id in symbol_ids:
            caller_links.append((target_id, source_id, relationship_type))
            related_ids.add(source_id)
        if source_id in symbol_ids and target_id is not None:
            callee_links.append((source_id, target_id, relationship_type))
            related_ids.add(target_id)

    if not related_ids:
        return {}, {}

    rows = db.execute(
        select(Symbol, SourceFile.path)
        .join(SourceFile, Symbol.file_id == SourceFile.id)
        .where(Symbol.revision_id == revision_id)
        .where(Symbol.id.in_(related_ids))
    ).all()
    by_id = {
        symbol.id: RelatedSymbol(
            symbol_id=symbol.id,
            qualified_name=symbol.qualified_name,
            kind=symbol.kind,
            file_id=symbol.file_id,
            file_path=path,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            relationship_type="",  # overwritten per-link below
        )
        for symbol, path in rows
    }

    def _bounded(
        links: list[tuple[uuid.UUID, uuid.UUID, str]],
    ) -> dict[uuid.UUID, list[RelatedSymbol]]:
        grouped: dict[uuid.UUID, list[RelatedSymbol]] = {}
        for target_id, other_id, relationship_type in links:
            base = by_id.get(other_id)
            if base is None:
                continue
            bucket = grouped.setdefault(target_id, [])
            if len(bucket) >= MAX_RELATED_SYMBOLS:
                continue
            bucket.append(
                RelatedSymbol(
                    symbol_id=base.symbol_id,
                    qualified_name=base.qualified_name,
                    kind=base.kind,
                    file_id=base.file_id,
                    file_path=base.file_path,
                    start_line=base.start_line,
                    end_line=base.end_line,
                    relationship_type=relationship_type,
                )
            )
        return grouped

    return _bounded(caller_links), _bounded(callee_links)


def discover_test_targets(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    *,
    target_symbol_id: uuid.UUID | None = None,
    impacted_symbol_ids: set[uuid.UUID] | None = None,
    impacted_file_ids: set[uuid.UUID] | None = None,
    impact_depths: dict[uuid.UUID, int] | None = None,
    max_targets: int = MAX_TARGETS,
) -> TargetDiscoveryResult:
    """No input given at all means no targets — this function never
    defaults to "every symbol in the revision" (unbounded, and would make
    "why was this selected" meaningless)."""
    reasons = _select_candidates(
        db,
        repository_id,
        revision_id,
        target_symbol_id=target_symbol_id,
        impacted_symbol_ids=impacted_symbol_ids,
        impacted_file_ids=impacted_file_ids,
    )
    if not reasons:
        return TargetDiscoveryResult(
            repository_id=repository_id, revision_id=revision_id, targets=()
        )

    symbol_rows = db.execute(
        select(Symbol, SourceFile.path)
        .join(SourceFile, Symbol.file_id == SourceFile.id)
        .where(Symbol.repository_id == repository_id)
        .where(Symbol.revision_id == revision_id)
        .where(Symbol.id.in_(reasons))
        .where(Symbol.kind.in_(TARGETABLE_SYMBOL_KINDS))
    ).all()
    if not symbol_rows:
        return TargetDiscoveryResult(
            repository_id=repository_id, revision_id=revision_id, targets=()
        )

    symbol_ids = {symbol.id for symbol, _ in symbol_rows}
    callers_by_id, callees_by_id = _related_symbols(db, repository_id, revision_id, symbol_ids)
    existing_tests_by_id = find_existing_tests_for_targets(
        db, repository_id, revision_id, symbol_ids
    )
    depths = impact_depths or {}

    targets = [
        TestTarget(
            target_symbol_id=symbol.id,
            qualified_name=symbol.qualified_name,
            kind=symbol.kind,
            file_id=symbol.file_id,
            file_path=path,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            selection_reason=reasons[symbol.id],
            impact_depth=depths.get(symbol.id),
            callers=tuple(callers_by_id.get(symbol.id, ())),
            callees=tuple(callees_by_id.get(symbol.id, ())),
            existing_tests=existing_tests_by_id.get(symbol.id, ()),
        )
        for symbol, path in symbol_rows
    ]
    targets.sort(
        key=lambda t: (
            _SELECTION_PRIORITY[t.selection_reason],
            t.qualified_name,
            t.target_symbol_id,
        )
    )
    return TargetDiscoveryResult(
        repository_id=repository_id, revision_id=revision_id, targets=tuple(targets[:max_targets])
    )
