"""Parent-resolution tests. The root-commit and explicit-but-impossible
cases never touch the database (checked here, pure). The remaining cases
(Task 4's full re-audit matrix) need a real RepositoryRevision and are
DB-dependent — same `db_session` fixture and explicit-skip behavior as
every other DB-dependent suite. Never substitutes SQLite.
"""

import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.automation.parent_resolution import resolve_parent
from app.models import RepositoryRevision
from tests.automation.conftest import ingest_revision, modify_and_commit


class _ExplodingSession:
    """A fake Session that raises if touched — proves the root-commit and
    impossible-parent-request paths never issue a query."""

    def get(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("should not query the database for this case")

    def execute(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("should not query the database for this case")


def _revision(parent_sha: str | None, repository_id: uuid.UUID | None = None) -> RepositoryRevision:
    revision = RepositoryRevision()
    revision.id = uuid.uuid4()
    revision.repository_id = repository_id or uuid.uuid4()
    revision.commit_sha = "a" * 40
    revision.parent_sha = parent_sha
    return revision


def test_root_commit_with_no_requested_parent_is_no_parent() -> None:
    revision = _revision(parent_sha=None)
    fake_session: Session = _ExplodingSession()  # type: ignore[assignment]
    resolution = resolve_parent(fake_session, revision.repository_id, revision, None)
    assert resolution.status == "no_parent"
    assert resolution.parent_revision_id is None


def test_root_commit_with_a_requested_parent_is_mismatch() -> None:
    revision = _revision(parent_sha=None)
    fake_session: Session = _ExplodingSession()  # type: ignore[assignment]
    resolution = resolve_parent(fake_session, revision.repository_id, revision, uuid.uuid4())
    assert resolution.status == "mismatch"
    assert resolution.parent_revision_id is None


def test_requested_parent_from_another_repository_is_not_found(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    # Repository B ingests the first commit, so it holds a revision whose
    # commit_sha is exactly the current revision's parent_sha. The current
    # revision must not be a root commit: a root commit answers "mismatch"
    # for any requested parent without querying, so the ownership check
    # would never be reached.
    other = ingest_revision(db_session, automation_fixture_repo, owner="owner-b")
    modify_and_commit(
        automation_fixture_repo, "app/math_utils.py", "def add(a, b):\n    return a + b\n", "v2"
    )
    own = ingest_revision(db_session, automation_fixture_repo, owner="owner-a")

    own_revision = db_session.get(RepositoryRevision, uuid.UUID(own.revision_id))
    other_revision = db_session.get(RepositoryRevision, uuid.UUID(other.revision_id))
    assert own_revision is not None and other_revision is not None
    assert own_revision.parent_sha == other_revision.commit_sha
    assert own_revision.repository_id != other_revision.repository_id
    resolution = resolve_parent(
        db_session, uuid.UUID(own.repository_id), own_revision, uuid.UUID(other.revision_id)
    )
    assert resolution.status == "not_found"
    assert resolution.parent_revision_id is None


def test_requested_parent_with_wrong_commit_sha_is_mismatch(
    db_session: Session, automation_fixture_repo: Path
) -> None:

    v1 = ingest_revision(db_session, automation_fixture_repo, owner="owner-a")
    modify_and_commit(
        automation_fixture_repo, "app/math_utils.py", "def add(a, b):\n    return a + b\n", "v2"
    )
    ingest_revision(db_session, automation_fixture_repo, owner="owner-a")
    modify_and_commit(
        automation_fixture_repo,
        "app/math_utils.py",
        "def add(a, b):\n    return a + b + 1\n",
        "v3",
    )
    v3 = ingest_revision(db_session, automation_fixture_repo, owner="owner-a")

    v3_revision = db_session.get(RepositoryRevision, uuid.UUID(v3.revision_id))
    assert v3_revision is not None
    # v3's real parent is v2, not v1.
    resolution = resolve_parent(
        db_session, uuid.UUID(v3.repository_id), v3_revision, uuid.UUID(v1.revision_id)
    )
    assert resolution.status == "mismatch"
    assert resolution.parent_revision_id is None


def test_no_requested_parent_auto_resolves_the_real_parent(
    db_session: Session, automation_fixture_repo: Path
) -> None:

    parent = ingest_revision(db_session, automation_fixture_repo, owner="owner-a")
    modify_and_commit(
        automation_fixture_repo, "app/math_utils.py", "def add(a, b):\n    return a + b\n", "v2"
    )
    current = ingest_revision(db_session, automation_fixture_repo, owner="owner-a")

    current_revision = db_session.get(RepositoryRevision, uuid.UUID(current.revision_id))
    assert current_revision is not None
    resolution = resolve_parent(
        db_session, uuid.UUID(current.repository_id), current_revision, None
    )
    assert resolution.status == "resolved"
    assert resolution.parent_revision_id == uuid.UUID(parent.revision_id)


def test_no_requested_parent_but_parent_was_never_ingested_is_no_parent(
    db_session: Session, automation_fixture_repo: Path
) -> None:

    result = ingest_revision(db_session, automation_fixture_repo, owner="owner-a")
    revision = db_session.get(RepositoryRevision, uuid.UUID(result.revision_id))
    assert revision is not None
    # The fixture's very first commit is a root commit (parent_sha is None)
    # — simulate "parent exists in git history but was never ingested" by
    # forcing a parent_sha that no ingested revision's commit_sha matches.
    revision.parent_sha = "f" * 40
    resolution = resolve_parent(db_session, uuid.UUID(result.repository_id), revision, None)
    assert resolution.status == "no_parent"
    assert "never ingested" in (resolution.detail or "")
