import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Repository, RepositoryRevision, SourceBlob, SourceFile


def test_blob_requires_exactly_one_storage_mode(db_session: Session) -> None:
    neither = SourceBlob(content_hash="e" * 40, size_bytes=10)
    db_session.add(neither)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_blob_rejects_both_storage_modes_at_once(db_session: Session) -> None:
    both = SourceBlob(
        content_hash="f" * 40, inline_content="x", storage_key="s3://bucket/key", size_bytes=10
    )
    db_session.add(both)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_blob_with_inline_content_only_succeeds(db_session: Session) -> None:
    blob = SourceBlob(content_hash="g" * 40, inline_content="x", size_bytes=1)
    db_session.add(blob)
    db_session.flush()  # must not raise


def test_blob_with_storage_key_only_succeeds(db_session: Session) -> None:
    blob = SourceBlob(content_hash="h" * 40, storage_key="s3://bucket/key", size_bytes=1)
    db_session.add(blob)
    db_session.flush()  # must not raise


def test_file_path_is_unique_per_revision(
    db_session: Session, repository: Repository, revision: RepositoryRevision
) -> None:
    blob = SourceBlob(content_hash="i" * 40, inline_content="x", size_bytes=1)
    db_session.add(blob)
    db_session.flush()

    first = SourceFile(
        repository_id=repository.id,
        revision_id=revision.id,
        path="src/app.py",
        content_hash=blob.content_hash,
        size_bytes=1,
    )
    db_session.add(first)
    db_session.flush()

    duplicate = SourceFile(
        repository_id=repository.id,
        revision_id=revision.id,
        path="src/app.py",
        content_hash=blob.content_hash,
        size_bytes=1,
    )
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_path_allowed_across_different_revisions(
    db_session: Session, repository: Repository, revision: RepositoryRevision
) -> None:
    """Path uniqueness is scoped per revision — a file keeps its path across
    revisions and that must not collide with itself."""
    blob = SourceBlob(content_hash="j" * 40, inline_content="x", size_bytes=1)
    db_session.add(blob)
    db_session.flush()

    other_revision = RepositoryRevision(
        repository_id=repository.id, commit_sha="k" * 40, committed_at=revision.committed_at
    )
    db_session.add(other_revision)
    db_session.flush()

    db_session.add_all(
        [
            SourceFile(
                repository_id=repository.id,
                revision_id=revision.id,
                path="src/app.py",
                content_hash=blob.content_hash,
                size_bytes=1,
            ),
            SourceFile(
                repository_id=repository.id,
                revision_id=other_revision.id,
                path="src/app.py",
                content_hash=blob.content_hash,
                size_bytes=1,
            ),
        ]
    )
    db_session.flush()  # must not raise
