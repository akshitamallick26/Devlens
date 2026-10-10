import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AnalysisRun, Repository, RepositoryRevision, SourceFile, Symbol
from tests.conftest import make_repo_and_revision


def _symbol(
    repository: Repository, revision: RepositoryRevision, source_file: SourceFile, **kwargs
) -> Symbol:
    defaults = dict(
        repository_id=repository.id,
        revision_id=revision.id,
        file_id=source_file.id,
        kind="method",
        name="total",
        qualified_name="Invoice.total",
        start_line=10,
        end_line=12,
        analyzer="tree-sitter-python@0.21",
    )
    defaults.update(kwargs)
    return Symbol(**defaults)


def test_parent_symbol_in_same_revision_succeeds(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    source_file: SourceFile,
    symbol: Symbol,
) -> None:
    child = _symbol(repository, revision, source_file, parent_symbol_id=symbol.id)
    db_session.add(child)
    db_session.flush()  # must not raise


def test_parent_symbol_from_another_revision_is_rejected_by_the_database(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    source_file: SourceFile,
) -> None:
    """DB-enforced (category A): the composite FK (revision_id,
    parent_symbol_id) -> symbols(revision_id, id) added during the hardening
    pass. A symbol from a different revision cannot be used as a parent."""
    other_repo, other_revision = make_repo_and_revision(db_session, "someone-else")
    blob_owner_file = SourceFile(
        repository_id=other_repo.id,
        revision_id=other_revision.id,
        path="src/other.py",
        content_hash=source_file.content_hash,
        size_bytes=source_file.size_bytes,
    )
    db_session.add(blob_owner_file)
    db_session.flush()
    foreign_parent = _symbol(other_repo, other_revision, blob_owner_file)
    db_session.add(foreign_parent)
    db_session.flush()

    child = _symbol(repository, revision, source_file, parent_symbol_id=foreign_parent.id)
    db_session.add(child)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_analysis_run_from_another_revision_is_rejected_by_the_application_layer(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    source_file: SourceFile,
    analysis_run: AnalysisRun,
) -> None:
    """Application-validated (not DB-enforced): Symbol.analysis_run_id is a
    plain SET NULL FK, but the `@validates` backstop rejects a run from a
    different revision before it ever reaches the database."""
    other_repo, other_revision = make_repo_and_revision(db_session, "someone-else")
    foreign_run = AnalysisRun(
        repository_id=other_repo.id,
        revision_id=other_revision.id,
        analysis_type="parsing",
        analyzer_name="tree-sitter-python",
        analyzer_version="0.21.0",
    )
    db_session.add(foreign_run)
    db_session.flush()

    child = _symbol(repository, revision, source_file)
    db_session.add(child)
    db_session.flush()

    with pytest.raises(ValueError, match="different revision_id"):
        child.analysis_run_id = foreign_run.id
