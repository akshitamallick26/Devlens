"""Hardening Blocker 2 tests: execution strategy is always explicit and
distinct from the invalidation decision. Requires live PostgreSQL — same
`db_session` fixture and explicit-skip behavior as every other DB-dependent
suite. Never substitutes SQLite.
"""

import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.automation import service
from tests.automation.conftest import analyze_revision, ingest_revision, modify_and_commit
from tests.ingestion.conftest import run_git


def test_unchanged_revision_still_reports_full_rebuild_not_selective_skip(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    """An empty commit (no tracked file content changes at all) against its
    real, verified parent has `invalidated=False` for every stage, but
    `execution_strategy` must still read "full_revision_rebuild" — never
    implying files were selectively reparsed/skipped, since the underlying
    services have no such mode."""
    parent_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    analyze_revision(
        db_session, uuid.UUID(parent_result.repository_id), uuid.UUID(parent_result.revision_id)
    )

    run_git(automation_fixture_repo, "commit", "-q", "--allow-empty", "-m", "no-op commit")
    current_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    repository_id = uuid.UUID(current_result.repository_id)
    revision_id = uuid.UUID(current_result.revision_id)

    run_result = service.run_automation_analysis(db_session, repository_id, revision_id)

    assert run_result.parent_revision_id == uuid.UUID(parent_result.revision_id)
    assert run_result.file_delta.added == ()
    assert run_result.file_delta.modified == ()
    assert run_result.file_delta.deleted == ()
    assert len(run_result.file_delta.unchanged) > 0
    assert run_result.parsing_execution.invalidated is False
    assert run_result.graph_execution.invalidated is False

    assert run_result.parsing_execution.execution_strategy == "full_revision_rebuild"
    assert run_result.graph_execution.execution_strategy == "full_revision_rebuild"
    assert run_result.rag_invalidation.execution_strategy == "full_revision_rebuild"
    assert run_result.parsing_execution.status == "completed"
    assert run_result.graph_execution.status == "completed"
    assert any("full-revision rebuilds" in limitation for limitation in run_result.limitations)


def test_invalidated_reflects_real_change_detection_not_execution(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    parent_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    analyze_revision(
        db_session, uuid.UUID(parent_result.repository_id), uuid.UUID(parent_result.revision_id)
    )
    modify_and_commit(
        automation_fixture_repo,
        "app/math_utils.py",
        "def add(a, b):\n    return a + b + 1\n\n\ndef unused():\n    return 1\n",
        "modify add",
    )
    current_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")

    run_result = service.run_automation_analysis(
        db_session, uuid.UUID(current_result.repository_id), uuid.UUID(current_result.revision_id)
    )

    assert run_result.parsing_execution.invalidated is True
    assert run_result.graph_execution.invalidated is True
    # Still the same, constant execution strategy regardless of the decision.
    assert run_result.parsing_execution.execution_strategy == "full_revision_rebuild"
    assert run_result.graph_execution.execution_strategy == "full_revision_rebuild"
