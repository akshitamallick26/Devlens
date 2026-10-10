"""Graph-construction orchestration: Symbol/SourceFile rows -> GraphEdge
rows, for one revision. Mirrors the transaction/commit pattern established
in app/ingestion/service.py and app/parsing/service.py deliberately, not as
a new design. This is the only module in app/graph/ that touches a Session
or writes a GraphEdge row — extraction (python_extractor.py) and resolution
(resolver.py) stay pure. See docs/architecture/graph-construction.md.
"""

import logging
import platform
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.graph.python_extractor import extract_graph_candidates
from app.graph.resolver import (
    Resolution,
    RevisionTables,
    build_revision_tables,
    import_binding_table,
    resolve_imports_for_file,
    resolve_name_chain,
)
from app.models import AnalysisRun, GraphEdge, RepositoryRevision, SourceBlob, SourceFile, Symbol
from app.parsing.python_parser import module_dotted_name, parse_python_source

logger = logging.getLogger("devlens.graph")

ANALYZER_NAME = "devlens-python-graph-builder"
_CALLABLE_KINDS = frozenset({"function", "method"})


class GraphConstructionError(Exception):
    pass


class RevisionNotFoundError(GraphConstructionError):
    pass


@dataclass(frozen=True)
class GraphConstructionResult:
    revision_id: str
    analysis_run_id: str
    status: str
    import_edges: int
    call_edges: int
    inherits_edges: int


def _log_event(event: str, **fields: object) -> None:
    logger.info(event, extra={"event": event, **fields})


def _analyzer_version() -> str:
    return f"python-graph@{platform.python_version()}"


def _new_edge(
    *,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    analysis_run_id: uuid.UUID,
    relationship_type: str,
    source_symbol_id: uuid.UUID,
    resolution: Resolution,
    evidence_file_id: uuid.UUID,
    line: int,
    column: int | None,
    end_line: int | None,
    end_column: int | None,
) -> GraphEdge:
    return GraphEdge(
        repository_id=repository_id,
        revision_id=revision_id,
        analysis_run_id=analysis_run_id,
        relationship_type=relationship_type,
        source_symbol_id=source_symbol_id,
        target_symbol_id=resolution.symbol.id if resolution.symbol is not None else None,
        target_external_ref=resolution.external_ref if resolution.kind == "external" else None,
        # v1 has no calibrated confidence model: NULL means "not calibrated",
        # never an arbitrary numeric stand-in. The proof of resolution is
        # never this value — it's edge existence plus
        # target_symbol_id/target_external_ref. See graph-construction.md
        # section 7.
        confidence=None,
        # Deterministic AST-based scope/import/binding resolution, no type
        # inference — distinct from a future type-checker-backed resolver.
        # See graph-construction.md section 7.
        resolution_method="ast_resolution",
        evidence_file_id=evidence_file_id,
        evidence_start_line=line,
        evidence_start_column=column,
        evidence_end_line=end_line,
        evidence_end_column=end_column,
    )


def _dedup_key(edge: GraphEdge) -> tuple[object, ...]:
    return (
        edge.relationship_type,
        edge.source_symbol_id,
        edge.target_symbol_id,
        edge.target_external_ref,
        edge.evidence_file_id,
        edge.evidence_start_line,
    )


def run_graph_construction(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> GraphConstructionResult:
    revision = db.get(RepositoryRevision, revision_id)
    if revision is None or revision.repository_id != repository_id:
        raise RevisionNotFoundError(f"no revision {revision_id} for repository {repository_id}")

    run = AnalysisRun(
        repository_id=repository_id,
        revision_id=revision_id,
        analysis_type="graph_build",
        analyzer_name=ANALYZER_NAME,
        analyzer_version=_analyzer_version(),
        status="running",
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()
    _log_event(
        "graph_build_started", repository_id=str(repository_id), revision_id=str(revision_id)
    )

    try:
        result = _build_graph(db, repository_id, revision, run)
        db.commit()
        _log_event(
            "graph_build_completed",
            revision_id=str(revision_id),
            import_edges=result.import_edges,
            call_edges=result.call_edges,
            inherits_edges=result.inherits_edges,
        )
        return result
    except Exception as exc:
        db.rollback()
        run.status = "failed"
        run.error_message = str(exc)[:2000]
        run.finished_at = datetime.now(UTC)
        revision.latest_analysis_status = "failed"
        db.commit()
        _log_event("graph_build_failed", revision_id=str(revision_id), reason=str(exc))
        raise GraphConstructionError(str(exc)) from exc


def _build_graph(
    db: Session, repository_id: uuid.UUID, revision: RepositoryRevision, run: AnalysisRun
) -> GraphConstructionResult:
    # Idempotency (Task 15): delete-and-rebuild, scoped to this revision —
    # same strategy and rationale as app/parsing/service.py's Symbol rebuild.
    db.execute(delete(GraphEdge).where(GraphEdge.revision_id == revision.id))

    symbols = db.execute(select(Symbol).where(Symbol.revision_id == revision.id)).scalars().all()
    tables: RevisionTables = build_revision_tables(list(symbols))

    files = (
        db.execute(
            select(SourceFile)
            .where(SourceFile.revision_id == revision.id)
            .where(SourceFile.parsing_status == "parsed")
            .where(SourceFile.language == "python")
            .order_by(SourceFile.path)
        )
        .scalars()
        .all()
    )

    edges: dict[tuple[object, ...], GraphEdge] = {}
    for source_file in files:
        _process_file(db, repository_id, revision.id, run.id, source_file, tables, edges)

    db.add_all(edges.values())

    revision.latest_analysis_status = "completed"
    run.status = "completed"
    run.finished_at = datetime.now(UTC)

    counts = {"imports": 0, "calls": 0, "inherits": 0}
    for edge in edges.values():
        counts[edge.relationship_type] = counts.get(edge.relationship_type, 0) + 1

    return GraphConstructionResult(
        revision_id=str(revision.id),
        analysis_run_id=str(run.id),
        status="completed",
        import_edges=counts.get("imports", 0),
        call_edges=counts.get("calls", 0),
        inherits_edges=counts.get("inherits", 0),
    )


def _process_file(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    analysis_run_id: uuid.UUID,
    source_file: SourceFile,
    tables: RevisionTables,
    edges: dict[tuple[object, ...], GraphEdge],
) -> None:
    blob = db.get(SourceBlob, source_file.content_hash)
    if blob is None or blob.inline_content is None:
        return

    content = blob.inline_content
    module_name = module_dotted_name(source_file.path)
    module_symbol = tables.symbol_by_qualified_name.get(module_name)
    if module_symbol is None or module_symbol.kind != "module":
        # Should not happen for a file whose parsing_status is "parsed" —
        # defensive only (e.g. an ambiguous module name dropped by
        # build_revision_tables's collision handling).
        return

    parsed = parse_python_source(content, source_file.path)
    if parsed.status != "parsed":
        return

    extraction = extract_graph_candidates(content, module_name)
    if extraction.status != "extracted":
        return

    resolved_imports = resolve_imports_for_file(parsed.imports, source_file.path, tables)
    import_bindings = import_binding_table(resolved_imports)

    def add(edge: GraphEdge) -> None:
        edges[_dedup_key(edge)] = edge  # identical evidence collapses to one edge

    for resolved_import in resolved_imports:
        if resolved_import.resolution.kind == "unresolved":
            continue
        add(
            _new_edge(
                repository_id=repository_id,
                revision_id=revision_id,
                analysis_run_id=analysis_run_id,
                relationship_type="imports",
                source_symbol_id=module_symbol.id,
                resolution=resolved_import.resolution,
                evidence_file_id=source_file.id,
                line=resolved_import.line,
                column=None,
                end_line=None,
                end_column=None,
            )
        )

    for call in extraction.calls:
        caller_symbol = tables.symbol_by_qualified_name.get(call.scope_qualified_name)
        if caller_symbol is None:
            continue
        resolution = resolve_name_chain(
            call.name_chain,
            call.scope_qualified_name,
            module_name,
            tables,
            import_bindings,
            extraction.locally_bound_names,
            extraction.first_parameter_by_scope.get(call.scope_qualified_name),
        )
        if resolution.kind == "internal" and resolution.symbol is not None:
            if resolution.symbol.kind not in _CALLABLE_KINDS:
                # e.g. resolves to a class (instantiation) — not a "calls"
                # edge; see graph-construction.md section 6.
                continue
        elif resolution.kind != "external":
            continue
        add(
            _new_edge(
                repository_id=repository_id,
                revision_id=revision_id,
                analysis_run_id=analysis_run_id,
                relationship_type="calls",
                source_symbol_id=caller_symbol.id,
                resolution=resolution,
                evidence_file_id=source_file.id,
                line=call.line,
                column=call.column,
                end_line=call.end_line,
                end_column=call.end_column,
            )
        )

    for inheritance in extraction.inheritances:
        subclass_symbol = tables.symbol_by_qualified_name.get(inheritance.subclass_qualified_name)
        if subclass_symbol is None:
            continue
        resolution = resolve_name_chain(
            inheritance.name_chain,
            inheritance.scope_qualified_name,
            module_name,
            tables,
            import_bindings,
            extraction.locally_bound_names,
            None,
        )
        if (
            resolution.kind != "internal"
            or resolution.symbol is None
            or resolution.symbol.kind != "class"
        ):
            continue  # inherits never targets external code — see section 6c
        add(
            _new_edge(
                repository_id=repository_id,
                revision_id=revision_id,
                analysis_run_id=analysis_run_id,
                relationship_type="inherits",
                source_symbol_id=subclass_symbol.id,
                resolution=resolution,
                evidence_file_id=source_file.id,
                line=inheritance.line,
                column=inheritance.column,
                end_line=inheritance.end_line,
                end_column=inheritance.end_column,
            )
        )
