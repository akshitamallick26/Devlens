"""The Automation Intelligence HTTP surface. Deliberately thin, same
convention as every other app/api/routes/*.py: every decision lives in
app/automation/service.py — this module only translates the result into
HTTP and serializes the pure result model. No ORM object is ever returned
directly.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.automation.models import (
    AutomationRunResult,
    DeletedSymbolImpact,
    FileDelta,
    FileEntry,
    ImpactSummary,
    RAGInvalidationPlan,
    SecurityInvalidationPlan,
    StageExecutionPlan,
    SymbolDelta,
    SymbolSnapshot,
    TestingInvalidationPlan,
)
from app.automation.service import (
    AutomationError,
    ParentVerificationError,
    RevisionNotFoundError,
    run_automation_analysis,
)
from app.db.revision_lock import RevisionBusyError
from app.db.session import get_db
from app.impact.models import ImpactedFile, ImpactedSymbol

router = APIRouter()

MAX_DELTA_ITEMS_IN_RESPONSE = 200


class AutomationAnalysisRequest(BaseModel):
    revision_id: str
    parent_revision_id: str | None = None


class FileEntryResponse(BaseModel):
    path: str
    file_id: str
    content_hash: str

    @classmethod
    def from_model(cls, entry: FileEntry) -> "FileEntryResponse":
        return cls(path=entry.path, file_id=str(entry.file_id), content_hash=entry.content_hash)


class ModifiedFileResponse(BaseModel):
    path: str
    parent_content_hash: str
    current_content_hash: str


class FileDeltaResponse(BaseModel):
    added: list[FileEntryResponse]
    deleted: list[FileEntryResponse]
    modified: list[ModifiedFileResponse]
    added_count: int
    deleted_count: int
    modified_count: int
    unchanged_count: int
    rename_detection: str
    truncated: bool

    @classmethod
    def from_model(cls, delta: FileDelta) -> "FileDeltaResponse":
        return cls(
            added=[
                FileEntryResponse.from_model(e) for e in delta.added[:MAX_DELTA_ITEMS_IN_RESPONSE]
            ],
            deleted=[
                FileEntryResponse.from_model(e) for e in delta.deleted[:MAX_DELTA_ITEMS_IN_RESPONSE]
            ],
            modified=[
                ModifiedFileResponse(
                    path=current.path,
                    parent_content_hash=parent.content_hash,
                    current_content_hash=current.content_hash,
                )
                for parent, current in delta.modified[:MAX_DELTA_ITEMS_IN_RESPONSE]
            ],
            added_count=len(delta.added),
            deleted_count=len(delta.deleted),
            modified_count=len(delta.modified),
            unchanged_count=len(delta.unchanged),
            rename_detection=delta.rename_detection,
            truncated=(
                len(delta.added) > MAX_DELTA_ITEMS_IN_RESPONSE
                or len(delta.deleted) > MAX_DELTA_ITEMS_IN_RESPONSE
                or len(delta.modified) > MAX_DELTA_ITEMS_IN_RESPONSE
            ),
        )


class SymbolSnapshotResponse(BaseModel):
    file_path: str
    kind: str
    qualified_name: str
    symbol_id: str


def _symbol_response(snapshot: SymbolSnapshot) -> SymbolSnapshotResponse:
    return SymbolSnapshotResponse(
        file_path=snapshot.identity.file_path,
        kind=snapshot.identity.kind,
        qualified_name=snapshot.identity.qualified_name,
        symbol_id=str(snapshot.symbol_id),
    )


class ModifiedSymbolResponse(BaseModel):
    file_path: str
    kind: str
    qualified_name: str
    parent_symbol_id: str
    current_symbol_id: str


class SymbolDeltaResponse(BaseModel):
    added: list[SymbolSnapshotResponse]
    deleted: list[SymbolSnapshotResponse]
    modified: list[ModifiedSymbolResponse]
    added_count: int
    deleted_count: int
    modified_count: int
    unchanged_count: int
    truncated: bool

    @classmethod
    def from_model(cls, delta: SymbolDelta) -> "SymbolDeltaResponse":
        return cls(
            added=[_symbol_response(s) for s in delta.added[:MAX_DELTA_ITEMS_IN_RESPONSE]],
            deleted=[_symbol_response(s) for s in delta.deleted[:MAX_DELTA_ITEMS_IN_RESPONSE]],
            modified=[
                ModifiedSymbolResponse(
                    file_path=current.identity.file_path,
                    kind=current.identity.kind,
                    qualified_name=current.identity.qualified_name,
                    parent_symbol_id=str(parent.symbol_id),
                    current_symbol_id=str(current.symbol_id),
                )
                for parent, current in delta.modified[:MAX_DELTA_ITEMS_IN_RESPONSE]
            ],
            added_count=len(delta.added),
            deleted_count=len(delta.deleted),
            modified_count=len(delta.modified),
            unchanged_count=len(delta.unchanged),
            truncated=(
                len(delta.added) > MAX_DELTA_ITEMS_IN_RESPONSE
                or len(delta.deleted) > MAX_DELTA_ITEMS_IN_RESPONSE
                or len(delta.modified) > MAX_DELTA_ITEMS_IN_RESPONSE
            ),
        )


class ImpactedSymbolResponse(BaseModel):
    symbol_id: str
    qualified_name: str
    symbol_kind: str
    file_path: str
    depth: int
    relationship_types: list[str]

    @classmethod
    def from_model(cls, symbol: ImpactedSymbol) -> "ImpactedSymbolResponse":
        return cls(
            symbol_id=str(symbol.symbol_id),
            qualified_name=symbol.qualified_name,
            symbol_kind=symbol.symbol_kind,
            file_path=symbol.file_path,
            depth=symbol.depth,
            relationship_types=list(symbol.relationship_types),
        )


class ImpactedFileResponse(BaseModel):
    file_id: str
    path: str
    symbol_count: int
    minimum_depth: int

    @classmethod
    def from_model(cls, file: ImpactedFile) -> "ImpactedFileResponse":
        return cls(
            file_id=str(file.file_id),
            path=file.path,
            symbol_count=file.symbol_count,
            minimum_depth=file.minimum_depth,
        )


class DeletedSymbolImpactResponse(BaseModel):
    file_path: str
    kind: str
    qualified_name: str
    symbol_id: str
    impact_status: str
    reason: str

    @classmethod
    def from_model(cls, entry: DeletedSymbolImpact) -> "DeletedSymbolImpactResponse":
        return cls(
            file_path=entry.symbol.identity.file_path,
            kind=entry.symbol.identity.kind,
            qualified_name=entry.symbol.identity.qualified_name,
            symbol_id=str(entry.symbol.symbol_id),
            impact_status=entry.impact_status,
            reason=entry.reason,
        )


class ImpactSummaryResponse(BaseModel):
    changed_symbol_count: int
    direct: list[ImpactedSymbolResponse]
    transitive: list[ImpactedSymbolResponse]
    affected_files: list[ImpactedFileResponse]
    max_depth: int
    truncated: bool
    # Hardening Blocker 3: deleted symbols are never folded into `direct`/
    # `transitive` as if they had zero impact — each gets an explicit entry
    # here instead, with impact_status="unavailable_deleted_symbol".
    deleted_symbol_impacts: list[DeletedSymbolImpactResponse]

    @classmethod
    def from_model(cls, summary: ImpactSummary) -> "ImpactSummaryResponse":
        return cls(
            changed_symbol_count=summary.changed_symbol_count,
            direct=[ImpactedSymbolResponse.from_model(s) for s in summary.direct],
            transitive=[ImpactedSymbolResponse.from_model(s) for s in summary.transitive],
            affected_files=[ImpactedFileResponse.from_model(f) for f in summary.affected_files],
            max_depth=summary.max_depth,
            truncated=summary.truncated,
            deleted_symbol_impacts=[
                DeletedSymbolImpactResponse.from_model(e) for e in summary.deleted_symbol_impacts
            ],
        )


class StageExecutionResponse(BaseModel):
    stage: str
    invalidated: bool
    execution_strategy: str
    status: str
    detail: str | None

    @classmethod
    def from_model(cls, plan: StageExecutionPlan) -> "StageExecutionResponse":
        return cls(
            stage=plan.stage,
            invalidated=plan.invalidated,
            execution_strategy=plan.execution_strategy,
            status=plan.status,
            detail=plan.detail,
        )


class RAGInvalidationResponse(BaseModel):
    status: str
    chunks_invalidated: int
    embeddings_require_regeneration: bool
    execution_strategy: str
    detail: str | None

    @classmethod
    def from_model(cls, plan: RAGInvalidationPlan) -> "RAGInvalidationResponse":
        return cls(
            status=plan.status,
            chunks_invalidated=plan.chunks_invalidated,
            embeddings_require_regeneration=plan.embeddings_require_regeneration,
            execution_strategy=plan.execution_strategy,
            detail=plan.detail,
        )


class TestingInvalidationResponse(BaseModel):
    stale_generated_test_ids: list[str]
    changed_target_symbol_ids: list[str]
    detail: str | None

    @classmethod
    def from_model(cls, plan: TestingInvalidationPlan) -> "TestingInvalidationResponse":
        return cls(
            stale_generated_test_ids=[str(i) for i in plan.stale_generated_test_ids],
            changed_target_symbol_ids=[str(i) for i in plan.changed_target_symbol_ids],
            detail=plan.detail,
        )


class SecurityInvalidationResponse(BaseModel):
    rerun_required: bool
    reason: str
    detail: str | None

    @classmethod
    def from_model(cls, plan: SecurityInvalidationPlan) -> "SecurityInvalidationResponse":
        return cls(rerun_required=plan.rerun_required, reason=plan.reason, detail=plan.detail)


class ChangeSummaryResponse(BaseModel):
    files_added: int
    files_modified: int
    files_deleted: int
    files_unchanged: int
    symbols_added: int
    symbols_modified: int
    symbols_deleted: int
    symbols_unchanged: int
    directly_affected_symbols: int
    transitively_affected_symbols: int
    affected_files: int
    chunks_invalidated: int
    embeddings_require_regeneration: bool
    stale_generated_tests: int
    changed_test_targets: int
    security_rerun_required: bool
    deleted_symbols_with_unavailable_impact: int


class AutomationAnalysisResponse(BaseModel):
    repository_id: str
    current_revision_id: str
    parent_revision_id: str | None
    analysis_run_id: str | None
    analysis_status: str
    file_delta: FileDeltaResponse
    symbol_delta: SymbolDeltaResponse
    parsing_execution: StageExecutionResponse
    graph_execution: StageExecutionResponse
    impact_summary: ImpactSummaryResponse | None
    rag_invalidation: RAGInvalidationResponse
    testing_invalidation: TestingInvalidationResponse
    security_invalidation: SecurityInvalidationResponse
    summary: ChangeSummaryResponse
    limitations: list[str]
    execution_note: str = (
        "Automation Intelligence v1 performs incremental change detection and invalidation "
        "planning, but uses deterministic full-revision rebuilds for revision-scoped parsing, "
        "graph construction, and chunk generation — see parsing_execution/graph_execution/"
        "rag_invalidation.execution_strategy."
    )

    @classmethod
    def from_result(cls, result: AutomationRunResult) -> "AutomationAnalysisResponse":
        return cls(
            repository_id=str(result.repository_id),
            current_revision_id=str(result.current_revision_id),
            parent_revision_id=str(result.parent_revision_id)
            if result.parent_revision_id
            else None,
            analysis_run_id=str(result.analysis_run_id) if result.analysis_run_id else None,
            analysis_status=result.analysis_status,
            file_delta=FileDeltaResponse.from_model(result.file_delta),
            symbol_delta=SymbolDeltaResponse.from_model(result.symbol_delta),
            parsing_execution=StageExecutionResponse.from_model(result.parsing_execution),
            graph_execution=StageExecutionResponse.from_model(result.graph_execution),
            impact_summary=(
                ImpactSummaryResponse.from_model(result.impact_summary)
                if result.impact_summary is not None
                else None
            ),
            rag_invalidation=RAGInvalidationResponse.from_model(result.rag_invalidation),
            testing_invalidation=TestingInvalidationResponse.from_model(
                result.testing_invalidation
            ),
            security_invalidation=SecurityInvalidationResponse.from_model(
                result.security_invalidation
            ),
            summary=ChangeSummaryResponse(
                files_added=result.summary.files_added,
                files_modified=result.summary.files_modified,
                files_deleted=result.summary.files_deleted,
                files_unchanged=result.summary.files_unchanged,
                symbols_added=result.summary.symbols_added,
                symbols_modified=result.summary.symbols_modified,
                symbols_deleted=result.summary.symbols_deleted,
                symbols_unchanged=result.summary.symbols_unchanged,
                directly_affected_symbols=result.summary.directly_affected_symbols,
                transitively_affected_symbols=result.summary.transitively_affected_symbols,
                affected_files=result.summary.affected_files,
                chunks_invalidated=result.summary.chunks_invalidated,
                embeddings_require_regeneration=result.summary.embeddings_require_regeneration,
                stale_generated_tests=result.summary.stale_generated_tests,
                changed_test_targets=result.summary.changed_test_targets,
                security_rerun_required=result.summary.security_rerun_required,
                deleted_symbols_with_unavailable_impact=(
                    result.summary.deleted_symbols_with_unavailable_impact
                ),
            ),
            limitations=list(result.limitations),
        )


@router.post(
    "/repositories/{repository_id}/automation/analyze", response_model=AutomationAnalysisResponse
)
def analyze_automation(
    repository_id: str,
    payload: AutomationAnalysisRequest,
    db: Annotated[Session, Depends(get_db)],
) -> AutomationAnalysisResponse:
    try:
        repo_uuid = uuid.UUID(repository_id)
        revision_uuid = uuid.UUID(payload.revision_id)
        parent_uuid = (
            uuid.UUID(payload.parent_revision_id)
            if payload.parent_revision_id is not None
            else None
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid uuid") from exc

    try:
        result = run_automation_analysis(db, repo_uuid, revision_uuid, parent_uuid)
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ParentVerificationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RevisionBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AutomationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return AutomationAnalysisResponse.from_result(result)
