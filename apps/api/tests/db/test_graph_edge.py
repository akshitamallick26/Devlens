import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AnalysisRun, GraphEdge, Repository, RepositoryRevision, Symbol
from tests.conftest import make_repo_and_revision


def _edge_kwargs(
    repository: Repository, revision: RepositoryRevision, symbol: Symbol, source_file
) -> dict:
    return dict(
        repository_id=repository.id,
        revision_id=revision.id,
        relationship_type="calls",
        source_symbol_id=symbol.id,
        confidence=0.9,
        resolution_method="syntactic",
        evidence_file_id=source_file.id,
        evidence_start_line=1,
    )


def test_edge_rejects_neither_target_populated(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    symbol: Symbol,
    source_file,
) -> None:
    edge = GraphEdge(**_edge_kwargs(repository, revision, symbol, source_file))
    db_session.add(edge)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_edge_rejects_both_targets_populated(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    symbol: Symbol,
    source_file,
) -> None:
    edge = GraphEdge(
        **_edge_kwargs(repository, revision, symbol, source_file),
        target_symbol_id=symbol.id,
        target_external_ref="numpy.array",
    )
    db_session.add(edge)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_edge_with_internal_target_succeeds(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    symbol: Symbol,
    source_file,
) -> None:
    edge = GraphEdge(
        **_edge_kwargs(repository, revision, symbol, source_file), target_symbol_id=symbol.id
    )
    db_session.add(edge)
    db_session.flush()  # must not raise


def test_edge_with_external_target_succeeds(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    symbol: Symbol,
    source_file,
) -> None:
    edge = GraphEdge(
        **_edge_kwargs(repository, revision, symbol, source_file), target_external_ref="numpy.array"
    )
    db_session.add(edge)
    db_session.flush()  # must not raise


def test_edge_repository_must_match_revisions_repository(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    symbol: Symbol,
    source_file,
) -> None:
    """The composite FK (repository_id, revision_id) -> repository_revisions
    must reject a repository_id that doesn't actually own this revision —
    this is the design-audit item 5 invariant."""
    other_repo = Repository(
        owner="someone-else", name="other-repo", url="https://x/other", default_branch="main"
    )
    db_session.add(other_repo)
    db_session.flush()

    edge = GraphEdge(
        **{
            **_edge_kwargs(repository, revision, symbol, source_file),
            "repository_id": other_repo.id,
        },
        target_external_ref="numpy.array",
    )
    db_session.add(edge)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_edge_source_symbol_must_belong_to_same_revision(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    symbol: Symbol,
    source_file,
) -> None:
    """The composite FK (revision_id, source_symbol_id) -> symbols rejects a
    symbol id that doesn't actually exist in this revision."""
    edge = GraphEdge(
        **{
            **_edge_kwargs(repository, revision, symbol, source_file),
            "source_symbol_id": uuid.uuid4(),
        },
        target_external_ref="numpy.array",
    )
    db_session.add(edge)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_edge_analysis_run_from_another_revision_is_rejected_by_the_application_layer(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    symbol: Symbol,
    source_file,
) -> None:
    """Application-validated (not DB-enforced): analysis_run_id is a plain
    SET NULL FK (provenance pointer), with `_validate_analysis_run_id` as
    the backstop."""
    _other_repo, other_revision = make_repo_and_revision(db_session, "someone-else")
    foreign_run = AnalysisRun(
        repository_id=other_revision.repository_id,
        revision_id=other_revision.id,
        analysis_type="graph_build",
        analyzer_name="devlens-graph-builder",
        analyzer_version="0.1.0",
    )
    db_session.add(foreign_run)
    db_session.flush()

    edge = GraphEdge(
        **_edge_kwargs(repository, revision, symbol, source_file), target_external_ref="numpy.array"
    )
    db_session.add(edge)
    db_session.flush()

    with pytest.raises(ValueError, match="different revision_id"):
        edge.analysis_run_id = foreign_run.id
