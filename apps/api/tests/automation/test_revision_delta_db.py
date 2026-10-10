"""DB-dependent file-delta tests. Requires live PostgreSQL — same
`db_session` fixture and explicit-skip behavior as every other DB-dependent
suite in this project. Never substitutes SQLite.
"""

import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.automation.revision_delta import compute_file_delta
from tests.automation.conftest import delete_and_commit, ingest_revision, modify_and_commit
from tests.ingestion.conftest import run_git


def test_no_changes_between_identical_revisions_is_empty_delta(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    # Ingesting without any new commit just re-ingests the same HEAD — the
    # service layer creates the Repository once and reuses it; ingesting
    # "twice" with no new commit in between hits the UniqueConstraint on
    # (repository_id, commit_sha), so instead we compare a revision to
    # itself directly to exercise the zero-delta path deterministically.
    result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)

    delta = compute_file_delta(db_session, repository_id, revision_id, revision_id)
    assert delta.added == ()
    assert delta.deleted == ()
    assert delta.modified == ()
    assert len(delta.unchanged) > 0
    assert delta.rename_detection == "unsupported"


def test_no_parent_reports_every_file_as_added(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)

    delta = compute_file_delta(db_session, repository_id, revision_id, None)
    assert len(delta.added) > 0
    assert delta.deleted == ()
    assert delta.modified == ()
    assert delta.unchanged == ()


def test_added_file_is_detected(db_session: Session, automation_fixture_repo: Path) -> None:
    parent_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    (automation_fixture_repo / "app" / "new_module.py").write_text("def new_fn():\n    return 1\n")
    run_git(automation_fixture_repo, "add", "-A")
    run_git(automation_fixture_repo, "commit", "-q", "-m", "add new_module")
    current_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")

    delta = compute_file_delta(
        db_session,
        uuid.UUID(current_result.repository_id),
        uuid.UUID(current_result.revision_id),
        uuid.UUID(parent_result.revision_id),
    )
    assert any(e.path == "app/new_module.py" for e in delta.added)


def test_deleted_file_is_detected(db_session: Session, automation_fixture_repo: Path) -> None:
    parent_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    delete_and_commit(automation_fixture_repo, "app/math_utils.py", "remove math_utils")
    current_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")

    delta = compute_file_delta(
        db_session,
        uuid.UUID(current_result.repository_id),
        uuid.UUID(current_result.revision_id),
        uuid.UUID(parent_result.revision_id),
    )
    assert any(e.path == "app/math_utils.py" for e in delta.deleted)


def test_modified_file_is_detected(db_session: Session, automation_fixture_repo: Path) -> None:
    parent_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    modify_and_commit(
        automation_fixture_repo,
        "app/math_utils.py",
        "def add(a, b):\n    return a + b + 0\n\n\ndef unused():\n    return 1\n",
        "modify add",
    )
    current_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")

    delta = compute_file_delta(
        db_session,
        uuid.UUID(current_result.repository_id),
        uuid.UUID(current_result.revision_id),
        uuid.UUID(parent_result.revision_id),
    )
    modified_paths = {current.path for _, current in delta.modified}
    assert "app/math_utils.py" in modified_paths
    assert any(e.path == "app/caller.py" for e in delta.unchanged)


def test_cross_repository_isolation(db_session: Session, automation_fixture_repo: Path) -> None:
    first = ingest_revision(db_session, automation_fixture_repo, owner="owner-a")
    second = ingest_revision(db_session, automation_fixture_repo, owner="owner-b")

    delta = compute_file_delta(
        db_session,
        uuid.UUID(first.repository_id),
        uuid.UUID(first.revision_id),
        uuid.UUID(second.revision_id),
    )
    # second's revision belongs to a different repository entirely, so
    # nothing in it should ever be treated as "parent" content for first.
    assert len(delta.added) > 0  # everything in `first` looks added, not matched
