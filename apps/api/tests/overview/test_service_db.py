"""DB tests for app/overview/service.py: revision isolation, deterministic
ordering/tie-breaking, bounds/truncation, graph direction, status selection,
and read-only retrieval. Requires live PostgreSQL — same `db_session`
fixture and explicit-skip behavior as every other DB-dependent suite.

Rows are built directly (not via ingestion) so every expected count is
controlled exactly by the test.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.models import (
    AnalysisRun,
    GraphEdge,
    InvestigationSession,
    Repository,
    RepositoryRevision,
    SecurityFinding,
    SecurityFindingObservation,
    SourceBlob,
    SourceFile,
    Symbol,
)
from app.overview import service
from app.overview.models import NOT_RUN

T0 = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class _Builder:
    db: Session
    repository: Repository
    blob_hash: str
    _line: int = 0
    files: dict[tuple[uuid.UUID, str], SourceFile] = field(default_factory=dict)

    def revision(self, sha_char: str) -> RepositoryRevision:
        rev = RepositoryRevision(
            repository_id=self.repository.id, commit_sha=sha_char * 40, committed_at=T0
        )
        self.db.add(rev)
        self.db.flush()
        return rev

    def file(
        self, rev: RepositoryRevision, path: str, *, size: int = 10, language: str | None = "python"
    ) -> SourceFile:
        row = SourceFile(
            repository_id=self.repository.id,
            revision_id=rev.id,
            path=path,
            content_hash=self.blob_hash,
            size_bytes=size,
            language=language,
        )
        self.db.add(row)
        self.db.flush()
        self.files[(rev.id, path)] = row
        return row

    def symbol(
        self, rev: RepositoryRevision, file: SourceFile, qualified_name: str, kind: str = "function"
    ) -> Symbol:
        row = Symbol(
            repository_id=self.repository.id,
            revision_id=rev.id,
            file_id=file.id,
            kind=kind,
            name=qualified_name.rsplit(".", 1)[-1],
            qualified_name=qualified_name,
            start_line=1,
            end_line=2,
            analyzer="test",
        )
        self.db.add(row)
        self.db.flush()
        return row

    def edge(
        self,
        rev: RepositoryRevision,
        source: Symbol,
        target: Symbol | None,
        relationship: str = "calls",
        external_ref: str | None = None,
    ) -> GraphEdge:
        """source = dependent, target = dependency (canonical invariant)."""
        self._line += 1
        row = GraphEdge(
            repository_id=self.repository.id,
            revision_id=rev.id,
            relationship_type=relationship,
            source_symbol_id=source.id,
            target_symbol_id=target.id if target is not None else None,
            target_external_ref=external_ref,
            resolution_method="ast_resolution",
            evidence_file_id=source.file_id,
            evidence_start_line=self._line,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def run(
        self,
        rev: RepositoryRevision,
        analysis_type: str,
        status: str,
        *,
        started_at: datetime | None = T0,
        analyzer_name: str = "test-analyzer",
    ) -> AnalysisRun:
        row = AnalysisRun(
            repository_id=self.repository.id,
            revision_id=rev.id,
            analysis_type=analysis_type,
            analyzer_name=analyzer_name,
            analyzer_version="1",
            status=status,
            started_at=started_at,
        )
        self.db.add(row)
        self.db.flush()
        return row


@pytest.fixture
def builder(db_session: Session) -> _Builder:
    repo = Repository(
        owner="overview", name="repo", url="https://x/overview/repo", default_branch="main"
    )
    db_session.add(repo)
    blob = SourceBlob(content_hash="c" * 64, inline_content="x = 1\n", size_bytes=6)
    db_session.add(blob)
    db_session.flush()
    return _Builder(db=db_session, repository=repo, blob_hash=blob.content_hash)


def _two_revisions(b: _Builder) -> tuple[RepositoryRevision, RepositoryRevision]:
    """R1 and R2 deliberately differ in every revision-local dimension."""
    r1 = b.revision("1")
    f1 = b.file(r1, "alpha/core.py", size=100)
    core_mod = b.symbol(r1, f1, "alpha.core", kind="module")
    helper = b.symbol(r1, f1, "alpha.core.helper")
    caller = b.symbol(r1, f1, "alpha.core.caller")
    b.edge(r1, caller, helper)
    b.edge(r1, core_mod, None, relationship="imports", external_ref="os")
    b.run(r1, "graph_build", "completed")

    r2 = b.revision("2")
    g1 = b.file(r2, "beta/api.py", size=200)
    g2 = b.file(r2, "gamma/db.py", size=300)
    b.file(r2, "README.md", size=50, language=None)
    api = b.symbol(r2, g1, "beta.api.handle")
    query = b.symbol(r2, g2, "gamma.db.query")
    connect = b.symbol(r2, g2, "gamma.db.connect")
    b.symbol(r2, g2, "gamma.db.Model", kind="class")
    b.edge(r2, api, query)
    b.edge(r2, api, connect)
    b.edge(r2, query, connect)
    b.run(r2, "security_scan", "completed")

    b.repository.current_revision_id = r2.id
    b.db.flush()
    return r1, r2


# ---- Revision isolation ----


def test_r1_overview_never_reports_r2_facts(db_session: Session, builder: _Builder) -> None:
    r1, r2 = _two_revisions(builder)
    overview = service.get_repository_overview(db_session, builder.repository.id, r1.id)

    assert overview.revision.revision_id == r1.id
    assert overview.revision.is_current is False
    assert overview.statistics.stored_file_count == 1
    assert overview.statistics.total_source_bytes == 100
    assert overview.statistics.symbol_count == 3
    assert [d.name for d in overview.structure.top_level_directories] == ["alpha"]
    assert overview.structure.root_file_count == 0
    assert overview.graph.edge_count == 2
    assert overview.graph.internal_edge_count == 1
    assert overview.graph.external_edge_count == 1

    names = {
        c.qualified_name
        for c in (
            *overview.important_components.most_depended_upon_symbols,
            *overview.important_components.most_dependency_heavy_symbols,
        )
    }
    assert names == {"alpha.core.helper", "alpha.core.caller"}
    statuses = {s.analysis_type: s.status for s in overview.analysis_statuses}
    assert statuses["graph_build"] == "completed"
    assert statuses["security_scan"] == NOT_RUN  # R2's scan never leaks into R1
    assert overview.security.latest_run.status == NOT_RUN


def test_r2_overview_reports_only_r2_facts(db_session: Session, builder: _Builder) -> None:
    _, r2 = _two_revisions(builder)
    overview = service.get_repository_overview(db_session, builder.repository.id, r2.id)

    assert overview.revision.is_current is True
    assert overview.statistics.stored_file_count == 3
    assert overview.statistics.language_file_count == 2
    assert overview.statistics.total_source_bytes == 550
    assert overview.statistics.symbol_count == 4
    assert [d.name for d in overview.structure.top_level_directories] == ["beta", "gamma"]
    assert overview.structure.root_file_count == 1
    assert overview.graph.edge_count == 3
    statuses = {s.analysis_type: s.status for s in overview.analysis_statuses}
    assert statuses["graph_build"] == NOT_RUN  # R1's graph run never leaks into R2
    assert statuses["security_scan"] == "completed"
    names = {c.qualified_name for c in overview.important_components.most_depended_upon_symbols}
    assert not any(n.startswith("alpha.") for n in names)


def test_default_is_current_revision_and_explicit_r1_still_returns_r1(
    db_session: Session, builder: _Builder
) -> None:
    r1, r2 = _two_revisions(builder)
    default = service.get_repository_overview(db_session, builder.repository.id)
    assert default.revision.revision_id == r2.id
    explicit = service.get_repository_overview(db_session, builder.repository.id, r1.id)
    assert explicit.revision.revision_id == r1.id
    assert explicit.repository.current_revision_id == r2.id


def test_revision_of_another_repository_is_rejected(db_session: Session, builder: _Builder) -> None:
    _two_revisions(builder)
    other = Repository(owner="other", name="repo", url="https://x/other/repo", default_branch="m")
    db_session.add(other)
    db_session.flush()
    other_rev = RepositoryRevision(repository_id=other.id, commit_sha="9" * 40, committed_at=T0)
    db_session.add(other_rev)
    db_session.flush()
    with pytest.raises(service.RevisionConflictError):
        service.get_repository_overview(db_session, builder.repository.id, other_rev.id)


# ---- Graph direction ----


def test_in_degree_counts_dependents_and_out_degree_counts_dependencies(
    db_session: Session, builder: _Builder
) -> None:
    _, r2 = _two_revisions(builder)
    components = service.get_repository_overview(
        db_session, builder.repository.id, r2.id
    ).important_components

    top_dependency = components.most_depended_upon_symbols[0]
    # connect is called by handle and query: two direct dependents.
    assert (top_dependency.qualified_name, top_dependency.in_degree) == ("gamma.db.connect", 2)
    assert top_dependency.out_degree == 0
    top_dependent = components.most_dependency_heavy_symbols[0]
    # handle calls query and connect: two direct dependencies.
    assert (top_dependent.qualified_name, top_dependent.out_degree) == ("beta.api.handle", 2)

    # Module level: beta/api.py depends on gamma/db.py (one distinct file
    # each way); the intra-file query -> connect edge is not a module edge.
    [db_module] = components.most_depended_upon_modules
    assert (db_module.file_path, db_module.in_degree, db_module.out_degree) == (
        "gamma/db.py",
        1,
        0,
    )
    [api_module] = components.most_dependency_heavy_modules
    assert (api_module.file_path, api_module.out_degree) == ("beta/api.py", 1)


# ---- Determinism ----


def test_repeated_calls_are_equal(db_session: Session, builder: _Builder) -> None:
    _, r2 = _two_revisions(builder)
    first = service.get_repository_overview(db_session, builder.repository.id, r2.id)
    second = service.get_repository_overview(db_session, builder.repository.id, r2.id)
    assert first == second


def test_ranking_ties_break_by_qualified_name_then_id(
    db_session: Session, builder: _Builder
) -> None:
    rev = builder.revision("3")
    f = builder.file(rev, "pkg/m.py")
    caller = builder.symbol(rev, f, "pkg.m.caller")
    # Created in reverse alphabetical order so insertion order can't pass.
    for name in ("pkg.m.zeta", "pkg.m.beta", "pkg.m.alpha"):
        builder.edge(rev, caller, builder.symbol(rev, f, name))
    ranked = service.get_repository_overview(
        db_session, builder.repository.id, rev.id
    ).important_components.most_depended_upon_symbols
    assert [c.qualified_name for c in ranked] == ["pkg.m.alpha", "pkg.m.beta", "pkg.m.zeta"]
    assert {c.in_degree for c in ranked} == {1}


# ---- Bounds ----


def test_structure_and_rankings_truncate_deterministically(
    db_session: Session, builder: _Builder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "MAX_TOP_LEVEL_DIRECTORIES", 2)
    monkeypatch.setattr(service, "MAX_TOP_LEVEL_PACKAGES", 2)
    monkeypatch.setattr(service, "MAX_RANKED_COMPONENTS", 2)
    rev = builder.revision("4")
    hub_file = builder.file(rev, "hub/__init__.py")
    builder.symbol(rev, hub_file, "hub", kind="module")
    hub = builder.symbol(rev, hub_file, "hub.call_everything")
    for name in ("d1", "d2", "d3", "d4"):
        f = builder.file(rev, f"{name}/__init__.py")
        builder.symbol(rev, f, name, kind="module")
        builder.edge(rev, hub, builder.symbol(rev, f, f"{name}.target"))

    first = service.get_repository_overview(db_session, builder.repository.id, rev.id)
    structure = first.structure
    assert structure.top_level_directory_count == 5
    # All five directories hold one file; ties break by name.
    assert [d.name for d in structure.top_level_directories] == ["d1", "d2"]
    assert structure.top_level_directories_truncated is True
    assert [p.qualified_name for p in structure.top_level_packages] == ["d1", "d2"]
    assert structure.top_level_packages_truncated is True
    components = first.important_components
    assert len(components.most_depended_upon_symbols) == 2
    assert components.most_depended_upon_symbols_truncated is True
    assert len(components.most_depended_upon_modules) == 2
    assert components.most_depended_upon_modules_truncated is True
    assert [m.module_name for m in components.most_depended_upon_modules] == ["d1", "d2"]
    assert components.most_dependency_heavy_modules_truncated is False

    assert service.get_repository_overview(db_session, builder.repository.id, rev.id) == first


# ---- Status semantics ----


def test_latest_status_and_findings_come_from_the_right_runs(
    db_session: Session, builder: _Builder
) -> None:
    rev = builder.revision("5")
    completed = builder.run(rev, "security_scan", "completed", started_at=T0)
    builder.run(rev, "security_scan", "failed", started_at=T0 + timedelta(hours=1))
    finding = SecurityFinding(
        repository_id=builder.repository.id,
        analyzer="semgrep",
        rule_id="r",
        fingerprint="fp",
        severity="high",
        title="t",
        message="m",
    )
    db_session.add(finding)
    db_session.flush()
    db_session.add(
        SecurityFindingObservation(
            repository_id=builder.repository.id,
            finding_id=finding.id,
            revision_id=rev.id,
            analysis_run_id=completed.id,
            raw_finding={},
        )
    )
    db_session.flush()

    security = service.get_repository_overview(db_session, builder.repository.id, rev.id).security
    assert security.latest_run.status == "failed"  # never masked as healthy
    assert security.findings_run_id == completed.id
    assert security.finding_count == 1
    assert [(c.key, c.count) for c in security.findings_by_severity] == [("high", 1)]


# ---- Read-only ----


def test_retrieval_writes_nothing_and_never_touches_session_recency(
    db_session: Session, builder: _Builder
) -> None:
    _, r2 = _two_revisions(builder)
    session_row = InvestigationSession(repository_id=builder.repository.id, title="why?")
    db_session.add(session_row)
    db_session.flush()
    last_active = session_row.last_active_at

    overview = service.get_repository_overview(db_session, builder.repository.id, r2.id)

    assert not db_session.new and not db_session.dirty and not db_session.deleted
    db_session.refresh(session_row)
    assert session_row.last_active_at == last_active
    assert overview.sessions.session_count == 1
    assert overview.sessions.recent_sessions[0].title == "why?"
