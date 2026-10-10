"""Hardening Investigation Sessions v1: context-reference tests (Task 7/8/17
— typed/bounded references, repository consistency, duplicate prevention).
Requires live PostgreSQL — same `db_session`/`repository`/`revision`/
`source_file`/`symbol`/`analysis_run` fixtures and explicit-skip behavior as
every other DB-dependent suite in this project.
"""

import uuid

import pytest
from sqlalchemy.orm import Session

from app.investigation import service
from app.models import (
    AnalysisRun,
    Repository,
    RepositoryRevision,
    SecurityFinding,
    SourceBlob,
    SourceFile,
    Symbol,
)
from tests.conftest import make_repo_and_revision


def test_add_selected_file_context(
    db_session: Session, repository: Repository, source_file: SourceFile
) -> None:
    created = service.create_session(db_session, repository.id)
    ref = service.add_context_ref(
        db_session, repository.id, created.id, "selected_file", file_id=source_file.id
    )
    assert ref.ref_type == "selected_file"
    assert ref.file_id == source_file.id


def test_add_selected_symbol_context(
    db_session: Session, repository: Repository, symbol: Symbol
) -> None:
    created = service.create_session(db_session, repository.id)
    ref = service.add_context_ref(
        db_session, repository.id, created.id, "selected_symbol", symbol_id=symbol.id
    )
    assert ref.ref_type == "selected_symbol"
    assert ref.symbol_id == symbol.id


def test_add_analysis_reference_context(
    db_session: Session, repository: Repository, analysis_run: AnalysisRun
) -> None:
    created = service.create_session(db_session, repository.id)
    ref = service.add_context_ref(
        db_session,
        repository.id,
        created.id,
        "analysis_reference",
        analysis_run_id=analysis_run.id,
    )
    assert ref.ref_type == "analysis_reference"
    assert ref.analysis_run_id == analysis_run.id


def test_add_finding_reference_context(
    db_session: Session, repository: Repository, security_finding: SecurityFinding
) -> None:
    created = service.create_session(db_session, repository.id)
    ref = service.add_context_ref(
        db_session, repository.id, created.id, "finding_reference", finding_id=security_finding.id
    )
    assert ref.ref_type == "finding_reference"
    assert ref.finding_id == security_finding.id


def test_add_graph_focus_context(db_session: Session, repository: Repository) -> None:
    created = service.create_session(db_session, repository.id)
    ref = service.add_context_ref(
        db_session,
        repository.id,
        created.id,
        "graph_focus",
        graph_camera_state={"zoom": 1.5, "x": 10, "y": 20},
    )
    assert ref.ref_type == "graph_focus"
    assert ref.graph_camera_state == {"zoom": 1.5, "x": 10, "y": 20}


def test_selected_file_without_file_id_is_rejected(
    db_session: Session, repository: Repository
) -> None:
    created = service.create_session(db_session, repository.id)
    with pytest.raises(service.InvalidContextRefError):
        service.add_context_ref(db_session, repository.id, created.id, "selected_file")


def test_adding_the_same_file_reference_twice_is_idempotent_not_duplicated(
    db_session: Session, repository: Repository, source_file: SourceFile
) -> None:
    created = service.create_session(db_session, repository.id)
    first = service.add_context_ref(
        db_session, repository.id, created.id, "selected_file", file_id=source_file.id
    )
    second = service.add_context_ref(
        db_session, repository.id, created.id, "selected_file", file_id=source_file.id
    )
    assert first.id == second.id

    snapshot = service.get_session(db_session, repository.id, created.id)
    matching = [c for c in snapshot.context_refs if c.file_id == source_file.id]
    assert len(matching) == 1


def test_referencing_a_file_from_another_repository_is_rejected(
    db_session: Session, repository: Repository
) -> None:
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

    created = service.create_session(db_session, repository.id)
    with pytest.raises(service.CrossRepositoryReferenceError):
        service.add_context_ref(
            db_session, repository.id, created.id, "selected_file", file_id=other_file.id
        )


def test_referencing_a_symbol_from_a_different_repositorys_revision_is_the_wrong_revision_case(
    db_session: Session, repository: Repository
) -> None:
    """A symbol genuinely belonging to a different repository's revision is
    the concrete "wrong revision reference" rejection case (section 7): for
    revision-scoped entities, the repository check *is* the full revision-
    consistency check available, since a revision belongs to exactly one
    repository. Contrast with the next test, which exercises the
    deliberately-ALLOWED same-repository historical case."""
    other_repo, other_revision = make_repo_and_revision(db_session, "other-owner")
    # A real file in the other revision: symbols(revision_id, file_id) is a
    # composite FK to source_files, so a random file_id never reaches the
    # service at all.
    blob = SourceBlob(content_hash="e" * 40, inline_content="def f(): ...", size_bytes=12)
    db_session.add(blob)
    db_session.flush()
    other_file = SourceFile(
        repository_id=other_repo.id,
        revision_id=other_revision.id,
        path="other.py",
        content_hash=blob.content_hash,
        size_bytes=12,
    )
    db_session.add(other_file)
    db_session.flush()
    other_symbol = Symbol(
        repository_id=other_repo.id,
        revision_id=other_revision.id,
        file_id=other_file.id,
        kind="function",
        name="f",
        qualified_name="other.f",
        start_line=1,
        end_line=2,
        analyzer="tree-sitter-python@0.21",
    )
    db_session.add(other_symbol)
    db_session.flush()

    created = service.create_session(db_session, repository.id)
    with pytest.raises(service.CrossRepositoryReferenceError):
        service.add_context_ref(
            db_session, repository.id, created.id, "selected_symbol", symbol_id=other_symbol.id
        )
    assert service.get_session(db_session, repository.id, created.id).context_refs == ()


def test_referencing_a_symbol_from_an_older_revision_of_the_same_repository_is_accepted(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    symbol: Symbol,
) -> None:
    """Deliberately preserved precedent (see
    tests/db/test_session_context.py and
    docs/architecture/investigation-sessions.md section 5): a breadcrumb
    pointing at an older revision than the session's current one is the
    intended, legitimate case — not a gap to close."""
    newer_revision = RepositoryRevision(
        repository_id=repository.id,
        commit_sha="e" * 40,
        parent_sha=revision.commit_sha,
        committed_at=revision.committed_at,
    )
    db_session.add(newer_revision)
    db_session.flush()

    created = service.create_session(db_session, repository.id, newer_revision.id)
    ref = service.add_context_ref(
        db_session, repository.id, created.id, "selected_symbol", symbol_id=symbol.id
    )
    assert ref.symbol_id == symbol.id  # must not raise — this is expected behavior


def test_referencing_a_nonexistent_entity_raises_not_found(
    db_session: Session, repository: Repository
) -> None:
    created = service.create_session(db_session, repository.id)
    with pytest.raises(service.ReferencedEntityNotFoundError):
        service.add_context_ref(
            db_session, repository.id, created.id, "selected_file", file_id=uuid.uuid4()
        )


def test_context_ref_with_message_from_a_different_session_is_rejected(
    db_session: Session, repository: Repository, source_file: SourceFile
) -> None:
    session_a = service.create_session(db_session, repository.id)
    session_b = service.create_session(db_session, repository.id)
    message_in_b = service.append_message(db_session, repository.id, session_b.id, "user", "hi")

    with pytest.raises(service.CrossSessionReferenceError):
        service.add_context_ref(
            db_session,
            repository.id,
            session_a.id,
            "selected_file",
            file_id=source_file.id,
            message_id=message_in_b.id,
        )


def test_remove_context_ref(
    db_session: Session, repository: Repository, source_file: SourceFile
) -> None:
    created = service.create_session(db_session, repository.id)
    ref = service.add_context_ref(
        db_session, repository.id, created.id, "selected_file", file_id=source_file.id
    )
    service.remove_context_ref(db_session, repository.id, created.id, ref.id)

    snapshot = service.get_session(db_session, repository.id, created.id)
    assert snapshot.context_refs == ()


def test_remove_nonexistent_context_ref_raises(db_session: Session, repository: Repository) -> None:
    created = service.create_session(db_session, repository.id)
    with pytest.raises(service.ContextRefNotFoundError):
        service.remove_context_ref(db_session, repository.id, created.id, uuid.uuid4())
