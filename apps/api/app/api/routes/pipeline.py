"""The repository-analysis pipeline HTTP surface. Thin, same convention as
every other app/api/routes/*.py: decisions live in app/pipeline/service.py.

The analyze endpoint is synchronous: the response is sent after every
requested stage finished (or failed). There is no background job and no
progress percentage. See docs/architecture/analysis-pipeline.md.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.pipeline.models import PipelineResult, PipelineStatus
from app.pipeline.service import (
    AnalysisInProgressError,
    PipelineError,
    RepositoryNotFoundError,
    RevisionConflictError,
    RevisionNotFoundError,
    analyze_revision,
    get_pipeline_status,
)

router = APIRouter()


def _parse_uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid {label}") from exc


def _handle(exc: PipelineError) -> HTTPException:
    if isinstance(exc, (RepositoryNotFoundError, RevisionNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (RevisionConflictError, AnalysisInProgressError)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


class AnalyzeRevisionRequest(BaseModel):
    run_parsing: bool = True
    run_graph: bool = True
    run_rag: bool = True
    # Rebuild requested stages even when their output is current.
    force: bool = False


@router.post(
    "/repositories/{repository_id}/revisions/{revision_id}/analyze",
    response_model=PipelineResult,
)
def analyze_revision_endpoint(
    repository_id: str,
    revision_id: str,
    db: Annotated[Session, Depends(get_db)],
    payload: AnalyzeRevisionRequest | None = None,
) -> PipelineResult:
    payload = payload or AnalyzeRevisionRequest()
    try:
        return analyze_revision(
            db,
            _parse_uuid(repository_id, "repository_id"),
            _parse_uuid(revision_id, "revision_id"),
            run_parsing_stage=payload.run_parsing,
            run_graph_stage=payload.run_graph,
            run_rag_stage=payload.run_rag,
            force=payload.force,
        )
    except PipelineError as exc:
        raise _handle(exc) from exc


@router.get(
    "/repositories/{repository_id}/revisions/{revision_id}/analysis",
    response_model=PipelineStatus,
)
def pipeline_status_endpoint(
    repository_id: str,
    revision_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> PipelineStatus:
    try:
        return get_pipeline_status(
            db,
            _parse_uuid(repository_id, "repository_id"),
            _parse_uuid(revision_id, "revision_id"),
        )
    except PipelineError as exc:
        raise _handle(exc) from exc
