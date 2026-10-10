"""Read-side code intelligence (Product Integration v1): bounded, revision-
scoped reads over rows the ingestion/parsing/graph/security/testing
subsystems already persisted. Read-only: nothing here adds, flushes or
commits, and nothing creates an AnalysisRun.

Every query filters on both repository_id and revision_id (or on a
revision already verified to belong to the repository). Every list is
LIMITed. Graph traversal is a bounded breadth-first walk with one grouped
SQL query per hop and direction; the graph is never loaded whole.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.orm import Session

from app.codeintel.models import (
    DirectoryEntry,
    DirectoryListing,
    ExecutionEntry,
    FileDetail,
    FileEntry,
    FindingEntry,
    FindingPage,
    GeneratedTestEntry,
    GeneratedTestPage,
    GraphEdgeView,
    GraphNeighborhood,
    GraphNode,
    RevisionEntry,
    RevisionPage,
    SourceRange,
    SymbolDetail,
    SymbolEntry,
    SymbolSearchResult,
)
from app.models import (
    AnalysisRun,
    GeneratedTest,
    GraphEdge,
    Repository,
    RepositoryRevision,
    SecurityFinding,
    SecurityFindingObservation,
    SourceBlob,
    SourceFile,
    Symbol,
    TestExecution,
)
from app.models.security import SEVERITIES

MAX_PAGE_SIZE = 200
MAX_DIRECTORY_ENTRIES = 500
MAX_FILE_SYMBOLS = 1000
MAX_SOURCE_LINES = 1000
MAX_SYMBOL_SEARCH = 50
MAX_GRAPH_DEPTH = 4
MAX_GRAPH_NODES = 200
MAX_EDGE_ROWS_PER_STEP = 2000
MAX_EXTERNAL_REFERENCES = 20
MAX_PATH_LENGTH = 1024

DIRECTIONS = ("dependents", "dependencies", "both")


class CodeIntelError(Exception):
    pass


class RepositoryNotFoundError(CodeIntelError):
    pass


class RevisionNotFoundError(CodeIntelError):
    pass


class RevisionConflictError(CodeIntelError):
    """The revision exists but belongs to another repository."""


class NotFoundError(CodeIntelError):
    """A file/symbol does not exist in the requested repository + revision."""


class InvalidRequestError(CodeIntelError):
    pass


def _bounded(limit: int, maximum: int) -> int:
    return max(1, min(limit, maximum))


def _resolve(db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID) -> RepositoryRevision:
    if db.get(Repository, repository_id) is None:
        raise RepositoryNotFoundError(f"no repository {repository_id}")
    revision = db.get(RepositoryRevision, revision_id)
    if revision is None:
        raise RevisionNotFoundError(f"no revision {revision_id}")
    if revision.repository_id != repository_id:
        raise RevisionConflictError(
            f"revision {revision_id} belongs to a different repository than {repository_id}"
        )
    return revision


def _file_scope(repository_id: uuid.UUID, revision_id: uuid.UUID) -> list[ColumnElement[bool]]:
    return [SourceFile.repository_id == repository_id, SourceFile.revision_id == revision_id]


def _symbol_scope(repository_id: uuid.UUID, revision_id: uuid.UUID) -> list[ColumnElement[bool]]:
    return [Symbol.repository_id == repository_id, Symbol.revision_id == revision_id]


def _file_entry(row: SourceFile) -> FileEntry:
    return FileEntry(
        file_id=row.id,
        path=row.path,
        language=row.language,
        size_bytes=row.size_bytes,
        line_count=row.line_count,
        parsing_status=row.parsing_status,
    )


def _symbol_entry(symbol: Symbol, file_path: str) -> SymbolEntry:
    return SymbolEntry(
        symbol_id=symbol.id,
        name=symbol.name,
        qualified_name=symbol.qualified_name,
        kind=symbol.kind,
        file_id=symbol.file_id,
        file_path=file_path,
        start_line=symbol.start_line,
        end_line=symbol.end_line,
        parent_symbol_id=symbol.parent_symbol_id,
        signature=symbol.signature,
    )


def _get_file(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, file_id: uuid.UUID
) -> SourceFile:
    row = db.execute(
        select(SourceFile)
        .where(SourceFile.id == file_id)
        .where(*_file_scope(repository_id, revision_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(f"no file {file_id} in revision {revision_id}")
    return row


# ---- Revisions ----


def list_revisions(
    db: Session, repository_id: uuid.UUID, limit: int = 50, offset: int = 0
) -> RevisionPage:
    repository = db.get(Repository, repository_id)
    if repository is None:
        raise RepositoryNotFoundError(f"no repository {repository_id}")
    limit, offset = _bounded(limit, MAX_PAGE_SIZE), max(0, offset)
    total = db.execute(
        select(func.count()).where(RepositoryRevision.repository_id == repository_id)
    ).scalar_one()
    rows = (
        db.execute(
            select(RepositoryRevision)
            .where(RepositoryRevision.repository_id == repository_id)
            .order_by(
                RepositoryRevision.committed_at.desc(),
                RepositoryRevision.discovered_at.desc(),
                RepositoryRevision.id,
            )
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return RevisionPage(
        revisions=tuple(
            RevisionEntry(
                revision_id=r.id,
                commit_sha=r.commit_sha,
                parent_sha=r.parent_sha,
                branch_ref=r.branch_ref,
                committed_at=r.committed_at,
                discovered_at=r.discovered_at,
                latest_analysis_status=r.latest_analysis_status,
                file_count=r.file_count,
                is_current=repository.current_revision_id == r.id,
            )
            for r in rows
        ),
        total_count=total,
        limit=limit,
        offset=offset,
    )


# ---- Files ----


def normalize_directory(directory: str) -> str:
    value = directory.strip().strip("/")
    if len(value) > MAX_PATH_LENGTH or "\x00" in value:
        raise InvalidRequestError("invalid directory")
    if any(part in ("", ".", "..") for part in value.split("/")) and value:
        raise InvalidRequestError("directory must be a plain repository-relative path")
    return value


def browse_directory(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    directory: str = "",
    limit: int = 100,
    offset: int = 0,
) -> DirectoryListing:
    """Immediate children of `directory`: its subdirectories (with the number
    of files below each) and a page of the files directly inside it. A pure
    database filter on stored paths; the filesystem is never touched."""
    _resolve(db, repository_id, revision_id)
    directory = normalize_directory(directory)
    limit, offset = _bounded(limit, MAX_PAGE_SIZE), max(0, offset)
    prefix = f"{directory}/" if directory else ""
    scope = _file_scope(repository_id, revision_id)
    under = SourceFile.path.startswith(prefix, autoescape=True) if prefix else None

    # Path remainder after the prefix, labelled in a subquery so GROUP BY
    # references a column rather than a re-rendered expression.
    rest = (
        select(
            func.substr(SourceFile.path, len(prefix) + 1).label("rest"),
        )
        .where(*scope)
        .where(*([under] if under is not None else []))
        .subquery()
    )
    child = func.split_part(rest.c.rest, "/", 1)
    in_subdirectory = func.strpos(rest.c.rest, "/") > 0
    directory_rows = db.execute(
        select(child.label("name"), func.count().label("files"))
        .select_from(rest)
        .where(in_subdirectory)
        .group_by(child)
        .order_by(child)
        .limit(MAX_DIRECTORY_ENTRIES + 1)
    ).all()

    direct = [
        *scope,
        *([under] if under is not None else []),
        func.strpos(func.substr(SourceFile.path, len(prefix) + 1), "/") == 0,
    ]
    total_files = db.execute(
        select(func.count()).select_from(SourceFile).where(*direct)
    ).scalar_one()
    files = (
        db.execute(
            select(SourceFile)
            .where(*direct)
            .order_by(SourceFile.path, SourceFile.id)
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return DirectoryListing(
        directory=directory,
        directories=tuple(
            DirectoryEntry(name=name, path=f"{prefix}{name}", file_count=count)
            for name, count in directory_rows[:MAX_DIRECTORY_ENTRIES]
        ),
        directories_truncated=len(directory_rows) > MAX_DIRECTORY_ENTRIES,
        files=tuple(_file_entry(f) for f in files),
        total_files=total_files,
        limit=limit,
        offset=offset,
    )


def get_file_detail(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, file_id: uuid.UUID
) -> FileDetail:
    _resolve(db, repository_id, revision_id)
    source_file = _get_file(db, repository_id, revision_id, file_id)
    symbols = (
        db.execute(
            select(Symbol)
            .where(*_symbol_scope(repository_id, revision_id))
            .where(Symbol.file_id == file_id)
            .order_by(Symbol.start_line, Symbol.qualified_name, Symbol.id)
            .limit(MAX_FILE_SYMBOLS + 1)
        )
        .scalars()
        .all()
    )
    inline = db.execute(
        select(SourceBlob.inline_content.is_not(None)).where(
            SourceBlob.content_hash == source_file.content_hash
        )
    ).scalar_one_or_none()
    return FileDetail(
        file=_file_entry(source_file),
        revision_id=revision_id,
        symbols=tuple(_symbol_entry(s, source_file.path) for s in symbols[:MAX_FILE_SYMBOLS]),
        symbols_truncated=len(symbols) > MAX_FILE_SYMBOLS,
        source_available=bool(inline),
    )


def get_source_range(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    file_id: uuid.UUID,
    start_line: int = 1,
    end_line: int | None = None,
) -> SourceRange:
    """At most MAX_SOURCE_LINES lines of one stored file. Content comes from
    the content-addressed SourceBlob; storage_key-backed files raise
    NotFoundError rather than returning invented text."""
    _resolve(db, repository_id, revision_id)
    if start_line < 1:
        raise InvalidRequestError("start_line must be >= 1")
    if end_line is not None and end_line < start_line:
        raise InvalidRequestError("end_line must be >= start_line")
    source_file = _get_file(db, repository_id, revision_id, file_id)
    content = db.execute(
        select(SourceBlob.inline_content).where(SourceBlob.content_hash == source_file.content_hash)
    ).scalar_one_or_none()
    if content is None:
        raise NotFoundError(f"source for file {file_id} is not stored inline and cannot be served")
    all_lines = content.splitlines()
    requested_end = end_line if end_line is not None else start_line + MAX_SOURCE_LINES - 1
    capped_end = min(requested_end, start_line + MAX_SOURCE_LINES - 1)
    selected = all_lines[start_line - 1 : capped_end]
    return SourceRange(
        file_id=file_id,
        path=source_file.path,
        start_line=start_line,
        end_line=start_line + len(selected) - 1,
        total_lines=len(all_lines),
        lines=tuple(selected),
        truncated=requested_end > capped_end and capped_end < len(all_lines),
    )


# ---- Symbols ----


def search_symbols(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    query: str,
    limit: int = 20,
) -> SymbolSearchResult:
    """Case-insensitive substring match on qualified_name (LIKE wildcards in
    the query are escaped). Exact matches first, then shorter names."""
    _resolve(db, repository_id, revision_id)
    text = query.strip()
    if not text or len(text) > 300:
        raise InvalidRequestError("q must be 1 to 300 characters")
    limit = _bounded(limit, MAX_SYMBOL_SEARCH)
    exact = case((func.lower(Symbol.qualified_name) == text.lower(), 0), else_=1)
    rows = db.execute(
        select(Symbol, SourceFile.path)
        .join(SourceFile, SourceFile.id == Symbol.file_id)
        .where(*_symbol_scope(repository_id, revision_id))
        .where(*_file_scope(repository_id, revision_id))
        .where(Symbol.qualified_name.icontains(text, autoescape=True))
        .order_by(exact, func.length(Symbol.qualified_name), Symbol.qualified_name, Symbol.id)
        .limit(limit + 1)
    ).all()
    return SymbolSearchResult(
        symbols=tuple(_symbol_entry(s, path) for s, path in rows[:limit]),
        truncated=len(rows) > limit,
    )


def _internal(repository_id: uuid.UUID, revision_id: uuid.UUID) -> list[ColumnElement[bool]]:
    return [
        GraphEdge.repository_id == repository_id,
        GraphEdge.revision_id == revision_id,
        GraphEdge.target_symbol_id.is_not(None),
        GraphEdge.source_symbol_id != GraphEdge.target_symbol_id,
    ]


def get_symbol_detail(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, symbol_id: uuid.UUID
) -> SymbolDetail:
    _resolve(db, repository_id, revision_id)
    row = db.execute(
        select(Symbol, SourceFile.path)
        .join(SourceFile, SourceFile.id == Symbol.file_id)
        .where(Symbol.id == symbol_id)
        .where(*_symbol_scope(repository_id, revision_id))
    ).one_or_none()
    if row is None:
        raise NotFoundError(f"no symbol {symbol_id} in revision {revision_id}")
    symbol, path = row

    parent: SymbolEntry | None = None
    if symbol.parent_symbol_id is not None:
        parent_row = db.execute(
            select(Symbol, SourceFile.path)
            .join(SourceFile, SourceFile.id == Symbol.file_id)
            .where(Symbol.id == symbol.parent_symbol_id)
            .where(*_symbol_scope(repository_id, revision_id))
        ).one_or_none()
        if parent_row is not None:
            parent = _symbol_entry(parent_row[0], parent_row[1])

    child_count = db.execute(
        select(func.count())
        .select_from(Symbol)
        .where(*_symbol_scope(repository_id, revision_id))
        .where(Symbol.parent_symbol_id == symbol_id)
    ).scalar_one()
    internal = _internal(repository_id, revision_id)
    dependents, dependencies = db.execute(
        select(
            func.count(func.distinct(GraphEdge.source_symbol_id)).filter(
                GraphEdge.target_symbol_id == symbol_id
            ),
            func.count(func.distinct(GraphEdge.target_symbol_id)).filter(
                GraphEdge.source_symbol_id == symbol_id
            ),
        )
        .where(*internal)
        .where(
            (GraphEdge.target_symbol_id == symbol_id) | (GraphEdge.source_symbol_id == symbol_id)
        )
    ).one()
    external = db.execute(
        select(func.count())
        .select_from(GraphEdge)
        .where(GraphEdge.repository_id == repository_id)
        .where(GraphEdge.revision_id == revision_id)
        .where(GraphEdge.source_symbol_id == symbol_id)
        .where(GraphEdge.target_symbol_id.is_(None))
    ).scalar_one()
    return SymbolDetail(
        symbol=_symbol_entry(symbol, path),
        revision_id=revision_id,
        parent=parent,
        child_count=child_count,
        dependent_count=dependents,
        dependency_count=dependencies,
        external_reference_count=external,
    )


# ---- Graph neighborhood ----


def _reached_via(
    node: uuid.UUID, focus: uuid.UUID, walks_by_node: dict[uuid.UUID, set[str]]
) -> str:
    if node == focus:
        return "focus"
    walks = walks_by_node.get(node, set())
    return "both" if len(walks) > 1 else next(iter(walks), "dependents")


def graph_neighborhood(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    symbol_id: uuid.UUID,
    direction: str = "both",
    depth: int = 2,
    limit: int = 100,
) -> GraphNeighborhood:
    """Bounded breadth-first neighbourhood of one symbol over internal edges.

    `dependents` follows edges backwards (who depends on the focus),
    `dependencies` forwards (what the focus depends on), `both` walks each
    direction separately from the focus. Returned edges keep the stored
    orientation: source = dependent, target = dependency. Cycles terminate
    via the visited set; a node keeps its shortest hop count. When the node
    limit or a per-hop edge cap is reached, traversal stops adding nodes and
    `truncated` is set. Edges are returned only between returned nodes."""
    _resolve(db, repository_id, revision_id)
    if direction not in DIRECTIONS:
        raise InvalidRequestError(f"direction must be one of {DIRECTIONS}")
    if not 1 <= depth <= MAX_GRAPH_DEPTH:
        raise InvalidRequestError(f"depth must be between 1 and {MAX_GRAPH_DEPTH}")
    if not 1 <= limit <= MAX_GRAPH_NODES:
        raise InvalidRequestError(f"limit must be between 1 and {MAX_GRAPH_NODES}")
    focus = db.execute(
        select(Symbol.id)
        .where(Symbol.id == symbol_id)
        .where(*_symbol_scope(repository_id, revision_id))
    ).scalar_one_or_none()
    if focus is None:
        raise NotFoundError(f"no symbol {symbol_id} in revision {revision_id}")

    internal = _internal(repository_id, revision_id)
    depth_by_node: dict[uuid.UUID, int] = {symbol_id: 0}
    walks_by_node: dict[uuid.UUID, set[str]] = {}
    edges: dict[tuple[uuid.UUID, uuid.UUID, str], int] = {}
    truncated = False
    max_depth_reached = 0

    walks = []
    if direction in ("dependents", "both"):
        walks.append("dependents")
    if direction in ("dependencies", "both"):
        walks.append("dependencies")

    for walk in walks:
        frontier = [symbol_id]
        seen = {symbol_id}
        for hop in range(1, depth + 1):
            if not frontier or truncated:
                break
            anchor = (
                GraphEdge.target_symbol_id if walk == "dependents" else GraphEdge.source_symbol_id
            )
            rows = db.execute(
                select(
                    GraphEdge.source_symbol_id,
                    GraphEdge.target_symbol_id,
                    GraphEdge.relationship_type,
                    func.count(),
                )
                .where(*internal)
                .where(anchor.in_(frontier))
                .group_by(
                    GraphEdge.source_symbol_id,
                    GraphEdge.target_symbol_id,
                    GraphEdge.relationship_type,
                )
                .order_by(
                    GraphEdge.relationship_type,
                    GraphEdge.source_symbol_id,
                    GraphEdge.target_symbol_id,
                )
                .limit(MAX_EDGE_ROWS_PER_STEP + 1)
            ).all()
            if len(rows) > MAX_EDGE_ROWS_PER_STEP:
                truncated = True
                rows = rows[:MAX_EDGE_ROWS_PER_STEP]
            next_frontier: list[uuid.UUID] = []
            for source, target, relationship, occurrences in rows:
                assert target is not None
                reached = source if walk == "dependents" else target
                if reached not in depth_by_node:
                    if len(depth_by_node) >= limit:
                        truncated = True
                        continue
                    depth_by_node[reached] = hop
                else:
                    # Reached by both walks: keep the shorter hop count.
                    depth_by_node[reached] = min(depth_by_node[reached], hop)
                max_depth_reached = max(max_depth_reached, depth_by_node[reached])
                if reached != symbol_id:
                    walks_by_node.setdefault(reached, set()).add(walk)
                if reached not in seen:
                    seen.add(reached)
                    next_frontier.append(reached)
                key = (source, target, relationship)
                edges[key] = max(edges.get(key, 0), occurrences)
            frontier = next_frontier

    node_rows = db.execute(
        select(Symbol, SourceFile.path)
        .join(SourceFile, SourceFile.id == Symbol.file_id)
        .where(*_symbol_scope(repository_id, revision_id))
        .where(Symbol.id.in_(depth_by_node))
    ).all()
    nodes = sorted(
        (
            GraphNode(
                symbol_id=s.id,
                file_id=s.file_id,
                file_path=path,
                qualified_name=s.qualified_name,
                kind=s.kind,
                depth=depth_by_node[s.id],
                reached_via=_reached_via(s.id, symbol_id, walks_by_node),
            )
            for s, path in node_rows
        ),
        key=lambda n: (n.depth, n.qualified_name, str(n.symbol_id)),
    )
    node_ids = {n.symbol_id for n in nodes}
    edge_views = sorted(
        (
            GraphEdgeView(source=s, target=t, relationship_type=r, occurrences=n)
            for (s, t, r), n in edges.items()
            if s in node_ids and t in node_ids
        ),
        key=lambda e: (e.relationship_type, str(e.source), str(e.target)),
    )
    external = (
        db.execute(
            select(GraphEdge.target_external_ref)
            .where(GraphEdge.repository_id == repository_id)
            .where(GraphEdge.revision_id == revision_id)
            .where(GraphEdge.source_symbol_id == symbol_id)
            .where(GraphEdge.target_symbol_id.is_(None))
            .distinct()
            .order_by(GraphEdge.target_external_ref)
            .limit(MAX_EXTERNAL_REFERENCES)
        )
        .scalars()
        .all()
    )
    return GraphNeighborhood(
        revision_id=revision_id,
        focus_symbol_id=symbol_id,
        direction=direction,
        depth=depth,
        node_limit=limit,
        nodes=tuple(nodes),
        edges=tuple(edge_views),
        truncated=truncated,
        max_depth_reached=max_depth_reached,
        focus_external_references=tuple(r for r in external if r is not None),
    )


# ---- Persisted security findings ----

_SEVERITY_RANK = case(
    *[(SecurityFinding.severity == s, i) for i, s in enumerate(SEVERITIES)],
    else_=len(SEVERITIES),
)


def list_findings(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> FindingPage:
    """Findings observed by the latest *completed* security_scan run of this
    revision (the same scoping as the overview's counts). Ordered by
    severity (critical first, unknown last), then title, then id."""
    _resolve(db, repository_id, revision_id)
    limit, offset = _bounded(limit, MAX_PAGE_SIZE), max(0, offset)
    run_id = db.execute(
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
    if run_id is None:
        return FindingPage(
            findings_run_id=None, findings=(), total_count=0, limit=limit, offset=offset
        )

    observations = (
        select(
            SecurityFindingObservation.finding_id.label("finding_id"),
            func.count().label("observation_count"),
            func.min(SourceFile.path).label("first_path"),
        )
        .outerjoin(SourceFile, SourceFile.id == SecurityFindingObservation.file_id)
        .where(SecurityFindingObservation.repository_id == repository_id)
        .where(SecurityFindingObservation.revision_id == revision_id)
        .where(SecurityFindingObservation.analysis_run_id == run_id)
        .group_by(SecurityFindingObservation.finding_id)
        .subquery()
    )
    base = (
        select(SecurityFinding, observations.c.observation_count, observations.c.first_path)
        .join(observations, observations.c.finding_id == SecurityFinding.id)
        .where(SecurityFinding.repository_id == repository_id)
    )
    total = db.execute(select(func.count()).select_from(observations)).scalar_one()
    rows: Sequence[Any] = db.execute(
        base.order_by(_SEVERITY_RANK, SecurityFinding.title, SecurityFinding.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return FindingPage(
        findings_run_id=run_id,
        findings=tuple(
            FindingEntry(
                finding_id=f.id,
                analyzer=f.analyzer,
                rule_id=f.rule_id,
                severity=f.severity,
                title=f.title,
                message=f.message,
                triage_status=f.status,
                cvss_score=f.cvss_score,
                observation_count=count,
                first_location=first_path,
            )
            for f, count, first_path in rows
        ),
        total_count=total,
        limit=limit,
        offset=offset,
    )


# ---- Persisted generated tests ----


def list_generated_tests(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    limit: int = 50,
    offset: int = 0,
) -> GeneratedTestPage:
    """Generated tests of this revision, newest first, each with its most
    recent execution (if any). Test source is not returned."""
    _resolve(db, repository_id, revision_id)
    limit, offset = _bounded(limit, MAX_PAGE_SIZE), max(0, offset)
    scope = [GeneratedTest.repository_id == repository_id, GeneratedTest.revision_id == revision_id]
    total = db.execute(select(func.count()).select_from(GeneratedTest).where(*scope)).scalar_one()
    rows = db.execute(
        select(GeneratedTest, Symbol.qualified_name)
        .outerjoin(
            Symbol,
            (Symbol.id == GeneratedTest.target_symbol_id) & (Symbol.revision_id == revision_id),
        )
        .where(*scope)
        .order_by(GeneratedTest.created_at.desc(), GeneratedTest.id)
        .limit(limit)
        .offset(offset)
    ).all()
    test_ids = [test.id for test, _ in rows]

    latest_execution: dict[uuid.UUID, ExecutionEntry] = {}
    if test_ids:
        rank = (
            func.row_number()
            .over(
                partition_by=TestExecution.generated_test_id,
                order_by=(
                    TestExecution.started_at.desc().nulls_last(),
                    TestExecution.id.desc(),
                ),
            )
            .label("rank")
        )
        ranked = (
            select(TestExecution.id.label("execution_id"), rank)
            .where(TestExecution.repository_id == repository_id)
            .where(TestExecution.revision_id == revision_id)
            .where(TestExecution.generated_test_id.in_(test_ids))
            .subquery()
        )
        for execution in (
            db.execute(
                select(TestExecution)
                .join(ranked, ranked.c.execution_id == TestExecution.id)
                .where(ranked.c.rank == 1)
            )
            .scalars()
            .all()
        ):
            assert execution.generated_test_id is not None
            latest_execution[execution.generated_test_id] = ExecutionEntry(
                execution_id=execution.id,
                status=execution.status,
                exit_code=execution.exit_code,
                timed_out=execution.timed_out,
                duration_ms=execution.duration_ms,
                finished_at=execution.finished_at,
            )

    return GeneratedTestPage(
        tests=tuple(
            GeneratedTestEntry(
                generated_test_id=test.id,
                target_symbol_id=test.target_symbol_id,
                target_qualified_name=name,
                proposed_path=test.proposed_path,
                status=test.status,
                validation_status=test.validation_status,
                provider=test.provider,
                model_name=test.model_name,
                created_at=test.created_at,
                latest_execution=latest_execution.get(test.id),
            )
            for test, name in rows
        ),
        total_count=total,
        limit=limit,
        offset=offset,
    )
