"""Deterministic mapping of a normalized security observation onto the
existing code graph (Task 8). DB-touching (reads already-persisted
Symbol/SourceFile/GraphEdge rows). Never creates a Symbol, never mutates
the graph, never guesses: a mapping is only ever reported when concrete
evidence supports it, and the exact resolution attempted is always
recorded via `GraphResolution.status`, even when it comes back
"unresolved" or "ambiguous".
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GraphEdge, SourceFile, Symbol
from app.security.models import GraphResolution


def resolve_source_finding(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, file_path: str, start_line: int
) -> GraphResolution:
    """For a Semgrep-style file/line finding: find the file, then the
    innermost Symbol whose span contains `start_line` — the standard,
    well-defined "most specific containing scope" choice, not a guess
    (containing spans nest strictly; picking the smallest is deterministic
    even when several symbols contain the same line)."""
    file_row = db.execute(
        select(SourceFile.id)
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == revision_id)
        .where(SourceFile.path == file_path)
    ).first()
    if file_row is None:
        return GraphResolution(status="unresolved")
    file_id = file_row[0]

    symbols = db.execute(
        select(Symbol.id, Symbol.start_line, Symbol.end_line)
        .where(Symbol.revision_id == revision_id)
        .where(Symbol.file_id == file_id)
        .where(Symbol.start_line <= start_line)
        .where(Symbol.end_line >= start_line)
    ).all()
    if not symbols:
        return GraphResolution(status="exact_file", file_id=file_id)

    smallest = min(symbols, key=lambda row: (row[2] - row[1], row[0]))
    return GraphResolution(status="exact_symbol", file_id=file_id, symbol_id=smallest[0])


def resolve_dependency_finding(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, package_name: str, ecosystem: str
) -> tuple[GraphResolution, ...]:
    """For an OSV-style package finding: find every symbol in this revision
    that directly imports `package_name` (a real `imports` GraphEdge to a
    matching `target_external_ref` — never a guessed module-name match).
    Returns one GraphResolution per distinct importing symbol, since a
    vulnerable dependency can legitimately be imported from many places;
    the caller decides how to fold that into a single observation's
    resolution_status (exact_symbol for exactly one, ambiguous for more
    than one, unresolved for zero) versus using the full set as blast-
    radius seeds."""
    if ecosystem.lower() != "pypi":
        # v1 only resolves Python imports — a non-Python ecosystem finding
        # is never guessed at via Python import-graph data.
        return ()

    rows = db.execute(
        select(GraphEdge.source_symbol_id)
        .where(GraphEdge.repository_id == repository_id)
        .where(GraphEdge.revision_id == revision_id)
        .where(GraphEdge.relationship_type == "imports")
        .where(
            (GraphEdge.target_external_ref == package_name)
            | GraphEdge.target_external_ref.like(f"{package_name}.%")
        )
        .distinct()
    ).all()
    symbol_ids = {row[0] for row in rows}
    if not symbol_ids:
        return ()

    symbols = db.execute(
        select(Symbol.id, Symbol.file_id)
        .where(Symbol.revision_id == revision_id)
        .where(Symbol.id.in_(symbol_ids))
        .order_by(Symbol.id)
    ).all()
    return tuple(
        GraphResolution(status="exact_symbol", file_id=file_id, symbol_id=symbol_id)
        for symbol_id, file_id in symbols
    )


def fold_dependency_resolutions(resolutions: tuple[GraphResolution, ...]) -> GraphResolution:
    """Collapses the full candidate set from `resolve_dependency_finding`
    into the single resolution a SecurityFindingObservation row can
    structurally hold: zero candidates is "unresolved", exactly one is
    "exact_symbol", more than one is honestly "ambiguous" — never an
    arbitrary pick among several real candidates."""
    if not resolutions:
        return GraphResolution(status="unresolved")
    if len(resolutions) == 1:
        return resolutions[0]
    return GraphResolution(status="ambiguous")
