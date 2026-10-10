"""DB-touching source/edge-evidence extraction for Impact Intelligence —
the second module in app/impact/ that touches a Session, alongside
service.py. Re-queries Symbol/SourceFile/SourceBlob/GraphEdge directly
rather than threading new fields through the already-approved
deterministic result model (app/impact/models.py, app/impact/traversal.py
— both left untouched by this phase). See
docs/architecture/impact-intelligence.md sections 0 and 4.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GraphEdge, SourceBlob, SourceFile, Symbol


@dataclass(frozen=True)
class SourceSpan:
    symbol_id: uuid.UUID
    file_path: str
    line_start: int
    line_end: int
    content: str | None  # None when inline_content isn't available — never fabricated


@dataclass(frozen=True)
class EdgeEvidence:
    """The real GraphEdge evidence location connecting a direct dependent
    to the target it depends on — bounded to direct impacts only (section
    3's documented "impact path" scope)."""

    source_symbol_id: uuid.UUID
    file_path: str
    line_start: int
    line_end: int | None
    relationship_type: str


def extract_source_spans(
    db: Session, revision_id: uuid.UUID, symbol_ids: list[uuid.UUID]
) -> dict[uuid.UUID, SourceSpan]:
    """Line-range extraction only (Symbol.start_column/end_column are
    nullable and not relied upon). Returns no entry's `content` when the
    file's content lives externally (storage_key, not inline_content) —
    the same precedent app/parsing/service.py already established for that
    exact condition; never approximated or invented."""
    if not symbol_ids:
        return {}

    rows = db.execute(
        select(Symbol, SourceFile.path, SourceBlob.inline_content)
        .join(SourceFile, Symbol.file_id == SourceFile.id)
        .join(SourceBlob, SourceFile.content_hash == SourceBlob.content_hash)
        .where(Symbol.revision_id == revision_id)
        .where(Symbol.id.in_(symbol_ids))
    ).all()

    spans: dict[uuid.UUID, SourceSpan] = {}
    for symbol, path, inline_content in rows:
        content: str | None = None
        if inline_content is not None:
            lines = inline_content.splitlines()
            content = "\n".join(lines[symbol.start_line - 1 : symbol.end_line])
        spans[symbol.id] = SourceSpan(
            symbol_id=symbol.id,
            file_path=path,
            line_start=symbol.start_line,
            line_end=symbol.end_line,
            content=content,
        )
    return spans


def extract_direct_impact_edge_evidence(
    db: Session,
    revision_id: uuid.UUID,
    target_symbol_id: uuid.UUID,
    direct_impact_ids: list[uuid.UUID],
) -> dict[uuid.UUID, EdgeEvidence]:
    if not direct_impact_ids:
        return {}

    rows = db.execute(
        select(GraphEdge, SourceFile.path)
        .join(SourceFile, GraphEdge.evidence_file_id == SourceFile.id)
        .where(GraphEdge.revision_id == revision_id)
        .where(GraphEdge.target_symbol_id == target_symbol_id)
        .where(GraphEdge.source_symbol_id.in_(direct_impact_ids))
    ).all()

    evidence: dict[uuid.UUID, EdgeEvidence] = {}
    for edge, path in rows:
        # One edge is enough to cite per direct impact — if more than one
        # relationship connects the same pair (e.g. both an import and a
        # call edge), the first one encountered wins. Still real evidence,
        # just not exhaustive.
        evidence.setdefault(
            edge.source_symbol_id,
            EdgeEvidence(
                source_symbol_id=edge.source_symbol_id,
                file_path=path,
                line_start=edge.evidence_start_line,
                line_end=edge.evidence_end_line,
                relationship_type=edge.relationship_type,
            ),
        )
    return evidence
