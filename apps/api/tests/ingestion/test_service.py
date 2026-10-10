"""Full-pipeline ingestion tests: local Git fixture -> ingest_identity ->
real Repository/RepositoryRevision/SourceFile/SourceBlob rows. Requires live
PostgreSQL — uses the same `db_session` fixture (and the same explicit-skip
behavior when Postgres isn't reachable) as tests/db/. Never substitutes
SQLite.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.ingestion import service as service_module
from app.ingestion.checkout import Checkout
from app.ingestion.service import IngestionError, ingest_identity
from app.ingestion.url_policy import RepositoryIdentity
from app.models import Repository, RepositoryRevision, SourceBlob, SourceFile
from tests.ingestion.conftest import run_git


def _local_identity(
    path: Path, owner: str = "local-test", name: str = "fixture-repo"
) -> RepositoryIdentity:
    """A RepositoryIdentity pointing at a local path — never produced by
    `normalize_repository_url` (which is HTTPS/github.com-only), used here
    specifically so these tests don't need real network access. See
    `ingest_identity`'s docstring for why this split exists."""
    return RepositoryIdentity(
        host="local-test",
        owner=owner,
        name=name,
        canonical_url=str(path),
        clone_url=str(path),
    )


@pytest.fixture
def empty_git_repo(tmp_path: Path) -> Path:
    """A repo with no commits at all — `git rev-parse HEAD` fails here,
    simulating a malformed/unusable repository state."""
    repo_dir = tmp_path / "empty-repo"
    repo_dir.mkdir()
    run_git(repo_dir, "init", "-q", "-b", "main")
    return repo_dir


def test_ingestion_creates_repository_revision_files_and_blobs(
    db_session: Session, local_git_repo: Path, local_git_repo_head_sha: str
) -> None:
    identity = _local_identity(local_git_repo)
    result = ingest_identity(db_session, identity)

    assert result.status == "ready"
    assert result.commit_sha == local_git_repo_head_sha
    assert result.file_count == 5  # 6 tracked files minus logo.png (binary)

    repository = db_session.get(Repository, result.repository_id)
    assert repository is not None
    assert repository.owner == "local-test"
    assert repository.name == "fixture-repo"
    assert repository.status == "ready"
    assert str(repository.current_revision_id) == result.revision_id
    assert repository.default_branch == "main"

    revision = db_session.get(RepositoryRevision, result.revision_id)
    assert revision is not None
    assert revision.commit_sha == local_git_repo_head_sha
    assert revision.file_count == 5
    assert revision.latest_analysis_status == "completed"

    files = (
        db_session.execute(
            select(SourceFile)
            .where(SourceFile.revision_id == revision.id)
            .order_by(SourceFile.path)
        )
        .scalars()
        .all()
    )
    paths = [f.path for f in files]
    assert paths == sorted(paths)  # deterministic ordering
    assert paths == [
        "README.md",
        "duplicate_a.txt",
        "duplicate_b.txt",
        "nested/deep/file.txt",
        "src/main.py",
    ]
    assert "logo.png" not in paths  # binary — never stored


def test_duplicate_content_deduplicates_to_one_source_blob(
    db_session: Session, local_git_repo: Path
) -> None:
    identity = _local_identity(local_git_repo)
    result = ingest_identity(db_session, identity)

    files = (
        db_session.execute(select(SourceFile).where(SourceFile.revision_id == result.revision_id))
        .scalars()
        .all()
    )
    by_path = {f.path: f for f in files}
    assert by_path["duplicate_a.txt"].content_hash == by_path["duplicate_b.txt"].content_hash

    blob = db_session.get(SourceBlob, by_path["duplicate_a.txt"].content_hash)
    assert blob is not None
    assert blob.inline_content == "identical content\n"


def test_second_ingestion_of_the_same_commit_is_idempotent(
    db_session: Session, local_git_repo: Path
) -> None:
    identity = _local_identity(local_git_repo)
    first = ingest_identity(db_session, identity)
    second = ingest_identity(db_session, identity)

    assert second.revision_id == first.revision_id
    assert second.commit_sha == first.commit_sha

    all_revisions = (
        db_session.execute(
            select(RepositoryRevision).where(
                RepositoryRevision.repository_id == first.repository_id
            )
        )
        .scalars()
        .all()
    )
    assert len(all_revisions) == 1  # no duplicate revision row

    files = (
        db_session.execute(select(SourceFile).where(SourceFile.revision_id == first.revision_id))
        .scalars()
        .all()
    )
    assert len(files) == 5  # not duplicated either


def test_temporary_checkout_is_removed_after_ingestion(
    db_session: Session, local_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Path] = {}
    original = service_module.checkout_repository

    @contextmanager
    def spy(clone_url: str, *, timeout: float) -> Iterator[Checkout]:
        with original(clone_url, timeout=timeout) as checkout:
            captured["path"] = checkout.path
            yield checkout

    monkeypatch.setattr(service_module, "checkout_repository", spy)

    ingest_identity(db_session, _local_identity(local_git_repo))

    assert "path" in captured
    assert not captured["path"].exists()


def test_ingestion_of_nonexistent_repository_fails_cleanly(
    db_session: Session, tmp_path: Path
) -> None:
    identity = _local_identity(tmp_path / "does-not-exist", owner="missing", name="repo")

    with pytest.raises(IngestionError):
        ingest_identity(db_session, identity)

    repository = db_session.query(Repository).filter_by(owner="missing", name="repo").one_or_none()
    assert repository is not None
    assert repository.status == "error"
    assert repository.current_revision_id is None


def test_ingestion_of_a_repository_with_no_commits_fails_cleanly(
    db_session: Session, empty_git_repo: Path
) -> None:
    identity = _local_identity(empty_git_repo, owner="empty", name="repo")

    with pytest.raises(IngestionError):
        ingest_identity(db_session, identity)

    repository = db_session.query(Repository).filter_by(owner="empty", name="repo").one_or_none()
    assert repository is not None
    assert repository.status == "error"


def test_clone_timeout_fails_cleanly(
    db_session: Session, local_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tiny_timeout_settings = Settings(ingestion_clone_timeout_seconds=0.0001)
    monkeypatch.setattr(service_module, "get_settings", lambda: tiny_timeout_settings)

    identity = _local_identity(local_git_repo, owner="timeout-test", name="repo")
    with pytest.raises(IngestionError):
        ingest_identity(db_session, identity)

    repository = (
        db_session.query(Repository).filter_by(owner="timeout-test", name="repo").one_or_none()
    )
    assert repository is not None
    assert repository.status == "error"


def test_oversized_file_is_skipped_not_stored(
    db_session: Session, local_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """src/main.py is larger than a 5-byte limit; it must be classified
    UNSUPPORTED and skipped, never stored with truncated content."""
    tiny_file_limit = Settings(ingestion_max_file_bytes=5)
    monkeypatch.setattr(service_module, "get_settings", lambda: tiny_file_limit)

    identity = _local_identity(local_git_repo, owner="oversize-test", name="repo")
    result = ingest_identity(db_session, identity)

    files = (
        db_session.execute(select(SourceFile).where(SourceFile.revision_id == result.revision_id))
        .scalars()
        .all()
    )
    paths = {f.path for f in files}
    assert "src/main.py" not in paths  # too large under this limit, skipped entirely
    assert "README.md" not in paths  # also too large under a 5-byte limit


def test_content_budget_exceeded_fails_the_whole_ingestion(
    db_session: Session, local_git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A total-bytes budget too small for even the first file must fail the
    entire ingestion — not silently store a partial subset."""
    tiny_budget = Settings(ingestion_max_total_bytes=1)
    monkeypatch.setattr(service_module, "get_settings", lambda: tiny_budget)

    identity = _local_identity(local_git_repo, owner="budget-test", name="repo")
    with pytest.raises(IngestionError):
        ingest_identity(db_session, identity)

    repository = (
        db_session.query(Repository).filter_by(owner="budget-test", name="repo").one_or_none()
    )
    assert repository is not None
    assert repository.status == "error"

    revision = (
        db_session.query(RepositoryRevision).filter_by(repository_id=repository.id).one_or_none()
    )
    # The revision row itself is allowed to survive (it's cheap identity —
    # see repository-ingestion.md section 12) but must have zero files.
    if revision is not None:
        assert revision.latest_analysis_status == "failed"
        files = (
            db_session.execute(select(SourceFile).where(SourceFile.revision_id == revision.id))
            .scalars()
            .all()
        )
        assert files == []


def test_too_many_tracked_files_fails_before_any_content_is_read(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_dir = tmp_path / "many-files-repo"
    repo_dir.mkdir()
    run_git(repo_dir, "init", "-q", "-b", "main")
    for i in range(10):
        (repo_dir / f"file_{i}.txt").write_text("x\n")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "many files")

    tiny_file_count_limit = Settings(ingestion_max_files=5)
    monkeypatch.setattr(service_module, "get_settings", lambda: tiny_file_count_limit)

    identity = _local_identity(repo_dir, owner="toolarge-test", name="repo")
    with pytest.raises(IngestionError):
        ingest_identity(db_session, identity)

    repository = (
        db_session.query(Repository).filter_by(owner="toolarge-test", name="repo").one_or_none()
    )
    assert repository is not None
    assert repository.status == "error"
    # Discovery failed before any revision was created at all.
    assert repository.current_revision_id is None
