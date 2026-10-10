"""Hardening-style DB tests for app/documentation/context.py (Task 4/6/19).
Requires live PostgreSQL — same `db_session` fixture and explicit-skip
behavior as every other DB-dependent suite in this project.
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.documentation.context as context_module
from app.documentation.context import (
    assemble_architecture_overview_context,
    assemble_module_context,
    assemble_repository_overview_context,
    assemble_symbol_context,
)
from app.models import Repository, RepositoryRevision, SourceFile, Symbol
from tests.automation.conftest import analyze_revision, ingest_revision


def _repo_and_revision(
    db_session: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> tuple[Repository, RepositoryRevision]:
    repository = db_session.get(Repository, repository_id)
    revision = db_session.get(RepositoryRevision, revision_id)
    assert repository is not None and revision is not None
    return repository, revision


def test_repository_overview_context_is_deterministic(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    analyze_revision(db_session, repository_id, revision_id)
    repository, revision = _repo_and_revision(db_session, repository_id, revision_id)

    package_a = assemble_repository_overview_context(db_session, repository, revision)
    package_b = assemble_repository_overview_context(db_session, repository, revision)

    assert package_a.facts == package_b.facts
    assert [e.evidence_id for e in package_a.evidence.items] == [
        e.evidence_id for e in package_b.evidence.items
    ]
    assert package_a.facts.file_count > 0
    assert package_a.facts.symbol_count > 0


def test_repository_overview_context_never_leaks_outside_repository_or_revision(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    analyze_revision(db_session, repository_id, revision_id)
    repository, revision = _repo_and_revision(db_session, repository_id, revision_id)

    package = assemble_repository_overview_context(db_session, repository, revision)
    for item in package.evidence.items:
        assert item.repository_id == repository_id
        assert item.revision_id == revision_id


def test_architecture_overview_context_ranks_key_symbols_by_graph_degree(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    analyze_revision(db_session, repository_id, revision_id)
    repository, revision = _repo_and_revision(db_session, repository_id, revision_id)

    package = assemble_architecture_overview_context(db_session, repository, revision)
    qualified_names = {s.qualified_name for s in package.facts.key_symbols}
    assert "app.math_utils.add" in qualified_names  # called by app.caller.compute


def test_module_documentation_context_lists_symbols_and_dependents(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    analyze_revision(db_session, repository_id, revision_id)
    repository, revision = _repo_and_revision(db_session, repository_id, revision_id)

    target_file = db_session.execute(
        select(SourceFile)
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == revision_id)
        .where(SourceFile.path == "app/math_utils.py")
    ).scalar_one()

    package = assemble_module_context(db_session, repository, revision, target_file)
    assert package.facts.file_path == "app/math_utils.py"
    symbol_names = {s.qualified_name for s in package.facts.symbols}
    assert "app.math_utils.add" in symbol_names
    assert "app/caller.py" in package.facts.inbound_dependents


def test_symbol_documentation_context_finds_real_caller(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    analyze_revision(db_session, repository_id, revision_id)
    repository, revision = _repo_and_revision(db_session, repository_id, revision_id)

    target_symbol = db_session.execute(
        select(Symbol)
        .where(Symbol.repository_id == repository_id)
        .where(Symbol.revision_id == revision_id)
        .where(Symbol.qualified_name == "app.math_utils.add")
    ).scalar_one()

    package = assemble_symbol_context(
        db_session, repository, revision, target_symbol, "app/math_utils.py"
    )
    assert package.facts.qualified_name == "app.math_utils.add"
    caller_names = {c.qualified_name for c in package.facts.callers}
    assert "app.caller.compute" in caller_names
    # The target's own source excerpt must be evidence D1 (target-specific
    # evidence first — Task 6's deterministic prioritization).
    assert package.evidence.items[0].symbol_id == target_symbol.id


def test_evidence_item_count_budget_is_enforced_and_marked_truncated(
    db_session: Session, automation_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    analyze_revision(db_session, repository_id, revision_id)
    repository, revision = _repo_and_revision(db_session, repository_id, revision_id)

    monkeypatch.setattr(context_module, "MAX_EVIDENCE_ITEMS", 0)
    # With the budget forced to zero, no evidence item is kept even though
    # key symbols exist — architecture overview always attempts at least
    # one symbol-source evidence item when key symbols are found.
    package = assemble_architecture_overview_context(db_session, repository, revision)
    assert package.evidence.items == ()
    assert package.truncated is True


def test_per_item_source_truncation_is_marked(
    db_session: Session, automation_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    analyze_revision(db_session, repository_id, revision_id)
    repository, revision = _repo_and_revision(db_session, repository_id, revision_id)

    monkeypatch.setattr(context_module, "MAX_SOURCE_CHARS_PER_ITEM", 1)
    package = assemble_architecture_overview_context(db_session, repository, revision)
    assert package.truncated is True
    for item in package.evidence.items:
        if item.content is not None:
            assert len(item.content) <= 1
