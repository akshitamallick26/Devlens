from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    InvestigationSession,
    Repository,
    RepositoryRevision,
    SessionContextRef,
    SourceFile,
    Symbol,
)


@pytest.fixture
def session_row(db_session: Session, repository: Repository) -> InvestigationSession:
    investigation = InvestigationSession(repository_id=repository.id, title="Payment refactor")
    db_session.add(investigation)
    db_session.flush()
    return investigation


def test_selected_file_with_only_file_id_succeeds(
    db_session: Session, session_row: InvestigationSession, source_file: SourceFile
) -> None:
    ref = SessionContextRef(
        session_id=session_row.id, ref_type="selected_file", file_id=source_file.id
    )
    db_session.add(ref)
    db_session.flush()  # must not raise


def test_selected_file_with_symbol_also_set_is_rejected(
    db_session: Session, session_row: InvestigationSession, source_file: SourceFile, symbol: Symbol
) -> None:
    """ref_type must match exactly the populated reference column — a
    contradictory combination (two references at once) must be rejected."""
    ref = SessionContextRef(
        session_id=session_row.id,
        ref_type="selected_file",
        file_id=source_file.id,
        symbol_id=symbol.id,
    )
    db_session.add(ref)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_selected_symbol_with_only_symbol_id_succeeds(
    db_session: Session, session_row: InvestigationSession, symbol: Symbol
) -> None:
    ref = SessionContextRef(
        session_id=session_row.id, ref_type="selected_symbol", symbol_id=symbol.id
    )
    db_session.add(ref)
    db_session.flush()  # must not raise


def test_graph_focus_without_camera_state_is_rejected(
    db_session: Session, session_row: InvestigationSession
) -> None:
    ref = SessionContextRef(session_id=session_row.id, ref_type="graph_focus")
    db_session.add(ref)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_graph_focus_with_camera_state_succeeds(
    db_session: Session, session_row: InvestigationSession
) -> None:
    ref = SessionContextRef(
        session_id=session_row.id,
        ref_type="graph_focus",
        graph_camera_state={"zoom": 1.5, "x": 100, "y": 200},
    )
    db_session.add(ref)
    db_session.flush()  # must not raise


def test_ref_type_with_no_reference_populated_is_rejected(
    db_session: Session, session_row: InvestigationSession
) -> None:
    ref = SessionContextRef(session_id=session_row.id, ref_type="selected_symbol")
    db_session.add(ref)
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_context_ref_may_point_at_an_older_revision_than_the_sessions_current_one(
    db_session: Session,
    session_row: InvestigationSession,
    repository: Repository,
    revision: RepositoryRevision,
    source_file: SourceFile,
    symbol: Symbol,
) -> None:
    """Category B (intentionally cross-revision), not a gap: a breadcrumb
    records what was true *at the time* — a session's investigation
    routinely spans multiple revisions as new commits land, so an older
    context ref pointing at an earlier revision's symbol is the expected
    case, not an error. See SessionContextRef's docstring and
    database-design.md."""
    newer_revision = RepositoryRevision(
        repository_id=repository.id, commit_sha="c" * 40, committed_at=datetime.now(UTC)
    )
    db_session.add(newer_revision)
    db_session.flush()
    session_row.current_revision_id = newer_revision.id
    db_session.flush()

    # `symbol` belongs to the older `revision`, not `newer_revision`.
    ref = SessionContextRef(
        session_id=session_row.id, ref_type="selected_symbol", symbol_id=symbol.id
    )
    db_session.add(ref)
    db_session.flush()  # must not raise — this is the intended behavior
