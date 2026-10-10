"""The one HTTP surface this phase adds. Deliberately thin, mirroring
app/api/routes/ingestion.py's own convention: every decision lives in
app/impact/service.py — this module only translates the service's typed
result/exceptions into HTTP responses and serializes the pure result model.
No ORM object is ever returned directly. See
docs/architecture/impact-analysis.md section 11.
"""

import uuid
from typing import Annotated, Self

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, model_validator
from sqlalchemy.orm import Session

from app.ai.models import AIConfigurationError
from app.ai.provider import get_provider
from app.db.session import get_db
from app.impact.explanation import ImpactExplanation, generate_impact_explanation
from app.impact.models import ImpactAnalysisResult, ImpactedFile, ImpactedSymbol, SymbolView
from app.impact.service import (
    ImpactAnalysisError,
    ImpactAnalysisRunResult,
    RevisionNotFoundError,
    run_impact_analysis_by_qualified_name,
    run_impact_analysis_by_symbol_id,
)

router = APIRouter()


class ImpactAnalysisRequest(BaseModel):
    revision_id: str
    symbol_id: str | None = None
    qualified_name: str | None = None
    # Optional — see docs/architecture/impact-intelligence.md section 12.
    # The deterministic result is always returned regardless of this flag;
    # when true but the AI provider is unavailable or fails, the response
    # still carries the full deterministic result, just with
    # analysis_status="deterministic_only" rather than "explained".
    include_explanation: bool = False

    @model_validator(mode="after")
    def _exactly_one_target(self) -> Self:
        if (self.symbol_id is None) == (self.qualified_name is None):
            raise ValueError("exactly one of symbol_id or qualified_name is required")
        return self


class ImpactedSymbolResponse(BaseModel):
    symbol_id: str
    qualified_name: str
    symbol_kind: str
    file_id: str
    file_path: str
    depth: int
    relationship_types: list[str]
    parent_symbol_id: str | None

    @classmethod
    def from_model(cls, symbol: ImpactedSymbol) -> "ImpactedSymbolResponse":
        return cls(
            symbol_id=str(symbol.symbol_id),
            qualified_name=symbol.qualified_name,
            symbol_kind=symbol.symbol_kind,
            file_id=str(symbol.file_id),
            file_path=symbol.file_path,
            depth=symbol.depth,
            relationship_types=list(symbol.relationship_types),
            parent_symbol_id=str(symbol.parent_symbol_id) if symbol.parent_symbol_id else None,
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


class EvidenceLocationResponse(BaseModel):
    file_path: str
    line_start: int | None
    line_end: int | None


class ImpactExplanationResponse(BaseModel):
    """No numeric field — see docs/architecture/impact-intelligence.md
    section 6. Mirrors app/impact/explanation.py::ImpactExplanation
    exactly; kept as a separate model so the API's response shape doesn't
    silently change if the internal model gains a field later."""

    summary: str
    why_it_matters: str
    affected_areas: list[str]
    review_recommendations: list[str]
    test_recommendations: list[str]
    evidence: list[str]
    limitations: str

    @classmethod
    def from_model(cls, explanation: ImpactExplanation) -> "ImpactExplanationResponse":
        return cls(
            summary=explanation.summary,
            why_it_matters=explanation.why_it_matters,
            affected_areas=explanation.affected_areas,
            review_recommendations=explanation.review_recommendations,
            test_recommendations=explanation.test_recommendations,
            evidence=explanation.evidence,
            limitations=explanation.limitations,
        )


class ImpactAnalysisResponse(BaseModel):
    revision_id: str
    target_symbol_id: str
    target_qualified_name: str
    direct_impacts: list[ImpactedSymbolResponse]
    transitive_impacts: list[ImpactedSymbolResponse]
    impacted_files: list[ImpactedFileResponse]
    max_depth: int
    analysis_run_id: str | None
    # "deterministic" (explanation not requested) | "deterministic_only"
    # (requested but unavailable/failed) | "explained".
    analysis_status: str = "deterministic"
    explanation: ImpactExplanationResponse | None = None
    evidence: dict[str, EvidenceLocationResponse] | None = None

    @classmethod
    def from_result(
        cls, result: ImpactAnalysisResult, analysis_run_id: uuid.UUID | None
    ) -> "ImpactAnalysisResponse":
        return cls(
            revision_id=str(result.revision_id),
            target_symbol_id=str(result.target_symbol_id),
            target_qualified_name=result.target_symbol.qualified_name,
            direct_impacts=[ImpactedSymbolResponse.from_model(s) for s in result.direct_impacts],
            transitive_impacts=[
                ImpactedSymbolResponse.from_model(s) for s in result.transitive_impacts
            ],
            impacted_files=[ImpactedFileResponse.from_model(f) for f in result.impacted_files],
            max_depth=result.max_depth,
            analysis_run_id=str(analysis_run_id) if analysis_run_id else None,
        )


def _ambiguity_detail(candidates: tuple[SymbolView, ...]) -> dict[str, object]:
    return {
        "message": "qualified name is ambiguous within this revision",
        "candidates": [
            {"symbol_id": str(c.id), "file_path": c.file_path, "symbol_kind": c.kind}
            for c in candidates
        ],
    }


def _to_response(
    run_result: ImpactAnalysisRunResult, db: Session, include_explanation: bool
) -> ImpactAnalysisResponse:
    if run_result.status == "not_found":
        raise HTTPException(status_code=404, detail="target symbol not found in this revision")
    if run_result.status == "ambiguous":
        raise HTTPException(status_code=409, detail=_ambiguity_detail(run_result.candidates))
    assert run_result.result is not None

    response = ImpactAnalysisResponse.from_result(run_result.result, run_result.analysis_run_id)
    if not include_explanation:
        return response

    # Never called at import/startup time — only here, on an explicit
    # request. Returns None (not an error) when AI isn't configured at all;
    # raises AIConfigurationError when it *is* configured but misconfigured
    # (e.g. an unsupported ai_provider name) — either way, the deterministic
    # result must never be lost over it (Task 10's state machine).
    try:
        provider = get_provider()
    except AIConfigurationError:
        response.analysis_status = "deterministic_only"
        return response
    intelligence = generate_impact_explanation(db, provider, run_result.result)
    response.analysis_status = intelligence.analysis_status
    if intelligence.explanation is not None:
        response.explanation = ImpactExplanationResponse.from_model(intelligence.explanation)
        response.evidence = {
            evidence_id: EvidenceLocationResponse(
                file_path=located.file_path,
                line_start=located.line_start,
                line_end=located.line_end,
            )
            for evidence_id, located in intelligence.evidence_map.items()
        }
    return response


@router.post(
    "/repositories/{repository_id}/impact",
    response_model=ImpactAnalysisResponse,
)
def analyze_impact(
    repository_id: str,
    payload: ImpactAnalysisRequest,
    db: Annotated[Session, Depends(get_db)],
) -> ImpactAnalysisResponse:
    try:
        repo_uuid = uuid.UUID(repository_id)
        revision_uuid = uuid.UUID(payload.revision_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid uuid") from exc

    try:
        if payload.symbol_id is not None:
            try:
                symbol_uuid = uuid.UUID(payload.symbol_id)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="invalid symbol_id uuid") from exc
            run_result = run_impact_analysis_by_symbol_id(db, repo_uuid, revision_uuid, symbol_uuid)
        else:
            assert payload.qualified_name is not None
            run_result = run_impact_analysis_by_qualified_name(
                db, repo_uuid, revision_uuid, payload.qualified_name
            )
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ImpactAnalysisError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return _to_response(run_result, db, payload.include_explanation)
