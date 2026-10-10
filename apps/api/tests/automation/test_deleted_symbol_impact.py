"""Hardening Blocker 3 tests: a deleted symbol's impact must never look
like zero impact. Pure test (no DB access, no graph traversal of any
kind — verified structurally) plus a DB-dependent end-to-end test for the
exact scenario the hardening request specifies.
"""

import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.automation import service
from app.automation.impact_reuse import compute_deleted_symbol_impacts
from app.automation.models import SymbolIdentity, SymbolSnapshot
from tests.automation.conftest import analyze_revision, delete_and_commit, ingest_revision


def _snap(name: str) -> SymbolSnapshot:
    return SymbolSnapshot(
        identity=SymbolIdentity(file_path="a.py", kind="function", qualified_name=name),
        symbol_id=uuid.uuid4(),
        file_id=uuid.uuid4(),
        start_line=1,
        end_line=2,
        content="x",
    )


def test_deleted_symbol_impact_is_never_zero_and_never_computed() -> None:
    """Pure: no database session is passed at all, structurally proving no
    traversal of any graph — parent or current — ever happens for a
    deleted symbol."""
    deleted = (_snap("a.deleted_function"),)
    impacts = compute_deleted_symbol_impacts(deleted)
    assert len(impacts) == 1
    entry = impacts[0]
    assert entry.impact_status == "unavailable_deleted_symbol"
    assert entry.symbol.identity.qualified_name == "a.deleted_function"
    assert "parent revision's graph" in entry.reason


def test_empty_deleted_set_produces_empty_impacts() -> None:
    assert compute_deleted_symbol_impacts(()) == ()


def test_end_to_end_deleted_function_with_a_caller_reports_unavailable_not_zero(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    """Exact scenario from the hardening request: parent revision has
    caller -> deleted_function (app.caller.compute calls
    app.math_utils.add); current revision removes math_utils.py entirely.
    """
    parent_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")
    analyze_revision(
        db_session, uuid.UUID(parent_result.repository_id), uuid.UUID(parent_result.revision_id)
    )

    delete_and_commit(automation_fixture_repo, "app/math_utils.py", "remove math_utils")
    current_result = ingest_revision(db_session, automation_fixture_repo, owner="auto-test")

    run_result = service.run_automation_analysis(
        db_session, uuid.UUID(current_result.repository_id), uuid.UUID(current_result.revision_id)
    )

    deleted_names = {s.identity.qualified_name for s in run_result.symbol_delta.deleted}
    assert "app.math_utils.add" in deleted_names

    assert run_result.impact_summary is not None
    unavailable_names = {
        e.symbol.identity.qualified_name for e in run_result.impact_summary.deleted_symbol_impacts
    }
    assert "app.math_utils.add" in unavailable_names

    add_entry = next(
        e
        for e in run_result.impact_summary.deleted_symbol_impacts
        if e.symbol.identity.qualified_name == "app.math_utils.add"
    )
    assert add_entry.impact_status == "unavailable_deleted_symbol"

    # Never silently reported as zero impact alongside the real
    # (added/modified-symbol) direct/transitive lists — it has its own,
    # separate, explicit representation instead.
    direct_names = {s.qualified_name for s in run_result.impact_summary.direct}
    transitive_names = {s.qualified_name for s in run_result.impact_summary.transitive}
    assert "app.math_utils.add" not in direct_names
    assert "app.math_utils.add" not in transitive_names

    assert run_result.summary.deleted_symbols_with_unavailable_impact >= 1
    assert any("unavailable_deleted_symbol" in limitation for limitation in run_result.limitations)
    assert (
        run_result.analysis_status == "completed"
    )  # a documented limitation, not a pipeline failure
