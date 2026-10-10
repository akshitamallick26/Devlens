"""Hardening-style DB tests for app/documentation/persistence.py (Task 12 —
idempotency/uniqueness). Requires live PostgreSQL — same `db_session`
fixture and explicit-skip behavior as every other DB-dependent suite.
"""

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.documentation.context import assemble_module_context, assemble_repository_overview_context
from app.documentation.fallback import generate_fallback_draft
from app.documentation.persistence import start_run, upsert_artifact
from app.models import GeneratedArtifact, Repository, RepositoryRevision, SourceFile
from tests.automation.conftest import ingest_revision, modify_and_commit


def _repo_and_revision(
    db_session: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> tuple[Repository, RepositoryRevision]:
    repository = db_session.get(Repository, repository_id)
    revision = db_session.get(RepositoryRevision, revision_id)
    assert repository is not None and revision is not None
    return repository, revision


def _rows(db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID) -> list[GeneratedArtifact]:
    return list(
        db.execute(
            select(GeneratedArtifact)
            .where(GeneratedArtifact.repository_id == repository_id)
            .where(GeneratedArtifact.revision_id == revision_id)
            .where(GeneratedArtifact.kind == "documentation")
        )
        .scalars()
        .all()
    )


def test_repeated_generation_for_the_same_identity_upserts_one_row(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    repository, revision = _repo_and_revision(db_session, repository_id, revision_id)
    package = assemble_repository_overview_context(db_session, repository, revision)
    draft = generate_fallback_draft(package)

    run1 = start_run(db_session, repository_id, revision_id, "repository_overview", None, None)
    artifact1 = upsert_artifact(
        db_session, package, draft, "deterministic_fallback", "", "", "", run1.id
    )
    run2 = start_run(db_session, repository_id, revision_id, "repository_overview", None, None)
    artifact2 = upsert_artifact(
        db_session, package, draft, "deterministic_fallback", "", "", "", run2.id
    )

    assert artifact1.id == artifact2.id
    rows = _rows(db_session, repository_id, revision_id)
    assert len(rows) == 1
    assert rows[0].analysis_run_id == run2.id  # updated to the latest successful run


def test_regeneration_replaces_content_not_duplicates(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    repository, revision = _repo_and_revision(db_session, repository_id, revision_id)
    package = assemble_repository_overview_context(db_session, repository, revision)
    draft = generate_fallback_draft(package)

    run = start_run(db_session, repository_id, revision_id, "repository_overview", None, None)
    artifact = upsert_artifact(
        db_session, package, draft, "deterministic_fallback", "", "", "", run.id
    )
    original_updated_at = artifact.updated_at

    run2 = start_run(db_session, repository_id, revision_id, "repository_overview", None, None)
    updated = upsert_artifact(
        db_session, package, draft, "llm_generated", "anthropic", "claude-x", "", run2.id
    )
    assert updated.id == artifact.id
    assert updated.generation_method == "llm_generated"
    assert updated.updated_at >= original_updated_at


def test_module_documentation_identity_is_scoped_by_target_file(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    result = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(result.repository_id)
    revision_id = uuid.UUID(result.revision_id)
    repository, revision = _repo_and_revision(db_session, repository_id, revision_id)

    file_a = db_session.execute(
        select(SourceFile)
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == revision_id)
        .where(SourceFile.path == "app/math_utils.py")
    ).scalar_one()
    file_b = db_session.execute(
        select(SourceFile)
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == revision_id)
        .where(SourceFile.path == "app/caller.py")
    ).scalar_one()

    package_a = assemble_module_context(db_session, repository, revision, file_a)
    package_b = assemble_module_context(db_session, repository, revision, file_b)
    run_a = start_run(
        db_session, repository_id, revision_id, "module_documentation", file_a.id, None
    )
    run_b = start_run(
        db_session, repository_id, revision_id, "module_documentation", file_b.id, None
    )
    artifact_a = upsert_artifact(
        db_session,
        package_a,
        generate_fallback_draft(package_a),
        "deterministic_fallback",
        "",
        "",
        "",
        run_a.id,
    )
    artifact_b = upsert_artifact(
        db_session,
        package_b,
        generate_fallback_draft(package_b),
        "deterministic_fallback",
        "",
        "",
        "",
        run_b.id,
    )

    assert artifact_a.id != artifact_b.id
    rows = _rows(db_session, repository_id, revision_id)
    assert len(rows) == 2


def test_different_revisions_get_separate_artifact_rows(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    first = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    repository_id = uuid.UUID(first.repository_id)
    first_revision_id = uuid.UUID(first.revision_id)
    repository, first_revision = _repo_and_revision(db_session, repository_id, first_revision_id)
    package1 = assemble_repository_overview_context(db_session, repository, first_revision)
    run1 = start_run(
        db_session, repository_id, first_revision_id, "repository_overview", None, None
    )
    artifact1 = upsert_artifact(
        db_session,
        package1,
        generate_fallback_draft(package1),
        "deterministic_fallback",
        "",
        "",
        "",
        run1.id,
    )

    modify_and_commit(
        automation_fixture_repo, "app/math_utils.py", "def add(a, b):\n    return a + b\n", "x"
    )
    second = ingest_revision(db_session, automation_fixture_repo, owner="doc-test")
    second_revision_id = uuid.UUID(second.revision_id)
    _repo2, second_revision = _repo_and_revision(db_session, repository_id, second_revision_id)
    package2 = assemble_repository_overview_context(db_session, repository, second_revision)
    run2 = start_run(
        db_session, repository_id, second_revision_id, "repository_overview", None, None
    )
    artifact2 = upsert_artifact(
        db_session,
        package2,
        generate_fallback_draft(package2),
        "deterministic_fallback",
        "",
        "",
        "",
        run2.id,
    )

    assert artifact1.id != artifact2.id
    assert len(_rows(db_session, repository_id, first_revision_id)) == 1
    assert len(_rows(db_session, repository_id, second_revision_id)) == 1
    # R1's row must be untouched by R2's generation.
    refreshed_artifact1 = db_session.get(GeneratedArtifact, artifact1.id)
    assert refreshed_artifact1 is not None
    assert refreshed_artifact1.revision_id == first_revision_id
