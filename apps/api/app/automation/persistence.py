"""AnalysisRun persistence for automation runs (Task 11, hardened per
Blocker 1). Automation has its own idempotency contract, distinct from
every other analyzer type's append-only-history convention: repeated calls
for the same logical `(repository_id, revision_id, parent_revision_id)`
must resolve to exactly one row, never an uncontrolled duplicate.

Implemented as an atomic `INSERT ... ON CONFLICT DO UPDATE` against the two
partial unique indexes added in migration `0007`
(`uq_analysis_runs_automation_with_parent` /
`uq_analysis_runs_automation_without_parent`) — the same upsert-by-unique-
key pattern `app/security/persistence.py::upsert_finding` and
`app/rag/embeddings.py` already established for their own idempotency
needs. A retry (including a retry after a prior `failed`/`partial` run)
deterministically replaces that same row's status/timestamps/config rather
than reusing stale state or creating a new row — chosen over "reuse the
cached result" because the underlying derived data (Symbol/GraphEdge/
CodeChunk) could have changed since the prior run for reasons outside
automation's own control, so recomputing is the only option in this
"correctness over optimization" phase that reliably carries no staleness
risk.
"""

import platform
import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.automation.models import AutomationRunResult
from app.models import AnalysisRun

ANALYZER_NAME = "devlens-automation-analysis"


def analyzer_version() -> str:
    return f"automation-analysis@{platform.python_version()}"


def start_or_reuse_run(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    parent_revision_id: uuid.UUID | None,
) -> AnalysisRun:
    """Atomically creates the logical automation run for this exact
    `(repository_id, revision_id, parent_revision_id)` identity, or — if one
    already exists (a retry, whatever its prior status) — resets that same
    row to `status="running"` in place. Never creates a second row for an
    identity that already has one."""
    now = datetime.now(UTC)
    version = analyzer_version()
    config = {"parent_revision_id": str(parent_revision_id) if parent_revision_id else None}

    if parent_revision_id is not None:
        index_elements = ["repository_id", "revision_id", "parent_revision_id", "analysis_type"]
        index_where = text(
            "analysis_type = 'automation_analysis' AND parent_revision_id IS NOT NULL"
        )
    else:
        index_elements = ["repository_id", "revision_id", "analysis_type"]
        index_where = text("analysis_type = 'automation_analysis' AND parent_revision_id IS NULL")

    stmt = (
        pg_insert(AnalysisRun)
        .values(
            repository_id=repository_id,
            revision_id=revision_id,
            parent_revision_id=parent_revision_id,
            analysis_type="automation_analysis",
            analyzer_name=ANALYZER_NAME,
            analyzer_version=version,
            status="running",
            started_at=now,
            finished_at=None,
            error_message=None,
            config=config,
        )
        .on_conflict_do_update(
            index_elements=index_elements,
            index_where=index_where,
            set_={
                "analyzer_version": version,
                "status": "running",
                "started_at": now,
                "finished_at": None,
                "error_message": None,
                "config": config,
            },
        )
        .returning(AnalysisRun.id)
    )
    run_id = db.execute(stmt).scalar_one()
    db.commit()
    run = db.get(AnalysisRun, run_id)
    assert run is not None  # just upserted in the same transaction
    return run


def finish_run(db: Session, run: AnalysisRun, result: AutomationRunResult) -> None:
    run.status = result.analysis_status
    run.finished_at = datetime.now(UTC)
    run.config = {
        "parent_revision_id": str(result.parent_revision_id) if result.parent_revision_id else None,
        "changed_file_count": (
            len(result.file_delta.added)
            + len(result.file_delta.deleted)
            + len(result.file_delta.modified)
        ),
        "changed_symbol_count": (
            len(result.symbol_delta.added)
            + len(result.symbol_delta.deleted)
            + len(result.symbol_delta.modified)
        ),
        "impacted_symbol_count": (
            (len(result.impact_summary.direct) + len(result.impact_summary.transitive))
            if result.impact_summary is not None
            else 0
        ),
        "affected_file_count": (
            len(result.impact_summary.affected_files) if result.impact_summary is not None else 0
        ),
        "limitations": list(result.limitations),
    }
    db.commit()


def fail_run(db: Session, run: AnalysisRun, error_message: str) -> None:
    db.rollback()
    run.status = "failed"
    run.error_message = error_message[:2000]
    run.finished_at = datetime.now(UTC)
    db.commit()
