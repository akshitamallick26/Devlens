"""Repository Overview v1: a deterministic, read-only, revision-scoped read
model aggregated from facts other subsystems already persisted. See
docs/architecture/repository-overview.md.

Read-only by construction: nothing here adds, flushes, or commits, creates
an AnalysisRun, touches session recency, or calls an AI provider. Every
revision-local query filters on both `repository_id` and `revision_id`;
the only repository-level reads are Repository itself, SecurityFinding's
triage status (joined through this revision's observations), and
Investigation Sessions (reused via app/investigation/service.py).

Every aggregate is SQL-side (COUNT/SUM/GROUP BY); no query loads file
contents, and every row-returning query is LIMITed.
"""

import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import ColumnElement, Select, func, select, union_all
from sqlalchemy.orm import Session, aliased

from app.investigation.service import list_recent_sessions
from app.models import (
    AnalysisRun,
    CodeChunk,
    Embedding,
    GeneratedArtifact,
    GeneratedTest,
    GraphEdge,
    Repository,
    RepositoryRevision,
    SecurityFinding,
    SecurityFindingObservation,
    SourceFile,
    Symbol,
    TestExecution,
)
from app.models.analysis import ANALYSIS_TYPES
from app.models.security import SEVERITIES
from app.overview.models import (
    NOT_RUN,
    AnalysisStatusSummary,
    AutomationSummary,
    CountByKey,
    DirectoryEntry,
    DocumentationSummary,
    GeneratedTestSummary,
    GraphSummary,
    ImportantComponent,
    ImportantComponents,
    ModuleComponent,
    PackageEntry,
    RagSummary,
    RecentSession,
    RecentSessionSummary,
    RepositoryOverview,
    RepositoryStatistics,
    RepositoryStructure,
    RepositorySummary,
    RevisionSummary,
    SecuritySummary,
)
from app.rag.embeddings import ANALYZER_NAME as RAG_EMBEDDER_ANALYZER_NAME
from app.rag.persistence import ANALYZER_NAME as RAG_CHUNKER_ANALYZER_NAME

MAX_TOP_LEVEL_DIRECTORIES = 50
MAX_TOP_LEVEL_PACKAGES = 50
MAX_RANKED_COMPONENTS = 10
MAX_RECENT_SESSIONS = 5

UNKNOWN_KEY = "unknown"

STATIC_LIMITATIONS = (
    "Tracked file count is not persisted by ingestion; stored_file_count counts "
    "the text files ingestion actually stored.",
    "Connected components and cycles are not computed: no existing graph "
    "utility provides them without materializing the full graph.",
    "Automation execution strategy and per-subsystem invalidation decisions are "
    "not persisted on the AnalysisRun; only the recorded counts are reported.",
    "Security scanner-level status is not persisted; only the security_scan "
    "run status is reported.",
    "Test coverage is not reported.",
    "Investigation sessions and security finding triage status are "
    "repository-level, not revision-local.",
)


class OverviewError(Exception):
    pass


class RepositoryNotFoundError(OverviewError):
    pass


class RevisionNotFoundError(OverviewError):
    """No such revision, or the repository has no current revision and none
    was requested."""


class RevisionConflictError(OverviewError):
    """The revision exists but belongs to a different repository."""


def _resolve(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID | None
) -> tuple[Repository, RepositoryRevision]:
    repository = db.get(Repository, repository_id)
    if repository is None:
        raise RepositoryNotFoundError(f"no repository {repository_id}")
    if revision_id is None:
        if repository.current_revision_id is None:
            raise RevisionNotFoundError(f"repository {repository_id} has no current revision")
        revision_id = repository.current_revision_id
    revision = db.get(RepositoryRevision, revision_id)
    if revision is None:
        raise RevisionNotFoundError(f"no revision {revision_id}")
    if revision.repository_id != repository_id:
        raise RevisionConflictError(
            f"revision {revision_id} belongs to a different repository than {repository_id}"
        )
    return repository, revision


def _counts(rows: Iterable[Sequence[Any]], *, by_count: bool) -> tuple[CountByKey, ...]:
    items = [CountByKey(key=key if key is not None else UNKNOWN_KEY, count=n) for key, n in rows]
    if by_count:
        return tuple(sorted(items, key=lambda c: (-c.count, c.key)))
    return tuple(sorted(items, key=lambda c: c.key))


def _file_scope(repository_id: uuid.UUID, revision_id: uuid.UUID) -> list[ColumnElement[bool]]:
    return [SourceFile.repository_id == repository_id, SourceFile.revision_id == revision_id]


def _symbol_scope(repository_id: uuid.UUID, revision_id: uuid.UUID) -> list[ColumnElement[bool]]:
    return [Symbol.repository_id == repository_id, Symbol.revision_id == revision_id]


def _edge_scope(repository_id: uuid.UUID, revision_id: uuid.UUID) -> list[ColumnElement[bool]]:
    return [GraphEdge.repository_id == repository_id, GraphEdge.revision_id == revision_id]


# ---- Statistics ----


def _statistics(
    db: Session, repository_id: uuid.UUID, revision: RepositoryRevision
) -> RepositoryStatistics:
    file_scope = _file_scope(repository_id, revision.id)
    stored, with_language, total_bytes = db.execute(
        select(
            func.count(),
            func.count(SourceFile.language),
            func.coalesce(func.sum(SourceFile.size_bytes), 0),
        ).where(*file_scope)
    ).one()
    by_language = db.execute(
        select(SourceFile.language, func.count()).where(*file_scope).group_by(SourceFile.language)
    ).all()
    by_parsing_status = db.execute(
        select(SourceFile.parsing_status, func.count())
        .where(*file_scope)
        .group_by(SourceFile.parsing_status)
    ).all()
    by_kind = db.execute(
        select(Symbol.kind, func.count())
        .where(*_symbol_scope(repository_id, revision.id))
        .group_by(Symbol.kind)
    ).all()
    symbols_by_kind = _counts(by_kind, by_count=True)
    return RepositoryStatistics(
        stored_file_count=stored,
        recorded_file_count=revision.file_count,
        language_file_count=with_language,
        total_source_bytes=int(total_bytes),
        files_by_language=_counts(by_language, by_count=True),
        files_by_parsing_status=_counts(by_parsing_status, by_count=False),
        symbol_count=sum(c.count for c in symbols_by_kind),
        symbols_by_kind=symbols_by_kind,
    )


# ---- Structure ----


def _structure(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> RepositoryStructure:
    file_scope = _file_scope(repository_id, revision_id)
    in_directory = SourceFile.path.contains("/", autoescape=True)

    root_file_count = db.execute(
        select(func.count()).where(*file_scope).where(~in_directory)
    ).scalar_one()

    # Labelled in a subquery so GROUP BY/ORDER BY reference one column, not a
    # re-rendered split_part(...) expression with fresh bind parameters.
    files = (
        select(
            func.split_part(SourceFile.path, "/", 1).label("top"),
            SourceFile.size_bytes.label("size_bytes"),
        )
        .where(*file_scope)
        .where(in_directory)
        .subquery()
    )
    directory_count = db.execute(select(func.count(func.distinct(files.c.top)))).scalar_one()
    file_count = func.count().label("file_count")
    directory_rows = db.execute(
        select(files.c.top, file_count, func.coalesce(func.sum(files.c.size_bytes), 0))
        .group_by(files.c.top)
        .order_by(file_count.desc(), files.c.top)
        .limit(MAX_TOP_LEVEL_DIRECTORIES)
    ).all()

    names = [row[0] for row in directory_rows]
    symbol_counts: dict[str, int] = {}
    if names:
        symbol_files = (
            select(func.split_part(SourceFile.path, "/", 1).label("top"))
            .select_from(Symbol)
            .join(SourceFile, SourceFile.id == Symbol.file_id)
            .where(*_symbol_scope(repository_id, revision_id))
            .where(*file_scope)
            .where(in_directory)
            .subquery()
        )
        symbol_counts = {
            top: n
            for top, n in db.execute(
                select(symbol_files.c.top, func.count())
                .where(symbol_files.c.top.in_(names))
                .group_by(symbol_files.c.top)
            ).all()
        }

    module_scope = [*_symbol_scope(repository_id, revision_id), Symbol.kind == "module"]
    python_module_count = db.execute(
        select(func.count()).select_from(Symbol).where(*module_scope)
    ).scalar_one()
    # Exactly `<top>/__init__.py` — an equality, not LIKE, since "_" is a
    # LIKE wildcard.
    is_top_level_init = SourceFile.path == func.concat(
        func.split_part(SourceFile.path, "/", 1), "/__init__.py"
    )
    package_rows = db.execute(
        select(Symbol.qualified_name, SourceFile.path)
        .join(SourceFile, SourceFile.id == Symbol.file_id)
        .where(*module_scope)
        .where(*file_scope)
        .where(is_top_level_init)
        .order_by(Symbol.qualified_name, SourceFile.path)
        .limit(MAX_TOP_LEVEL_PACKAGES + 1)
    ).all()

    return RepositoryStructure(
        root_file_count=root_file_count,
        top_level_directory_count=directory_count,
        top_level_directories=tuple(
            DirectoryEntry(
                name=top,
                file_count=n,
                total_bytes=int(size),
                symbol_count=symbol_counts.get(top, 0),
            )
            for top, n, size in directory_rows
        ),
        top_level_directories_truncated=directory_count > len(directory_rows),
        python_module_count=python_module_count,
        top_level_packages=tuple(
            PackageEntry(qualified_name=name, file_path=path)
            for name, path in package_rows[:MAX_TOP_LEVEL_PACKAGES]
        ),
        top_level_packages_truncated=len(package_rows) > MAX_TOP_LEVEL_PACKAGES,
    )


# ---- Graph ----


def _internal_edges(repository_id: uuid.UUID, revision_id: uuid.UUID) -> list[ColumnElement[bool]]:
    """Edges between two distinct Symbols of this revision. Direction is the
    canonical GraphEdge invariant: source = dependent, target = dependency."""
    return [
        *_edge_scope(repository_id, revision_id),
        GraphEdge.target_symbol_id.is_not(None),
        GraphEdge.source_symbol_id != GraphEdge.target_symbol_id,
    ]


def _graph(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, node_count: int
) -> GraphSummary:
    edge_scope = _edge_scope(repository_id, revision_id)
    edge_count, internal_count = db.execute(
        select(func.count(), func.count(GraphEdge.target_symbol_id)).where(*edge_scope)
    ).one()
    by_relationship = db.execute(
        select(GraphEdge.relationship_type, func.count())
        .where(*edge_scope)
        .group_by(GraphEdge.relationship_type)
    ).all()
    internal = _internal_edges(repository_id, revision_id)
    endpoints = union_all(
        select(GraphEdge.source_symbol_id.label("symbol_id")).where(*internal),
        select(GraphEdge.target_symbol_id.label("symbol_id")).where(*internal),
    ).subquery()
    connected = db.execute(select(func.count(func.distinct(endpoints.c.symbol_id)))).scalar_one()
    return GraphSummary(
        edge_count=edge_count,
        internal_edge_count=internal_count,
        external_edge_count=edge_count - internal_count,
        edges_by_relationship=_counts(by_relationship, by_count=True),
        node_count=node_count,
        connected_node_count=connected,
    )


# (at most MAX_RANKED_COMPONENTS entries, truncated). Every ranking query
# fetches MAX_RANKED_COMPONENTS + 1 rows so truncation is known exactly.
type _Ranking[T] = tuple[tuple[T, ...], bool]


def _ranked[T](rows: Sequence[Any], build: Callable[[Any], T]) -> _Ranking[T]:
    return tuple(build(row) for row in rows[:MAX_RANKED_COMPONENTS]), (
        len(rows) > MAX_RANKED_COMPONENTS
    )


def _symbol_rankings(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> tuple[_Ranking[ImportantComponent], _Ranking[ImportantComponent]]:
    internal = _internal_edges(repository_id, revision_id)
    in_degree = (
        select(
            GraphEdge.target_symbol_id.label("symbol_id"),
            func.count(func.distinct(GraphEdge.source_symbol_id)).label("degree"),
        )
        .where(*internal)
        .group_by(GraphEdge.target_symbol_id)
        .subquery()
    )
    out_degree = (
        select(
            GraphEdge.source_symbol_id.label("symbol_id"),
            func.count(func.distinct(GraphEdge.target_symbol_id)).label("degree"),
        )
        .where(*internal)
        .group_by(GraphEdge.source_symbol_id)
        .subquery()
    )
    in_value = func.coalesce(in_degree.c.degree, 0)
    out_value = func.coalesce(out_degree.c.degree, 0)

    def _base() -> Select[uuid.UUID, str, str, str, Any, Any]:
        # Module symbols are ranked at module level below, not here.
        return (
            select(
                Symbol.id, Symbol.qualified_name, Symbol.kind, SourceFile.path, in_value, out_value
            )
            .join(SourceFile, SourceFile.id == Symbol.file_id)
            .outerjoin(in_degree, in_degree.c.symbol_id == Symbol.id)
            .outerjoin(out_degree, out_degree.c.symbol_id == Symbol.id)
            .where(*_symbol_scope(repository_id, revision_id))
            .where(*_file_scope(repository_id, revision_id))
            .where(Symbol.kind != "module")
        )

    def _build(row: Any) -> ImportantComponent:
        return ImportantComponent(
            symbol_id=row[0],
            qualified_name=row[1],
            kind=row[2],
            file_path=row[3],
            in_degree=row[4],
            out_degree=row[5],
        )

    by_in = db.execute(
        _base()
        .where(in_degree.c.degree.is_not(None))
        .order_by(in_value.desc(), Symbol.qualified_name, Symbol.id)
        .limit(MAX_RANKED_COMPONENTS + 1)
    ).all()
    by_out = db.execute(
        _base()
        .where(out_degree.c.degree.is_not(None))
        .order_by(out_value.desc(), Symbol.qualified_name, Symbol.id)
        .limit(MAX_RANKED_COMPONENTS + 1)
    ).all()
    return _ranked(by_in, _build), _ranked(by_out, _build)


def _module_rankings(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> tuple[_Ranking[ModuleComponent], _Ranking[ModuleComponent]]:
    source_symbol = aliased(Symbol)
    target_symbol = aliased(Symbol)
    # Distinct (dependent file, dependency file) pairs across files.
    file_pairs = (
        select(
            source_symbol.file_id.label("source_file_id"),
            target_symbol.file_id.label("target_file_id"),
        )
        .select_from(GraphEdge)
        .join(source_symbol, source_symbol.id == GraphEdge.source_symbol_id)
        .join(target_symbol, target_symbol.id == GraphEdge.target_symbol_id)
        .where(*_internal_edges(repository_id, revision_id))
        .where(source_symbol.repository_id == repository_id)
        .where(source_symbol.revision_id == revision_id)
        .where(target_symbol.repository_id == repository_id)
        .where(target_symbol.revision_id == revision_id)
        .where(source_symbol.file_id != target_symbol.file_id)
        .distinct()
        .cte("file_pairs")
    )
    in_degree = (
        select(file_pairs.c.target_file_id.label("file_id"), func.count().label("degree"))
        .group_by(file_pairs.c.target_file_id)
        .subquery()
    )
    out_degree = (
        select(file_pairs.c.source_file_id.label("file_id"), func.count().label("degree"))
        .group_by(file_pairs.c.source_file_id)
        .subquery()
    )
    in_value = func.coalesce(in_degree.c.degree, 0)
    out_value = func.coalesce(out_degree.c.degree, 0)
    base = (
        select(SourceFile.id, SourceFile.path, in_value, out_value)
        .outerjoin(in_degree, in_degree.c.file_id == SourceFile.id)
        .outerjoin(out_degree, out_degree.c.file_id == SourceFile.id)
        .where(*_file_scope(repository_id, revision_id))
    )
    by_in = db.execute(
        base.where(in_degree.c.degree.is_not(None))
        .order_by(in_value.desc(), SourceFile.path, SourceFile.id)
        .limit(MAX_RANKED_COMPONENTS + 1)
    ).all()
    by_out = db.execute(
        base.where(out_degree.c.degree.is_not(None))
        .order_by(out_value.desc(), SourceFile.path, SourceFile.id)
        .limit(MAX_RANKED_COMPONENTS + 1)
    ).all()

    # Module names come from the parser's own kind="module" symbols — one
    # bounded lookup for at most 2 * (MAX_RANKED_COMPONENTS + 1) files.
    file_ids = {row[0] for row in (*by_in, *by_out)}
    module_names: dict[uuid.UUID, str] = {}
    if file_ids:
        for file_id, name in db.execute(
            select(Symbol.file_id, Symbol.qualified_name)
            .where(*_symbol_scope(repository_id, revision_id))
            .where(Symbol.kind == "module")
            .where(Symbol.file_id.in_(file_ids))
            .order_by(Symbol.file_id, Symbol.qualified_name)
        ).all():
            module_names.setdefault(file_id, name)

    def _build(row: Any) -> ModuleComponent:
        return ModuleComponent(
            file_id=row[0],
            file_path=row[1],
            module_name=module_names.get(row[0]),
            in_degree=row[2],
            out_degree=row[3],
        )

    return _ranked(by_in, _build), _ranked(by_out, _build)


def _important_components(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> ImportantComponents:
    (sym_in, sym_in_t), (sym_out, sym_out_t) = _symbol_rankings(db, repository_id, revision_id)
    (mod_in, mod_in_t), (mod_out, mod_out_t) = _module_rankings(db, repository_id, revision_id)
    return ImportantComponents(
        most_depended_upon_symbols=sym_in,
        most_depended_upon_symbols_truncated=sym_in_t,
        most_dependency_heavy_symbols=sym_out,
        most_dependency_heavy_symbols_truncated=sym_out_t,
        most_depended_upon_modules=mod_in,
        most_depended_upon_modules_truncated=mod_in_t,
        most_dependency_heavy_modules=mod_out,
        most_dependency_heavy_modules_truncated=mod_out_t,
    )


# ---- Analysis statuses ----

_EPOCH = datetime.min.replace(tzinfo=UTC)


def _recency(run: AnalysisRun) -> tuple[bool, datetime, datetime, uuid.UUID]:
    """Latest = most recent started_at (runs never started sort last), then
    created_at, then id — total and deterministic. started_at first because
    automation runs are upserted in place (created_at stays at first insert)."""
    return (run.started_at is not None, run.started_at or _EPOCH, run.created_at, run.id)


def latest_runs(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> dict[tuple[str, str], AnalysisRun]:
    """Latest run per (analysis_type, analyzer_name) for this revision, in one
    query. Row count is bounded by the number of distinct analyzers."""
    rank = (
        func.row_number()
        .over(
            partition_by=(AnalysisRun.analysis_type, AnalysisRun.analyzer_name),
            order_by=(
                AnalysisRun.started_at.desc().nulls_last(),
                AnalysisRun.created_at.desc(),
                AnalysisRun.id.desc(),
            ),
        )
        .label("rank")
    )
    ranked = (
        select(AnalysisRun.id.label("run_id"), rank)
        .where(AnalysisRun.repository_id == repository_id)
        .where(AnalysisRun.revision_id == revision_id)
        .subquery()
    )
    runs = (
        db.execute(
            select(AnalysisRun)
            .join(ranked, ranked.c.run_id == AnalysisRun.id)
            .where(ranked.c.rank == 1)
            .order_by(AnalysisRun.analysis_type, AnalysisRun.analyzer_name)
        )
        .scalars()
        .all()
    )
    return {(run.analysis_type, run.analyzer_name): run for run in runs}


def _status(analysis_type: str, run: AnalysisRun | None) -> AnalysisStatusSummary:
    if run is None:
        return AnalysisStatusSummary(
            analysis_type=analysis_type,
            status=NOT_RUN,
            analysis_run_id=None,
            analyzer_name=None,
            started_at=None,
            finished_at=None,
            error_message=None,
        )
    return AnalysisStatusSummary(
        analysis_type=analysis_type,
        status=run.status,
        analysis_run_id=run.id,
        analyzer_name=run.analyzer_name,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error_message=run.error_message,
    )


def _latest_of_type(
    latest: dict[tuple[str, str], AnalysisRun], analysis_type: str
) -> AnalysisRun | None:
    candidates = [run for (kind, _), run in latest.items() if kind == analysis_type]
    return max(candidates, key=_recency) if candidates else None


# ---- Subsystem summaries ----


def _security(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, latest_run: AnalysisRun | None
) -> SecuritySummary:
    findings_run_id = db.execute(
        select(AnalysisRun.id)
        .where(AnalysisRun.repository_id == repository_id)
        .where(AnalysisRun.revision_id == revision_id)
        .where(AnalysisRun.analysis_type == "security_scan")
        .where(AnalysisRun.status == "completed")
        .order_by(
            AnalysisRun.started_at.desc().nulls_last(),
            AnalysisRun.created_at.desc(),
            AnalysisRun.id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()

    finding_count = 0
    by_severity: list[CountByKey] = []
    by_status: tuple[CountByKey, ...] = ()
    if findings_run_id is not None:
        # Same scoping as app/security/service.py::_load_findings_for_run:
        # findings observed by this run in this revision, never every
        # finding ever recorded for the repository.
        observed = (
            select(SecurityFindingObservation.finding_id)
            .where(SecurityFindingObservation.repository_id == repository_id)
            .where(SecurityFindingObservation.revision_id == revision_id)
            .where(SecurityFindingObservation.analysis_run_id == findings_run_id)
            .distinct()
            .subquery()
        )
        findings = (
            select(SecurityFinding.severity, SecurityFinding.status)
            .join(observed, observed.c.finding_id == SecurityFinding.id)
            .where(SecurityFinding.repository_id == repository_id)
            .subquery()
        )
        severity_counts = {
            key if key is not None else UNKNOWN_KEY: n
            for key, n in db.execute(
                select(findings.c.severity, func.count()).group_by(findings.c.severity)
            ).all()
        }
        order = [*SEVERITIES, UNKNOWN_KEY]
        by_severity = [
            CountByKey(key=key, count=severity_counts[key])
            for key in order
            if key in severity_counts
        ]
        finding_count = sum(severity_counts.values())
        by_status = _counts(
            db.execute(select(findings.c.status, func.count()).group_by(findings.c.status)).all(),
            by_count=False,
        )

    return SecuritySummary(
        latest_run=_status("security_scan", latest_run),
        findings_run_id=findings_run_id,
        finding_count=finding_count,
        findings_by_severity=tuple(by_severity),
        findings_by_current_triage_status=by_status,
    )


def _testing(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, latest_run: AnalysisRun | None
) -> GeneratedTestSummary:
    tests_by_status = _counts(
        db.execute(
            select(GeneratedTest.status, func.count())
            .where(GeneratedTest.repository_id == repository_id)
            .where(GeneratedTest.revision_id == revision_id)
            .group_by(GeneratedTest.status)
        ).all(),
        by_count=False,
    )
    executions_by_status = _counts(
        db.execute(
            select(TestExecution.status, func.count())
            .where(TestExecution.repository_id == repository_id)
            .where(TestExecution.revision_id == revision_id)
            .group_by(TestExecution.status)
        ).all(),
        by_count=False,
    )
    return GeneratedTestSummary(
        latest_run=_status("test_generation", latest_run),
        generated_test_count=sum(c.count for c in tests_by_status),
        generated_tests_by_status=tests_by_status,
        execution_count=sum(c.count for c in executions_by_status),
        executions_by_status=executions_by_status,
    )


def _documentation(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, latest_run: AnalysisRun | None
) -> DocumentationSummary:
    scope = [
        GeneratedArtifact.repository_id == repository_id,
        GeneratedArtifact.revision_id == revision_id,
        GeneratedArtifact.kind == "documentation",
    ]
    by_type = _counts(
        db.execute(
            select(GeneratedArtifact.artifact_type, func.count())
            .where(*scope)
            .group_by(GeneratedArtifact.artifact_type)
        ).all(),
        by_count=False,
    )
    by_method = _counts(
        db.execute(
            select(GeneratedArtifact.generation_method, func.count())
            .where(*scope)
            .group_by(GeneratedArtifact.generation_method)
        ).all(),
        by_count=False,
    )
    latest_updated_at = db.execute(
        select(func.max(GeneratedArtifact.updated_at)).where(*scope)
    ).scalar_one()
    return DocumentationSummary(
        latest_run=_status("documentation_generation", latest_run),
        artifact_count=sum(c.count for c in by_type),
        artifacts_by_type=by_type,
        artifacts_by_generation_method=by_method,
        latest_artifact_updated_at=latest_updated_at,
    )


def _rag(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    latest: dict[tuple[str, str], AnalysisRun],
) -> RagSummary:
    chunk_scope = [CodeChunk.repository_id == repository_id, CodeChunk.revision_id == revision_id]
    chunk_count, indexed_files = db.execute(
        select(func.count(), func.count(func.distinct(CodeChunk.file_id))).where(*chunk_scope)
    ).one()
    embedding_count = db.execute(
        select(func.count())
        .select_from(Embedding)
        .join(CodeChunk, CodeChunk.id == Embedding.chunk_id)
        .where(*chunk_scope)
    ).scalar_one()
    return RagSummary(
        chunking_run=_status(
            "rag_indexing", latest.get(("rag_indexing", RAG_CHUNKER_ANALYZER_NAME))
        ),
        embedding_run=_status(
            "rag_indexing", latest.get(("rag_indexing", RAG_EMBEDDER_ANALYZER_NAME))
        ),
        chunk_count=chunk_count,
        indexed_file_count=indexed_files,
        embedding_count=embedding_count,
    )


def _config_int(config: dict[str, Any] | None, key: str) -> int | None:
    value = (config or {}).get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _automation(run: AnalysisRun | None) -> AutomationSummary:
    config = run.config if run is not None else None
    limitations = (config or {}).get("limitations")
    return AutomationSummary(
        latest_run=_status("automation_analysis", run),
        parent_revision_id=run.parent_revision_id if run is not None else None,
        changed_file_count=_config_int(config, "changed_file_count"),
        changed_symbol_count=_config_int(config, "changed_symbol_count"),
        impacted_symbol_count=_config_int(config, "impacted_symbol_count"),
        affected_file_count=_config_int(config, "affected_file_count"),
        limitation_count=len(limitations) if isinstance(limitations, list) else None,
    )


def _sessions(db: Session, repository_id: uuid.UUID) -> RecentSessionSummary:
    # Reuses the existing read-only listing (one count + one aggregated
    # query, no N+1, never touches last_active_at or message bodies).
    page = list_recent_sessions(db, repository_id, limit=MAX_RECENT_SESSIONS)
    return RecentSessionSummary(
        session_count=page.total_count,
        recent_sessions=tuple(
            RecentSession(
                session_id=s.id,
                title=s.title,
                status=s.status,
                last_active_at=s.last_active_at,
                message_count=s.message_count,
            )
            for s in page.sessions
        ),
    )


# ---- Entry point ----


def get_repository_overview(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID | None = None
) -> RepositoryOverview:
    """Read-only. `revision_id=None` means Repository.current_revision_id;
    every revision-local fact comes from that single resolved revision."""
    repository, revision = _resolve(db, repository_id, revision_id)
    rev_id = revision.id

    statistics = _statistics(db, repository_id, revision)
    latest = latest_runs(db, repository_id, rev_id)
    latest_by_type = {kind: _latest_of_type(latest, kind) for kind in ANALYSIS_TYPES}

    return RepositoryOverview(
        repository=RepositorySummary(
            repository_id=repository.id,
            owner=repository.owner,
            name=repository.name,
            url=repository.url,
            default_branch=repository.default_branch,
            status=repository.status,
            current_revision_id=repository.current_revision_id,
        ),
        revision=RevisionSummary(
            revision_id=rev_id,
            commit_sha=revision.commit_sha,
            parent_sha=revision.parent_sha,
            branch_ref=revision.branch_ref,
            committed_at=revision.committed_at,
            discovered_at=revision.discovered_at,
            is_current=repository.current_revision_id == rev_id,
            latest_analysis_status=revision.latest_analysis_status,
        ),
        statistics=statistics,
        structure=_structure(db, repository_id, rev_id),
        graph=_graph(db, repository_id, rev_id, statistics.symbol_count),
        important_components=_important_components(db, repository_id, rev_id),
        analysis_statuses=tuple(_status(kind, latest_by_type[kind]) for kind in ANALYSIS_TYPES),
        security=_security(db, repository_id, rev_id, latest_by_type["security_scan"]),
        testing=_testing(db, repository_id, rev_id, latest_by_type["test_generation"]),
        documentation=_documentation(
            db, repository_id, rev_id, latest_by_type["documentation_generation"]
        ),
        rag=_rag(db, repository_id, rev_id, latest),
        automation=_automation(latest_by_type["automation_analysis"]),
        sessions=_sessions(db, repository_id),
        limitations=STATIC_LIMITATIONS,
    )
