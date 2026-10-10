"""The Documentation Intelligence HTTP surface. Deliberately thin, same
convention as every other app/api/routes/*.py: every decision lives in
app/documentation/service.py — this module only translates typed
exceptions into HTTP and serializes the pure result shapes. No ORM object
is ever returned directly. See
docs/architecture/documentation-intelligence.md section 16.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.ai.provider import get_provider
from app.api.rate_limit import rate_limited
from app.core.config import get_settings
from app.db.session import get_db
from app.documentation.models import DocumentationArtifactResult, DocumentationArtifactSummary
from app.documentation.service import (
    ArtifactNotFoundError,
    DocumentationError,
    InvalidArtifactTypeError,
    MissingTargetError,
    RevisionNotFoundError,
    TargetConflictError,
    TargetNotFoundError,
    UnexpectedTargetError,
    generate_documentation,
    get_artifact,
    list_artifacts,
)

router = APIRouter()


def _parse_uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid {label}") from exc


def _handle(exc: DocumentationError) -> HTTPException:
    if isinstance(exc, (RevisionNotFoundError, TargetNotFoundError, ArtifactNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, TargetConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (InvalidArtifactTypeError, MissingTargetError, UnexpectedTargetError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


class GenerateDocumentationRequest(BaseModel):
    revision_id: str
    artifact_type: str
    source_file_id: str | None = None
    symbol_id: str | None = None


class DocumentationArtifactResponse(BaseModel):
    artifact_id: str
    repository_id: str
    revision_id: str
    artifact_type: str
    target_file_id: str | None
    target_symbol_id: str | None
    title: str
    content: str
    generation_status: str  # "generated" | "fallback" — see service docstring
    provider: str
    model_name: str
    model_version: str
    evidence_ids: list[str]
    cited_evidence_ids: list[str]
    context_truncated: bool
    analysis_run_id: str
    created_at: str
    updated_at: str

    @classmethod
    def from_result(cls, result: DocumentationArtifactResult) -> "DocumentationArtifactResponse":
        return cls(
            artifact_id=str(result.artifact_id),
            repository_id=str(result.repository_id),
            revision_id=str(result.revision_id),
            artifact_type=result.artifact_type,
            target_file_id=str(result.target_file_id) if result.target_file_id else None,
            target_symbol_id=str(result.target_symbol_id) if result.target_symbol_id else None,
            title=result.title,
            content=result.content,
            generation_status=(
                "generated" if result.generation_method == "llm_generated" else "fallback"
            ),
            provider=result.provider,
            model_name=result.model_name,
            model_version=result.model_version,
            evidence_ids=list(result.evidence_ids),
            cited_evidence_ids=list(result.cited_evidence_ids),
            context_truncated=result.context_truncated,
            analysis_run_id=str(result.analysis_run_id),
            created_at=result.created_at,
            updated_at=result.updated_at,
        )


class DocumentationArtifactSummaryResponse(BaseModel):
    artifact_id: str
    repository_id: str
    revision_id: str
    artifact_type: str
    target_file_id: str | None
    target_symbol_id: str | None
    title: str
    generation_status: str
    created_at: str
    updated_at: str

    @classmethod
    def from_summary(
        cls, summary: DocumentationArtifactSummary
    ) -> "DocumentationArtifactSummaryResponse":
        return cls(
            artifact_id=str(summary.artifact_id),
            repository_id=str(summary.repository_id),
            revision_id=str(summary.revision_id),
            artifact_type=summary.artifact_type,
            target_file_id=str(summary.target_file_id) if summary.target_file_id else None,
            target_symbol_id=str(summary.target_symbol_id) if summary.target_symbol_id else None,
            title=summary.title,
            generation_status=(
                "generated" if summary.generation_method == "llm_generated" else "fallback"
            ),
            created_at=summary.created_at,
            updated_at=summary.updated_at,
        )


class DocumentationArtifactListResponse(BaseModel):
    artifacts: list[DocumentationArtifactSummaryResponse]


@router.post(
    "/repositories/{repository_id}/documentation/generate",
    response_model=DocumentationArtifactResponse,
    dependencies=[Depends(rate_limited("analysis"))],
)
def generate_documentation_endpoint(
    repository_id: str,
    payload: GenerateDocumentationRequest,
    db: Annotated[Session, Depends(get_db)],
) -> DocumentationArtifactResponse:
    repo_uuid = _parse_uuid(repository_id, "repository_id")
    revision_uuid = _parse_uuid(payload.revision_id, "revision_id")
    source_file_uuid = (
        _parse_uuid(payload.source_file_id, "source_file_id")
        if payload.source_file_id is not None
        else None
    )
    symbol_uuid = (
        _parse_uuid(payload.symbol_id, "symbol_id") if payload.symbol_id is not None else None
    )

    settings = get_settings()
    llm_provider = get_provider(settings)

    try:
        result = generate_documentation(
            db,
            llm_provider,
            repo_uuid,
            revision_uuid,
            payload.artifact_type,
            source_file_id=source_file_uuid,
            symbol_id=symbol_uuid,
            provider_name=settings.ai_provider,
            model_name=settings.ai_model,
            model_version="",
        )
    except DocumentationError as exc:
        raise _handle(exc) from exc
    return DocumentationArtifactResponse.from_result(result)


@router.get(
    "/repositories/{repository_id}/documentation",
    response_model=DocumentationArtifactListResponse,
)
def list_documentation_endpoint(
    repository_id: str,
    db: Annotated[Session, Depends(get_db)],
    revision_id: Annotated[str | None, Query()] = None,
    artifact_type: Annotated[str | None, Query()] = None,
) -> DocumentationArtifactListResponse:
    repo_uuid = _parse_uuid(repository_id, "repository_id")
    revision_uuid = _parse_uuid(revision_id, "revision_id") if revision_id is not None else None
    summaries = list_artifacts(
        db, repo_uuid, revision_id=revision_uuid, artifact_type=artifact_type
    )
    return DocumentationArtifactListResponse(
        artifacts=[DocumentationArtifactSummaryResponse.from_summary(s) for s in summaries]
    )


@router.get(
    "/repositories/{repository_id}/documentation/{artifact_id}",
    response_model=DocumentationArtifactResponse,
)
def get_documentation_endpoint(
    repository_id: str,
    artifact_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> DocumentationArtifactResponse:
    repo_uuid = _parse_uuid(repository_id, "repository_id")
    artifact_uuid = _parse_uuid(artifact_id, "artifact_id")
    try:
        result = get_artifact(db, repo_uuid, artifact_uuid)
    except DocumentationError as exc:
        raise _handle(exc) from exc
    return DocumentationArtifactResponse.from_result(result)
