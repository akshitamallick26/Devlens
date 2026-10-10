import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Repository, RepositoryRevision


def test_revision_requires_a_real_repository(db_session: Session) -> None:
    """A RepositoryRevision can't reference a repository that doesn't exist."""
    bogus = RepositoryRevision(
        repository_id=uuid.uuid4(),
        commit_sha="a" * 40,
        committed_at=datetime.now(UTC),
    )
    db_session.add(bogus)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_commit_sha_is_unique_per_repository(db_session: Session, repository: Repository) -> None:
    """Re-ingesting the same commit for the same repository must fail."""
    first = RepositoryRevision(
        repository_id=repository.id, commit_sha="c" * 40, committed_at=datetime.now(UTC)
    )
    db_session.add(first)
    db_session.flush()

    duplicate = RepositoryRevision(
        repository_id=repository.id, commit_sha="c" * 40, committed_at=datetime.now(UTC)
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_commit_sha_allowed_across_different_repositories(db_session: Session) -> None:
    """The uniqueness is scoped to (repository_id, commit_sha), not global."""
    repo_a = Repository(owner="a", name="repo", url="https://x/a/repo", default_branch="main")
    repo_b = Repository(owner="b", name="repo", url="https://x/b/repo", default_branch="main")
    db_session.add_all([repo_a, repo_b])
    db_session.flush()

    db_session.add_all(
        [
            RepositoryRevision(
                repository_id=repo_a.id, commit_sha="d" * 40, committed_at=datetime.now(UTC)
            ),
            RepositoryRevision(
                repository_id=repo_b.id, commit_sha="d" * 40, committed_at=datetime.now(UTC)
            ),
        ]
    )
    db_session.flush()  # must not raise
