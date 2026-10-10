"""Hardening-style DB tests for app/documentation/discovery.py (Task 14).
Requires live PostgreSQL — same `db_session` fixture and explicit-skip
behavior as every other DB-dependent suite in this project.
"""

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.documentation.discovery import resolve_target
from app.models import SourceBlob, SourceFile, Symbol
from tests.automation.conftest import analyze_revision, ingest_revision, modify_and_commit
from tests.conftest import make_repo_and_revision


def test_repository_overview_resolves_with_no_target(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)

    resolution = resolve_target(
        db_session,
        repository_id,
        revision_id,
        "repository_overview",
        source_file_id=None,
        symbol_id=None,
    )
    assert resolution.status == "resolved"
    assert resolution.target_file_id is None
    assert resolution.target_symbol_id is None


def test_architecture_overview_with_unexpected_target_is_rejected(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)

    resolution = resolve_target(
        db_session,
        repository_id,
        revision_id,
        "architecture_overview",
        source_file_id=uuid.uuid4(),
        symbol_id=None,
    )
    assert resolution.status == "unexpected_target"


def test_module_documentation_without_file_id_is_missing_target(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    resolution = resolve_target(
        db_session,
        uuid.UUID(result.repository_id),
        uuid.UUID(result.revision_id),
        "module_documentation",
        source_file_id=None,
        symbol_id=None,
    )
    assert resolution.status == "missing_target"


def test_module_documentation_resolves_a_real_file(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    file_row = db_session.execute(
        select(SourceFile)
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == revision_id)
        .where(SourceFile.path == "app/math_utils.py")
    ).scalar_one()

    resolution = resolve_target(
        db_session,
        repository_id,
        revision_id,
        "module_documentation",
        source_file_id=file_row.id,
        symbol_id=None,
    )
    assert resolution.status == "resolved"
    assert resolution.target_file_id == file_row.id


def test_module_documentation_nonexistent_file_is_not_found(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    resolution = resolve_target(
        db_session,
        uuid.UUID(result.repository_id),
        uuid.UUID(result.revision_id),
        "module_documentation",
        source_file_id=uuid.uuid4(),
        symbol_id=None,
    )
    assert resolution.status == "not_found"


def test_module_documentation_file_from_another_repository_is_wrong_repository(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)

    other_repo, other_revision = make_repo_and_revision(db_session, "other-owner")
    blob = SourceBlob(content_hash="d" * 40, inline_content="x = 1", size_bytes=5)
    db_session.add(blob)
    db_session.flush()
    other_file = SourceFile(
        repository_id=other_repo.id,
        revision_id=other_revision.id,
        path="other.py",
        content_hash=blob.content_hash,
        size_bytes=5,
    )
    db_session.add(other_file)
    db_session.flush()

    resolution = resolve_target(
        db_session,
        repository_id,
        revision_id,
        "module_documentation",
        source_file_id=other_file.id,
        symbol_id=None,
    )
    assert resolution.status == "wrong_repository"


def test_module_documentation_file_from_a_different_revision_is_wrong_revision(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    first = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(first.repository_id)
    first_revision_id = uuid.UUID(first.revision_id)
    modify_and_commit(
        automation_fixture_repo, "app/math_utils.py", "def add(a, b):\n    return a + b\n", "x"
    )
    second = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    second_revision_id = uuid.UUID(second.revision_id)

    file_row = db_session.execute(
        select(SourceFile)
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == first_revision_id)
        .where(SourceFile.path == "app/math_utils.py")
    ).scalar_one()

    resolution = resolve_target(
        db_session,
        repository_id,
        second_revision_id,
        "module_documentation",
        source_file_id=file_row.id,
        symbol_id=None,
    )
    assert resolution.status == "wrong_revision"


def test_symbol_documentation_resolves_a_real_symbol(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    analyze_revision(db_session, repository_id, revision_id)

    symbol = db_session.execute(
        select(Symbol)
        .where(Symbol.repository_id == repository_id)
        .where(Symbol.revision_id == revision_id)
        .where(Symbol.qualified_name == "app.math_utils.add")
    ).scalar_one()

    resolution = resolve_target(
        db_session,
        repository_id,
        revision_id,
        "symbol_documentation",
        source_file_id=None,
        symbol_id=symbol.id,
    )
    assert resolution.status == "resolved"
    assert resolution.target_symbol_id == symbol.id


def test_symbol_documentation_without_symbol_id_is_missing_target(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    resolution = resolve_target(
        db_session,
        uuid.UUID(result.repository_id),
        uuid.UUID(result.revision_id),
        "symbol_documentation",
        source_file_id=None,
        symbol_id=None,
    )
    assert resolution.status == "missing_target"
