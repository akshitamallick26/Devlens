"""Documentation Intelligence v1.1: cross-revision and cross-repository
isolation. Live PostgreSQL (same `db_session` fixture and explicit skip as
every DB suite), using the real ingestion -> parsing -> graph pipeline.

Contract: documentation for (repository, revision) is built only from that
revision's own files, symbols, source, graph edges and README, and its
artifact cites only evidence assembled for that revision. Every fixture
symbol below takes part in a `calls` edge, so each would surface in every
artifact type if a query leaked; the positive controls prove that."""

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.documentation import service
from app.documentation.context import (
    assemble_architecture_overview_context,
    assemble_module_context,
    assemble_repository_overview_context,
    assemble_symbol_context,
)
from app.documentation.generation import DocumentationResponse, DocumentationSectionResponse
from app.documentation.models import (
    ArchitectureOverviewFacts,
    DocumentationArtifactResult,
    DocumentationContextPackage,
    RepositoryOverviewFacts,
)
from app.models import (
    GeneratedArtifact,
    GraphEdge,
    Repository,
    RepositoryRevision,
    SourceFile,
    Symbol,
)
from tests.ai.fakes import FakeProvider
from tests.automation.conftest import analyze_revision, ingest_revision, modify_and_commit
from tests.ingestion.conftest import run_git

T = TypeVar("T", bound=BaseModel)

ARTIFACT_TYPES = (
    "repository_overview",
    "architecture_overview",
    "module_documentation",
    "symbol_documentation",
)


def _module(tag: str) -> str:
    return f"def alpha_{tag}():\n    return 1\n\n\ndef use():\n    return alpha_{tag}()\n"


def _readme(tag: str) -> str:
    return f"readme_{tag}_marker\n"


@pytest.fixture
def isolation_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "isolation-repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text("")
    (repo / "app" / "mod.py").write_text(_module("r1"))
    (repo / "README.md").write_text(_readme("r1"))
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "r1")
    return repo


@dataclass(frozen=True)
class _Revision:
    repository_id: uuid.UUID
    revision_id: uuid.UUID


def _analyze(db: Session, repo: Path, owner: str) -> _Revision:
    result = ingest_revision(db, repo, owner=owner)
    rev = _Revision(uuid.UUID(result.repository_id), uuid.UUID(result.revision_id))
    analyze_revision(db, rev.repository_id, rev.revision_id)
    return rev


def _two_revisions(db: Session, repo: Path) -> tuple[_Revision, _Revision]:
    r1 = _analyze(db, repo, "isolation")
    (repo / "README.md").write_text(_readme("r2"))
    modify_and_commit(repo, "app/mod.py", _module("r2"), "r2")
    r2 = _analyze(db, repo, "isolation")
    assert r1.repository_id == r2.repository_id and r1.revision_id != r2.revision_id
    return r1, r2


def _targets(db: Session, rev: _Revision) -> tuple[SourceFile, Symbol]:
    source_file = db.execute(
        select(SourceFile).where(
            SourceFile.revision_id == rev.revision_id, SourceFile.path == "app/mod.py"
        )
    ).scalar_one()
    symbol = db.execute(
        select(Symbol).where(
            Symbol.revision_id == rev.revision_id, Symbol.qualified_name == "app.mod.use"
        )
    ).scalar_one()
    return source_file, symbol


def _target_kwargs(db: Session, rev: _Revision, artifact_type: str) -> dict[str, uuid.UUID]:
    source_file, symbol = _targets(db, rev)
    if artifact_type == "module_documentation":
        return {"source_file_id": source_file.id}
    if artifact_type == "symbol_documentation":
        return {"symbol_id": symbol.id}
    return {}


def _packages(db: Session, rev: _Revision) -> dict[str, DocumentationContextPackage]:
    repository = db.get(Repository, rev.repository_id)
    revision = db.get(RepositoryRevision, rev.revision_id)
    assert repository is not None and revision is not None
    source_file, symbol = _targets(db, rev)
    return {
        "repository_overview": assemble_repository_overview_context(db, repository, revision),
        "architecture_overview": assemble_architecture_overview_context(db, repository, revision),
        "module_documentation": assemble_module_context(db, repository, revision, source_file),
        "symbol_documentation": assemble_symbol_context(
            db, repository, revision, symbol, source_file.path
        ),
    }


def _generate_all(
    db: Session, rev: _Revision, provider: FakeProvider | None = None
) -> dict[str, DocumentationArtifactResult]:
    return {
        artifact_type: service.generate_documentation(
            db,
            provider,
            rev.repository_id,
            rev.revision_id,
            artifact_type,
            **_target_kwargs(db, rev, artifact_type),
        )
        for artifact_type in ARTIFACT_TYPES
    }


def _symbol_ids(db: Session, revision_id: uuid.UUID) -> set[uuid.UUID]:
    return set(db.execute(select(Symbol.id).where(Symbol.revision_id == revision_id)).scalars())


def _assert_documentation_isolated(
    db: Session, own: _Revision, foreign: _Revision, own_marker: str, foreign_marker: str
) -> None:
    own_symbols = _symbol_ids(db, own.revision_id)
    foreign_symbols = _symbol_ids(db, foreign.revision_id)
    packages = _packages(db, own)
    overview = packages["repository_overview"].facts
    assert isinstance(overview, RepositoryOverviewFacts)
    own_edges = db.execute(
        select(func.count()).where(GraphEdge.revision_id == own.revision_id)
    ).scalar_one()
    own_files = db.execute(
        select(func.count()).where(SourceFile.revision_id == own.revision_id)
    ).scalar_one()
    # Counts are revision facts too: R1+R2 totals would mean a leak.
    assert (overview.symbol_count, overview.file_count, overview.graph_edge_count) == (
        len(own_symbols),
        own_files,
        own_edges,
    )
    architecture = packages["architecture_overview"].facts
    assert isinstance(architecture, ArchitectureOverviewFacts)
    assert sum(count for _, count in architecture.relationship_counts) == own_edges
    for artifact_type, package in packages.items():
        assembled = repr(package.facts) + repr(package.evidence)
        assert foreign_marker not in assembled, artifact_type
        assert package.evidence.items, artifact_type
        for item in package.evidence.items:
            assert (item.repository_id, item.revision_id) == (
                own.repository_id,
                own.revision_id,
            ), artifact_type
            if item.symbol_id is not None:
                assert item.symbol_id in own_symbols, artifact_type
                assert item.symbol_id not in foreign_symbols, artifact_type

    for artifact_type, outcome in _generate_all(db, own).items():
        assert (outcome.repository_id, outcome.revision_id) == (
            own.repository_id,
            own.revision_id,
        )
        assert foreign_marker not in outcome.content, artifact_type
        reread = service.get_artifact(db, own.repository_id, outcome.artifact_id)
        assert reread.revision_id == own.revision_id
        assert foreign_marker not in reread.content, artifact_type
        if artifact_type != "repository_overview":
            # Positive control: the same query does surface this revision's
            # own graph-connected symbol (the repository overview reports
            # counts, not symbol names).
            assert own_marker in outcome.content, artifact_type


# ---- 1/2. Cross-revision, both directions ----


def test_r1_documentation_cannot_see_r2_symbols(db_session: Session, isolation_repo: Path) -> None:
    r1, r2 = _two_revisions(db_session, isolation_repo)
    _assert_documentation_isolated(db_session, r1, r2, "alpha_r1", "alpha_r2")
    readme = _packages(db_session, r1)["repository_overview"].evidence.items[0]
    assert readme.content == _readme("r1")  # R1's README text, not R2's


def test_r2_documentation_cannot_see_r1_symbols(db_session: Session, isolation_repo: Path) -> None:
    r1, r2 = _two_revisions(db_session, isolation_repo)
    _assert_documentation_isolated(db_session, r2, r1, "alpha_r2", "alpha_r1")
    readme = _packages(db_session, r2)["repository_overview"].evidence.items[0]
    assert readme.content == _readme("r2")


def test_generating_r2_never_rewrites_r1_artifacts(
    db_session: Session, isolation_repo: Path
) -> None:
    r1, r2 = _two_revisions(db_session, isolation_repo)
    first = _generate_all(db_session, r1)
    second = _generate_all(db_session, r2)
    for artifact_type, outcome in first.items():
        assert second[artifact_type].artifact_id != outcome.artifact_id
        reread = service.get_artifact(db_session, r1.repository_id, outcome.artifact_id)
        assert (reread.content, reread.evidence_ids) == (outcome.content, outcome.evidence_ids)
    listed = service.list_artifacts(db_session, r1.repository_id, revision_id=r1.revision_id)
    assert {a.artifact_id for a in listed} == {o.artifact_id for o in first.values()}


def test_a_target_from_another_revision_is_rejected_before_any_run(
    db_session: Session, isolation_repo: Path
) -> None:
    r1, r2 = _two_revisions(db_session, isolation_repo)
    r1_file, r1_symbol = _targets(db_session, r1)
    for kwargs in ({"symbol_id": r1_symbol.id}, {"source_file_id": r1_file.id}):
        artifact_type = "symbol_documentation" if "symbol_id" in kwargs else "module_documentation"
        with pytest.raises(service.TargetConflictError, match="different revision"):
            service.generate_documentation(
                db_session, None, r2.repository_id, r2.revision_id, artifact_type, **kwargs
            )
    assert service.list_artifacts(db_session, r2.repository_id) == ()


# ---- 3. Cross-repository, identical names ----


def test_repository_a_cannot_see_repository_b(
    db_session: Session, isolation_repo: Path, tmp_path: Path
) -> None:
    a = _analyze(db_session, isolation_repo, "isolation-a")
    # Repository B: identical module and symbol names, plus a B-only caller.
    repo_b = tmp_path / "isolation-repo-b"
    run_git(tmp_path, "clone", "-q", str(isolation_repo), str(repo_b))
    (repo_b / "README.md").write_text(_readme("repo_b"))
    modify_and_commit(
        repo_b, "app/mod.py", _module("r1") + "\n\ndef only_in_b():\n    return use()\n", "b"
    )
    b = _analyze(db_session, repo_b, "isolation-b")
    assert a.repository_id != b.repository_id

    a_symbols = set(
        db_session.execute(select(Symbol.id).where(Symbol.repository_id == a.repository_id))
        .scalars()
        .all()
    )
    for artifact_type, package in _packages(db_session, a).items():
        assembled = repr(package.facts) + repr(package.evidence)
        assert "only_in_b" not in assembled and "readme_repo_b_marker" not in assembled
        for item in package.evidence.items:
            assert item.repository_id == a.repository_id, artifact_type
            assert item.symbol_id is None or item.symbol_id in a_symbols, artifact_type

    a_docs = _generate_all(db_session, a)
    b_docs = _generate_all(db_session, b)
    for artifact_type, outcome in a_docs.items():
        assert "only_in_b" not in outcome.content and "readme_repo_b_marker" not in outcome.content
        assert outcome.repository_id == a.repository_id, artifact_type
    assert "only_in_b" in b_docs["symbol_documentation"].content  # positive control: B's caller

    # Untrusted identifiers never resolve across repositories.
    _, b_symbol = _targets(db_session, b)
    with pytest.raises(service.TargetConflictError, match="different repository"):
        service.generate_documentation(
            db_session,
            None,
            a.repository_id,
            a.revision_id,
            "symbol_documentation",
            symbol_id=b_symbol.id,
        )
    with pytest.raises(service.RevisionNotFoundError):
        service.generate_documentation(
            db_session, None, a.repository_id, b.revision_id, "repository_overview"
        )
    with pytest.raises(service.ArtifactNotFoundError):
        service.get_artifact(
            db_session, a.repository_id, b_docs["module_documentation"].artifact_id
        )
    assert {s.artifact_id for s in service.list_artifacts(db_session, a.repository_id)} == {
        o.artifact_id for o in a_docs.values()
    }


# ---- 4/5. Evidence ids and persisted citations ----


@dataclass
class _RecordingProvider(FakeProvider):
    """Records exactly what would be sent to a model; never a network call."""

    prompts: list[str] = field(default_factory=list)

    def generate_structured(
        self, *, system_prompt: str, user_content: str, response_model: type[T]
    ) -> T:
        self.prompts.append(user_content)
        return super().generate_structured(
            system_prompt=system_prompt, user_content=user_content, response_model=response_model
        )


def test_evidence_ids_and_persisted_citations_stay_revision_local(
    db_session: Session, isolation_repo: Path
) -> None:
    r1, r2 = _two_revisions(db_session, isolation_repo)
    # Cites real ids plus D99, which no package contains.
    provider = _RecordingProvider(
        response=DocumentationResponse(
            title="Doc",
            summary="See [D1] and [D99].",
            sections=[
                DocumentationSectionResponse(
                    heading="Body", content="[D1] [D2] [D99]", evidence_ids=["D1", "D2", "D99"]
                )
            ],
        )
    )
    outcomes = _generate_all(db_session, r1, provider)

    assert len(provider.prompts) == len(ARTIFACT_TYPES)
    for prompt in provider.prompts:
        assert "alpha_r2" not in prompt and "readme_r2_marker" not in prompt

    r1_symbols = _symbol_ids(db_session, r1.revision_id)
    packages = _packages(db_session, r1)  # deterministic: the same D<n> assignment
    for artifact_type, outcome in outcomes.items():
        assert outcome.generation_method == "llm_generated"
        artifact = db_session.get(GeneratedArtifact, outcome.artifact_id)
        assert artifact is not None and artifact.revision_id == r1.revision_id
        by_id = packages[artifact_type].evidence.by_id()
        assert list(artifact.evidence_ids) == list(by_id)
        assert "D99" not in artifact.cited_evidence_ids
        assert set(artifact.cited_evidence_ids) <= set(artifact.evidence_ids)
        for cited in artifact.cited_evidence_ids:
            item = by_id[cited]
            assert item.revision_id == r1.revision_id, artifact_type
            assert item.symbol_id is None or item.symbol_id in r1_symbols, artifact_type
        assert "[D99]" not in outcome.content and "alpha_r2" not in outcome.content


# ---- Architecture-overview contract (why the old fixture was vacuous) ----


def test_architecture_overview_lists_only_graph_connected_symbols(
    db_session: Session, isolation_repo: Path
) -> None:
    """Key symbols are ranked by graph degree, so a symbol with no edges is
    never listed in any revision. Isolation tests must therefore use
    graph-connected symbols, or "absent from R1" proves nothing."""
    modify_and_commit(
        isolation_repo,
        "app/mod.py",
        _module("r1") + "\n\ndef isolated_symbol():\n    return 0\n",
        "unconnected symbol",
    )
    rev = _analyze(db_session, isolation_repo, "isolation")
    content = service.generate_documentation(
        db_session, None, rev.repository_id, rev.revision_id, "architecture_overview"
    ).content
    assert "alpha_r1" in content and "app.mod.use" in content
    assert "isolated_symbol" not in content
