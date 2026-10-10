import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Repository, RepositoryRevision, SourceFile, TestExecution
from tests.conftest import make_repo_and_revision


def test_existing_test_execution_with_matching_repo_and_revision_succeeds(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    source_file: SourceFile,
) -> None:
    execution = TestExecution(
        repository_id=repository.id,
        revision_id=revision.id,
        file_id=source_file.id,
        test_identifier="tests/test_x.py::test_y",
        status="passed",
    )
    db_session.add(execution)
    db_session.flush()  # must not raise


def test_repository_and_revision_from_different_repos_is_rejected(
    db_session: Session, repository: Repository, source_file: SourceFile
) -> None:
    """DB-enforced (category A, added during the hardening pass): the
    composite FK (repository_id, revision_id) -> repository_revisions."""
    _other_repo, other_revision = make_repo_and_revision(db_session, "someone-else")

    execution = TestExecution(
        repository_id=repository.id,  # mismatched with other_revision's owner
        revision_id=other_revision.id,
        file_id=source_file.id,
        test_identifier="tests/test_x.py::test_y",
        status="passed",
    )
    db_session.add(execution)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_repository_set_without_revision_is_rejected(
    db_session: Session, repository: Repository, source_file: SourceFile
) -> None:
    """The CHECK constraint added alongside the composite FK: MATCH SIMPLE
    would otherwise let repository_id be set alone and silently skip the
    composite FK check entirely. file_id/test_identifier are populated with
    a valid pair so only the repository/revision CHECK is under test."""
    execution = TestExecution(
        repository_id=repository.id,
        revision_id=None,
        file_id=source_file.id,
        test_identifier="tests/test_x.py::test_y",
        status="passed",
    )
    db_session.add(execution)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_file_from_another_revision_is_rejected_by_the_application_layer(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    source_file: SourceFile,
) -> None:
    """Application-validated (not DB-enforced): file_id stays a simple FK
    because TestExecution and SourceFile are sibling cascades off
    repository_revisions — see database-design.md. `_validate_file_id` is
    the backstop."""
    other_repo, other_revision = make_repo_and_revision(db_session, "someone-else")
    foreign_file = SourceFile(
        repository_id=other_repo.id,
        revision_id=other_revision.id,
        path="src/other.py",
        content_hash=source_file.content_hash,
        size_bytes=source_file.size_bytes,
    )
    db_session.add(foreign_file)
    db_session.flush()

    execution = TestExecution(
        repository_id=repository.id,
        revision_id=revision.id,
        test_identifier="tests/test_x.py::test_y",
        status="passed",
    )
    # Attached but not flushed: the row is incomplete until file_id is set
    # (the exactly_one_test_identity CHECK rejects it), and the validator
    # only needs the session to look the file up.
    db_session.add(execution)

    with pytest.raises(ValueError, match="different revision_id"):
        execution.file_id = foreign_file.id
