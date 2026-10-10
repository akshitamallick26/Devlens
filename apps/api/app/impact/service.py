"""Impact-analysis orchestration: resolves a target Symbol and computes its
reverse-graph impact for one revision, from already-persisted GraphEdge/
Symbol/SourceFile rows. Mirrors the AnalysisRun lifecycle established by
app/graph/service.py::run_graph_construction. This is the only module in
app/impact/ that touches a Session — target resolution (models.py's
TargetResolution) and the traversal itself (traversal.py) stay pure. See
docs/architecture/impact-analysis.md.
"""

import logging
import platform
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.impact.models import EdgeView, ImpactAnalysisResult, SymbolView, TargetResolution
from app.impact.traversal import compute_impact, resolve_target
from app.models import AnalysisRun, GraphEdge, RepositoryRevision, SourceFile, Symbol

logger = logging.getLogger("devlens.impact")

ANALYZER_NAME = "devlens-impact-analysis"


class ImpactAnalysisError(Exception):
    pass


class RevisionNotFoundError(ImpactAnalysisError):
    pass


def _log_event(event: str, **fields: object) -> None:
    logger.info(event, extra={"event": event, **fields})


def _analyzer_version() -> str:
    return f"impact-analysis@{platform.python_version()}"


def _symbol_view(symbol: Symbol, file_path: str) -> SymbolView:
    return SymbolView(
        id=symbol.id,
        qualified_name=symbol.qualified_name,
        kind=symbol.kind,
        file_id=symbol.file_id,
        file_path=file_path,
        parent_symbol_id=symbol.parent_symbol_id,
    )


def _load_all_symbol_views(db: Session, revision_id: uuid.UUID) -> dict[uuid.UUID, SymbolView]:
    rows = db.execute(
        select(Symbol, SourceFile.path)
        .join(SourceFile, Symbol.file_id == SourceFile.id)
        .where(Symbol.revision_id == revision_id)
    ).all()
    return {symbol.id: _symbol_view(symbol, path) for symbol, path in rows}


def _load_internal_edges(db: Session, revision_id: uuid.UUID) -> list[EdgeView]:
    """Only edges with a real internal target are relevant to reverse
    traversal — external-ref edges (target_symbol_id IS NULL) can never be
    a traversal frontier node (impact-analysis.md section 0/6), so they are
    filtered out here rather than carried through as dead weight."""
    rows = (
        db.execute(
            select(GraphEdge)
            .where(GraphEdge.revision_id == revision_id)
            .where(GraphEdge.target_symbol_id.is_not(None))
        )
        .scalars()
        .all()
    )
    return [
        EdgeView(
            source_symbol_id=edge.source_symbol_id,
            target_symbol_id=edge.target_symbol_id,  # never None — filtered above
            relationship_type=edge.relationship_type,
        )
        for edge in rows
        if edge.target_symbol_id is not None
    ]


def _resolve_target_by_id(
    symbols_by_id: dict[uuid.UUID, SymbolView], symbol_id: uuid.UUID
) -> TargetResolution:
    view = symbols_by_id.get(symbol_id)
    return resolve_target([view] if view is not None else [])


def _resolve_target_by_qualified_name(
    db: Session, revision_id: uuid.UUID, qualified_name: str
) -> TargetResolution:
    rows = db.execute(
        select(Symbol, SourceFile.path)
        .join(SourceFile, Symbol.file_id == SourceFile.id)
        .where(Symbol.revision_id == revision_id)
        .where(Symbol.qualified_name == qualified_name)
    ).all()
    candidates = [_symbol_view(symbol, path) for symbol, path in rows]
    return resolve_target(candidates)


@dataclass(frozen=True)
class ImpactAnalysisRunResult:
    status: str  # "completed" | "not_found" | "ambiguous"
    result: ImpactAnalysisResult | None = None
    candidates: tuple[SymbolView, ...] = ()
    analysis_run_id: uuid.UUID | None = None


def _get_revision(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> RepositoryRevision:
    """Validated before any target resolution — a nonexistent revision_id
    must never be allowed to masquerade as a legitimate "symbol not found"
    result; it's a different kind of error entirely."""
    revision = db.get(RepositoryRevision, revision_id)
    if revision is None or revision.repository_id != repository_id:
        raise RevisionNotFoundError(f"no revision {revision_id} for repository {repository_id}")
    return revision


def run_impact_analysis_by_symbol_id(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, symbol_id: uuid.UUID
) -> ImpactAnalysisRunResult:
    _get_revision(db, repository_id, revision_id)
    symbols_by_id = _load_all_symbol_views(db, revision_id)
    resolution = _resolve_target_by_id(symbols_by_id, symbol_id)
    return _run(db, repository_id, revision_id, resolution, symbols_by_id)


def run_impact_analysis_by_qualified_name(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, qualified_name: str
) -> ImpactAnalysisRunResult:
    _get_revision(db, repository_id, revision_id)
    resolution = _resolve_target_by_qualified_name(db, revision_id, qualified_name)
    symbols_by_id = _load_all_symbol_views(db, revision_id) if resolution.status == "found" else {}
    return _run(db, repository_id, revision_id, resolution, symbols_by_id)


def _run(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    resolution: TargetResolution,
    symbols_by_id: dict[uuid.UUID, SymbolView],
) -> ImpactAnalysisRunResult:
    if resolution.status != "found":
        # Not found / ambiguous: no AnalysisRun is recorded — nothing was
        # actually analyzed, so there's no completed-or-failed attempt
        # worth a durable record.
        return ImpactAnalysisRunResult(status=resolution.status, candidates=resolution.candidates)

    target = resolution.symbol
    assert target is not None

    run = AnalysisRun(
        repository_id=repository_id,
        revision_id=revision_id,
        analysis_type="impact_analysis",
        analyzer_name=ANALYZER_NAME,
        analyzer_version=_analyzer_version(),
        status="running",
        started_at=datetime.now(UTC),
        config={"target_symbol_id": str(target.id)},
    )
    db.add(run)
    db.commit()
    _log_event(
        "impact_analysis_started",
        repository_id=str(repository_id),
        revision_id=str(revision_id),
        target_symbol_id=str(target.id),
    )

    try:
        edges = _load_internal_edges(db, revision_id)
        # Impact analysis is read-only over an already-built graph — unlike
        # graph construction, a failure here never marks
        # revision.latest_analysis_status; the graph itself isn't affected.
        result = compute_impact(revision_id, target, edges, symbols_by_id)

        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        db.commit()
        _log_event(
            "impact_analysis_completed",
            revision_id=str(revision_id),
            target_symbol_id=str(target.id),
            direct_impacts=len(result.direct_impacts),
            transitive_impacts=len(result.transitive_impacts),
        )
        return ImpactAnalysisRunResult(status="completed", result=result, analysis_run_id=run.id)
    except Exception as exc:
        db.rollback()
        run.status = "failed"
        run.error_message = str(exc)[:2000]
        run.finished_at = datetime.now(UTC)
        db.commit()
        _log_event("impact_analysis_failed", revision_id=str(revision_id), reason=str(exc))
        raise ImpactAnalysisError(str(exc)) from exc
