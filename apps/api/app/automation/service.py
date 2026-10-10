"""Automation orchestration: `run_automation_analysis`. Pipeline —

    current revision -> resolve+verify parent -> file delta
    -> parsing (full revision rebuild, no partial mode exists)
    -> graph construction (full revision rebuild, no partial mode exists)
    -> symbol delta (bounded to changed files only)
    -> change impact (reuses app.impact.traversal.compute_impact directly)
    -> RAG rebuild (full revision rebuild, no partial mode exists)
    -> testing invalidation (decision only, no regeneration)
    -> security invalidation (decision only, no scan triggered)
    -> deterministic summary -> persist/reuse AnalysisRun

This module calls the existing, unmodified orchestrators for parsing
(app.parsing.service), graph construction (app.graph.service), and RAG
chunking (app.rag.persistence) exactly as-is — it does not duplicate their
logic.

Automation Intelligence v1 performs incremental CHANGE DETECTION and
INVALIDATION PLANNING, but uses deterministic full-revision rebuilds for
revision-scoped parsing, graph construction, and chunk generation. None of
those three support partial/incremental execution today (each
unconditionally rebuilds its derived rows for the whole revision), and a
brand-new revision starts with zero rows of its own in all three regardless
of how much file content is unchanged — there is no safe way to skip
calling them and still have the current revision's own derived data exist.
`StageExecutionPlan`/`RAGInvalidationPlan.execution_strategy` make this
explicit on every result (Hardening Blocker 2): `invalidated` reports the
real change-detection decision, `execution_strategy` always reports
`"full_revision_rebuild"` regardless of that decision, so a caller can
never mistake "no files changed" for "this stage was selectively skipped."

Testing and security are decision-only (Tasks 9/10): no GeneratedTest is
regenerated and no scanner is invoked by this module.

Idempotency (Hardening Blocker 1): `app.automation.persistence.
start_or_reuse_run` atomically upserts the one `AnalysisRun` row for this
exact `(repository_id, revision_id, parent_revision_id)` identity — a
retry, including a retry after a prior `failed`/`partial` run, always
reuses and overwrites that same row rather than creating another one.
"""

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.automation.impact_reuse import compute_change_impact
from app.automation.models import (
    AutomationRunResult,
    DeterministicChangeSummary,
    ParentResolution,
    RAGInvalidationPlan,
    StageExecutionPlan,
    SymbolDelta,
    TestingInvalidationPlan,
)
from app.automation.parent_resolution import resolve_parent
from app.automation.persistence import fail_run, finish_run, start_or_reuse_run
from app.automation.rag_invalidation import rebuild_rag_index
from app.automation.revision_delta import compute_file_delta
from app.automation.security_invalidation import compute_security_invalidation
from app.automation.symbol_delta import compute_symbol_delta, load_symbol_snapshots
from app.automation.testing_invalidation import compute_testing_invalidation
from app.db.revision_lock import revision_rebuild_lock
from app.graph.service import GraphConstructionError, run_graph_construction
from app.graph.service import RevisionNotFoundError as GraphRevisionNotFoundError
from app.models import RepositoryRevision
from app.parsing.service import ParsingError, run_parsing
from app.parsing.service import RevisionNotFoundError as ParsingRevisionNotFoundError

logger = logging.getLogger("devlens.automation")

_FULL_REVISION_REBUILD = "full_revision_rebuild"


class AutomationError(Exception):
    pass


class RevisionNotFoundError(AutomationError):
    pass


class ParentVerificationError(AutomationError):
    """The caller supplied a `parent_revision_id` that does not verify as
    the current revision's actual parent (Task 16: "Do NOT compare
    unrelated revisions merely because the user supplied two UUIDs")."""

    def __init__(self, resolution: ParentResolution) -> None:
        self.resolution = resolution
        super().__init__(resolution.detail or f"parent verification failed: {resolution.status}")


@dataclass
class _StageOutcomes:
    parsing_ok: bool = True
    graph_ok: bool = True
    rag_ok: bool = True
    limitations: list[str] | None = None

    def __post_init__(self) -> None:
        if self.limitations is None:
            self.limitations = []


def _log_event(event: str, **fields: object) -> None:
    logger.info(event, extra={"event": event, **fields})


def run_automation_analysis(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    requested_parent_revision_id: uuid.UUID | None = None,
) -> AutomationRunResult:
    revision = db.get(RepositoryRevision, revision_id)
    if revision is None or revision.repository_id != repository_id:
        raise RevisionNotFoundError(f"no revision {revision_id} for repository {repository_id}")

    parent_resolution = resolve_parent(db, repository_id, revision, requested_parent_revision_id)
    if parent_resolution.status in ("mismatch", "not_found"):
        raise ParentVerificationError(parent_resolution)
    parent_revision_id = parent_resolution.parent_revision_id  # None means "no_parent"

    # Same per-revision rebuild lock as the analysis pipeline: both delete and
    # rebuild this revision's symbols/edges/chunks, and two concurrent
    # rebuilds can commit duplicate symbol sets. RevisionBusyError is raised
    # before the automation run row is touched.
    with revision_rebuild_lock(db, revision_id):
        run = start_or_reuse_run(db, repository_id, revision_id, parent_revision_id)
        _log_event(
            "automation_analysis_started",
            repository_id=str(repository_id),
            revision_id=str(revision_id),
            parent_revision_id=str(parent_revision_id) if parent_revision_id else None,
            analysis_run_id=str(run.id),
        )

        try:
            pipeline_result = _execute_pipeline(db, repository_id, revision_id, parent_revision_id)
            result = AutomationRunResult(
                repository_id=pipeline_result.repository_id,
                current_revision_id=pipeline_result.current_revision_id,
                parent_revision_id=pipeline_result.parent_revision_id,
                analysis_run_id=run.id,
                analysis_status=pipeline_result.analysis_status,
                file_delta=pipeline_result.file_delta,
                symbol_delta=pipeline_result.symbol_delta,
                parsing_execution=pipeline_result.parsing_execution,
                graph_execution=pipeline_result.graph_execution,
                impact_summary=pipeline_result.impact_summary,
                rag_invalidation=pipeline_result.rag_invalidation,
                testing_invalidation=pipeline_result.testing_invalidation,
                security_invalidation=pipeline_result.security_invalidation,
                summary=pipeline_result.summary,
                limitations=pipeline_result.limitations,
            )
            finish_run(db, run, result)
            _log_event(
                "automation_analysis_finished",
                revision_id=str(revision_id),
                analysis_status=result.analysis_status,
            )
            return result
        except Exception as exc:
            fail_run(db, run, str(exc))
            _log_event("automation_analysis_failed", revision_id=str(revision_id), reason=str(exc))
            raise AutomationError(str(exc)) from exc


def _execute_pipeline(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    parent_revision_id: uuid.UUID | None,
) -> AutomationRunResult:
    stages = _StageOutcomes()
    assert stages.limitations is not None

    file_delta = compute_file_delta(db, repository_id, revision_id, parent_revision_id)
    any_file_changed = bool(file_delta.added or file_delta.deleted or file_delta.modified)

    try:
        run_parsing(db, repository_id, revision_id)
        parsing_execution = StageExecutionPlan(
            stage="parsing",
            invalidated=any_file_changed,
            execution_strategy=_FULL_REVISION_REBUILD,
            status="completed",
            detail=None,
        )
    except (ParsingError, ParsingRevisionNotFoundError) as exc:
        stages.parsing_ok = False
        stages.limitations.append(f"parsing failed: {exc}")
        parsing_execution = StageExecutionPlan(
            stage="parsing",
            invalidated=any_file_changed,
            execution_strategy=_FULL_REVISION_REBUILD,
            status="failed",
            detail=str(exc),
        )

    symbol_delta = SymbolDelta(added=(), deleted=(), modified=(), unchanged=())
    impact_summary = None
    graph_execution = StageExecutionPlan(
        stage="graph_construction",
        invalidated=any_file_changed,
        execution_strategy=_FULL_REVISION_REBUILD,
        status="skipped",
        detail="skipped — parsing failed",
    )
    rag_plan = RAGInvalidationPlan(
        status="failed",
        chunks_invalidated=0,
        embeddings_require_regeneration=True,
        execution_strategy=_FULL_REVISION_REBUILD,
        detail="skipped — an earlier required stage failed",
    )
    testing_plan = TestingInvalidationPlan(
        stale_generated_test_ids=(),
        changed_target_symbol_ids=(),
        detail="skipped — an earlier required stage failed",
    )

    if stages.parsing_ok:
        try:
            run_graph_construction(db, repository_id, revision_id)
            graph_execution = StageExecutionPlan(
                stage="graph_construction",
                invalidated=any_file_changed,
                execution_strategy=_FULL_REVISION_REBUILD,
                status="completed",
                detail=None,
            )
        except (GraphConstructionError, GraphRevisionNotFoundError) as exc:
            stages.graph_ok = False
            stages.limitations.append(f"graph construction failed: {exc}")
            graph_execution = StageExecutionPlan(
                stage="graph_construction",
                invalidated=any_file_changed,
                execution_strategy=_FULL_REVISION_REBUILD,
                status="failed",
                detail=str(exc),
            )

    if stages.parsing_ok and stages.graph_ok:
        current_changed_file_ids = {e.file_id for e in file_delta.added} | {
            current.file_id for _, current in file_delta.modified
        }
        parent_changed_file_ids = {e.file_id for e in file_delta.deleted} | {
            parent.file_id for parent, _ in file_delta.modified
        }
        current_snapshots = load_symbol_snapshots(
            db, repository_id, revision_id, current_changed_file_ids
        )
        parent_snapshots = (
            load_symbol_snapshots(db, repository_id, parent_revision_id, parent_changed_file_ids)
            if parent_revision_id is not None
            else []
        )
        symbol_delta = compute_symbol_delta(current_snapshots, parent_snapshots)

        changed_symbol_ids = {s.symbol_id for s in symbol_delta.added} | {
            current.symbol_id for _, current in symbol_delta.modified
        }
        # Hardening Blocker 3: deleted symbols are passed through so their
        # impact is explicitly marked unavailable, never silently absent or
        # zero — see app/automation/impact_reuse.py::compute_deleted_symbol_impacts.
        impact_summary = compute_change_impact(
            db, repository_id, revision_id, changed_symbol_ids, symbol_delta.deleted
        )
        if symbol_delta.deleted:
            stages.limitations.append(
                f"{len(symbol_delta.deleted)} deleted symbol(s) have impact_status="
                "unavailable_deleted_symbol — their former dependents cannot be determined "
                "without traversing the parent revision's graph, which this phase does not do"
            )

        testing_plan = compute_testing_invalidation(
            db, repository_id, parent_revision_id, symbol_delta
        )

        rag_plan = rebuild_rag_index(db, repository_id, revision_id)
        if rag_plan.status != "rebuilt":
            stages.rag_ok = False
            stages.limitations.append(f"RAG rebuild failed: {rag_plan.detail}")

    security_plan = compute_security_invalidation(file_delta, parent_revision_id is not None)

    if parent_revision_id is None:
        stages.limitations.append(
            "no parent revision — every file is reported as added; there is nothing to diff"
        )
    stages.limitations.append(
        "parsing, graph construction, and chunk generation all ran as deterministic "
        "full-revision rebuilds in v1 — none support partial/incremental execution; see "
        "parsing_execution/graph_execution/rag_invalidation.execution_strategy"
    )

    summary = DeterministicChangeSummary(
        files_added=len(file_delta.added),
        files_modified=len(file_delta.modified),
        files_deleted=len(file_delta.deleted),
        files_unchanged=len(file_delta.unchanged),
        symbols_added=len(symbol_delta.added),
        symbols_modified=len(symbol_delta.modified),
        symbols_deleted=len(symbol_delta.deleted),
        symbols_unchanged=len(symbol_delta.unchanged),
        directly_affected_symbols=len(impact_summary.direct) if impact_summary else 0,
        transitively_affected_symbols=len(impact_summary.transitive) if impact_summary else 0,
        affected_files=len(impact_summary.affected_files) if impact_summary else 0,
        chunks_invalidated=rag_plan.chunks_invalidated,
        embeddings_require_regeneration=rag_plan.embeddings_require_regeneration,
        stale_generated_tests=len(testing_plan.stale_generated_test_ids),
        changed_test_targets=len(testing_plan.changed_target_symbol_ids),
        security_rerun_required=security_plan.rerun_required,
        deleted_symbols_with_unavailable_impact=(
            len(impact_summary.deleted_symbol_impacts)
            if impact_summary
            else len(symbol_delta.deleted)
        ),
    )

    if not stages.parsing_ok:
        status = "failed"
    elif not stages.graph_ok or not stages.rag_ok:
        status = "partial"
    else:
        status = "completed"

    return AutomationRunResult(
        repository_id=repository_id,
        current_revision_id=revision_id,
        parent_revision_id=parent_revision_id,
        analysis_run_id=None,  # filled in by the caller
        analysis_status=status,
        file_delta=file_delta,
        symbol_delta=symbol_delta,
        parsing_execution=parsing_execution,
        graph_execution=graph_execution,
        impact_summary=impact_summary,
        rag_invalidation=rag_plan,
        testing_invalidation=testing_plan,
        security_invalidation=security_plan,
        summary=summary,
        limitations=tuple(stages.limitations),
    )
