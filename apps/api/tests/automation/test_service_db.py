"""End-to-end Automation Intelligence orchestration tests. Requires live
PostgreSQL — same `db_session` fixture and explicit-skip behavior as every
other DB-dependent suite. Never substitutes SQLite.
"""

import hashlib
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.automation import service
from app.models import CodeChunk, GeneratedTest, SourceBlob, Symbol
from tests.automation.conftest import (
    analyze_revision,
    delete_and_commit,
    ingest_revision,
    modify_and_commit,
)
from tests.ingestion.conftest import run_git


def test_first_revision_has_no_parent_and_reports_everything_added(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)

    run_result = service.run_automation_analysis(db_session, repository_id, revision_id)
    assert run_result.analysis_status == "completed"
    assert run_result.parent_revision_id is None
    assert len(run_result.file_delta.added) > 0
    assert run_result.file_delta.deleted == () and run_result.file_delta.modified == ()
    assert run_result.impact_summary is not None
    assert run_result.rag_invalidation.status == "rebuilt"
    assert run_result.security_invalidation.rerun_required is True
    assert any("no parent" in limitation for limitation in run_result.limitations)


def test_modified_file_produces_impact_on_its_direct_dependent(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    parent_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    analyze_revision(
        db_session, uuid.UUID(parent_result.repository_id), uuid.UUID(parent_result.revision_id)
    )

    modify_and_commit(
        automation_fixture_repo,
        "app/math_utils.py",
        "def add(a, b):\n    return a + b + 0\n\n\ndef unused():\n    return 1\n",
        "modify add",
    )
    current_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    repository_id = uuid.UUID(current_result.repository_id)
    revision_id = uuid.UUID(current_result.revision_id)

    run_result = service.run_automation_analysis(db_session, repository_id, revision_id)
    assert run_result.analysis_status == "completed"
    assert run_result.parent_revision_id == uuid.UUID(parent_result.revision_id)

    modified_paths = {current.path for _, current in run_result.file_delta.modified}
    assert "app/math_utils.py" in modified_paths

    modified_names = {
        current.identity.qualified_name for _, current in run_result.symbol_delta.modified
    }
    assert "app.math_utils.add" in modified_names
    # `unused` is byte-identical before/after — must not show up as modified.
    assert "app.math_utils.unused" not in modified_names

    assert run_result.impact_summary is not None
    direct_names = {s.qualified_name for s in run_result.impact_summary.direct}
    assert "app.caller.compute" in direct_names  # compute() calls add() directly


def test_added_file_has_no_impact_since_nothing_depends_on_it_yet(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    parent_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    analyze_revision(
        db_session, uuid.UUID(parent_result.repository_id), uuid.UUID(parent_result.revision_id)
    )

    (automation_fixture_repo / "app" / "new_module.py").write_text("def new_fn():\n    return 1\n")
    run_git(automation_fixture_repo, "add", "-A")
    run_git(automation_fixture_repo, "commit", "-q", "-m", "add new_module")
    current_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")

    run_result = service.run_automation_analysis(
        db_session, uuid.UUID(current_result.repository_id), uuid.UUID(current_result.revision_id)
    )
    added_names = {s.identity.qualified_name for s in run_result.symbol_delta.added}
    assert "app.new_module.new_fn" in added_names
    assert run_result.impact_summary is not None
    assert run_result.impact_summary.direct == ()
    assert run_result.impact_summary.transitive == ()


def test_deleted_file_invalidates_its_generated_tests(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    parent_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    parent_repository_id = uuid.UUID(parent_result.repository_id)
    parent_revision_id = uuid.UUID(parent_result.revision_id)
    analyze_revision(db_session, parent_repository_id, parent_revision_id)

    add_symbol = db_session.execute(
        select(Symbol)
        .where(Symbol.revision_id == parent_revision_id)
        .where(Symbol.qualified_name == "app.math_utils.add")
    ).scalar_one()

    source = "def test_add():\n    assert True\n"
    content_hash = hashlib.sha256(source.encode()).hexdigest()
    db_session.add(
        SourceBlob(content_hash=content_hash, inline_content=source, size_bytes=len(source))
    )
    db_session.flush()
    generated_test = GeneratedTest(
        repository_id=parent_repository_id,
        revision_id=parent_revision_id,
        target_symbol_id=add_symbol.id,
        proposed_path="test_add.py",
        content_hash=content_hash,
        language="python",
        status="ready",
    )
    db_session.add(generated_test)
    db_session.flush()

    delete_and_commit(automation_fixture_repo, "app/math_utils.py", "remove math_utils")
    current_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")

    run_result = service.run_automation_analysis(
        db_session, uuid.UUID(current_result.repository_id), uuid.UUID(current_result.revision_id)
    )
    assert generated_test.id in run_result.testing_invalidation.stale_generated_test_ids


def test_parent_mismatch_raises_a_typed_error(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    v1 = ingest_revision(db_session, automation_fixture_repo, owner="owner-a")
    modify_and_commit(
        automation_fixture_repo, "app/math_utils.py", "def add(a, b):\n    return a + b\n", "second"
    )
    ingest_revision(db_session, automation_fixture_repo, owner="owner-a")
    modify_and_commit(
        automation_fixture_repo,
        "app/math_utils.py",
        "def add(a, b):\n    return a + b + 1\n",
        "third",
    )
    v3 = ingest_revision(db_session, automation_fixture_repo, owner="owner-a")

    # v3's real parent is v2, not v1 — supplying v1 must be rejected as a
    # mismatch, never silently accepted just because both UUIDs are valid
    # revisions of the same repository.
    with pytest.raises(service.ParentVerificationError) as exc_info:
        service.run_automation_analysis(
            db_session,
            uuid.UUID(v3.repository_id),
            uuid.UUID(v3.revision_id),
            uuid.UUID(v1.revision_id),
        )
    assert exc_info.value.resolution.status == "mismatch"


def test_revision_not_found_is_explicit(db_session: Session, automation_fixture_repo: Path) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    with pytest.raises(service.RevisionNotFoundError):
        service.run_automation_analysis(db_session, uuid.UUID(result.repository_id), uuid.uuid4())


def test_running_twice_does_not_duplicate_derived_rows(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)

    service.run_automation_analysis(db_session, repository_id, revision_id)
    first_symbol_count = db_session.scalar(
        select(func.count()).select_from(Symbol).where(Symbol.revision_id == revision_id)
    )
    first_chunk_count = db_session.scalar(
        select(func.count()).select_from(CodeChunk).where(CodeChunk.revision_id == revision_id)
    )

    service.run_automation_analysis(db_session, repository_id, revision_id)
    second_symbol_count = db_session.scalar(
        select(func.count()).select_from(Symbol).where(Symbol.revision_id == revision_id)
    )
    second_chunk_count = db_session.scalar(
        select(func.count()).select_from(CodeChunk).where(CodeChunk.revision_id == revision_id)
    )

    assert first_symbol_count == second_symbol_count  # delete-and-rebuild, never accumulating
    assert first_chunk_count == second_chunk_count


def test_cyclic_graph_does_not_hang_and_excludes_seeds_from_their_own_impact(
    db_session: Session, tmp_path: Path
) -> None:
    repo_dir = tmp_path / "automation-cyclic-repo"
    repo_dir.mkdir()
    (repo_dir / "mutual.py").write_text(
        "def is_even(n):\n"
        "    if n == 0:\n        return True\n"
        "    return is_odd(n - 1)\n\n\n"
        "def is_odd(n):\n"
        "    if n == 0:\n        return False\n"
        "    return is_even(n - 1)\n"
    )
    run_git(repo_dir, "init", "-q", "-b", "main")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "cyclic initial")
    parent_result = ingest_revision(db_session, repo_dir, owner="auto-test")
    analyze_revision(
        db_session, uuid.UUID(parent_result.repository_id), uuid.UUID(parent_result.revision_id)
    )

    modify_and_commit(
        repo_dir,
        "mutual.py",
        "def is_even(n):\n"
        "    if n == 0:\n        return True\n"
        "    return is_odd(n - 1)\n\n\n"
        "def is_odd(n):\n"
        "    if n == 0:\n        return False\n"
        "    return is_even(n - 1) or False\n",
        "modify is_odd",
    )
    current_result = ingest_revision(db_session, repo_dir, owner="auto-test")

    run_result = service.run_automation_analysis(
        db_session, uuid.UUID(current_result.repository_id), uuid.UUID(current_result.revision_id)
    )
    assert run_result.analysis_status == "completed"
    modified_names = {
        current.identity.qualified_name for _, current in run_result.symbol_delta.modified
    }
    assert "mutual.is_odd" in modified_names

    assert run_result.impact_summary is not None
    impacted_names = {s.qualified_name for s in run_result.impact_summary.direct}
    impacted_names |= {s.qualified_name for s in run_result.impact_summary.transitive}
    # `is_odd` calls `is_even` which calls `is_odd` — a real cycle. The
    # changed seed itself must never appear in its own impact set.
    assert "mutual.is_odd" not in impacted_names
    assert "mutual.is_even" in impacted_names
