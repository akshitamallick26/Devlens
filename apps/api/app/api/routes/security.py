"""The Security Intelligence HTTP surface. Deliberately thin, same
convention as app/api/routes/{impact,qa,testing}.py: every decision lives
in app/security/service.py — this module only translates the result into
HTTP and serializes the pure result model. No ORM object is ever returned
directly.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.ai.provider import get_provider
from app.api.rate_limit import rate_limited
from app.core.config import get_settings
from app.db.session import get_db
from app.security.explanation import SecurityExplanation
from app.security.models import BlastRadiusResult, BlastRadiusSymbol, FindingView, ObservationView
from app.security.service import (
    ALL_SCANNERS,
    FindingResult,
    RevisionNotFoundError,
    ScannerStatusSummary,
    SecurityAnalysisError,
    run_security_analysis,
)

router = APIRouter()

MAX_DEPTH_LIMIT = 10
MAX_SYMBOLS_LIMIT = 200
MAX_FILES_LIMIT = 200
_VALID_SCANNERS = frozenset(ALL_SCANNERS)


class SecurityAnalysisRequest(BaseModel):
    revision_id: str
    scanners: list[str] | None = None
    max_depth: int = Field(default=5, ge=1, le=MAX_DEPTH_LIMIT)
    max_symbols: int = Field(default=50, ge=1, le=MAX_SYMBOLS_LIMIT)
    max_files: int = Field(default=50, ge=1, le=MAX_FILES_LIMIT)
    include_explanation: bool = False


class ScannerStatusResponse(BaseModel):
    scanner: str
    status: str
    detail: str | None
    finding_count: int
    observation_count: int
    scanner_version: str | None

    @classmethod
    def from_model(cls, summary: ScannerStatusSummary) -> "ScannerStatusResponse":
        return cls(
            scanner=summary.scanner,
            status=summary.status,
            detail=summary.detail,
            finding_count=summary.finding_count,
            observation_count=summary.observation_count,
            scanner_version=summary.scanner_version,
        )


class FindingResponse(BaseModel):
    finding_id: str
    analyzer: str
    rule_id: str
    severity: str | None
    title: str
    message: str
    status: str
    cvss_score: float | None
    cvss_vector: str | None
    scanner_confidence: str | None
    references: list[str]

    @classmethod
    def from_model(cls, finding: FindingView) -> "FindingResponse":
        return cls(
            finding_id=str(finding.id),
            analyzer=finding.analyzer,
            rule_id=finding.rule_id,
            severity=finding.severity,
            title=finding.title,
            message=finding.message,
            status=finding.status,
            cvss_score=finding.cvss_score,
            cvss_vector=finding.cvss_vector,
            scanner_confidence=finding.scanner_confidence,
            references=list(finding.references),
        )


class ObservationResponse(BaseModel):
    observation_id: str
    file_path: str | None
    start_line: int | None
    end_line: int | None
    affected_package: str | None
    installed_version: str | None
    affected_range: str | None
    fixed_version: str | None
    resolution_status: str
    symbol_id: str | None

    @classmethod
    def from_model(cls, observation: ObservationView) -> "ObservationResponse":
        return cls(
            observation_id=str(observation.id),
            file_path=observation.file_path,
            start_line=observation.start_line,
            end_line=observation.end_line,
            affected_package=observation.affected_package,
            installed_version=observation.installed_version,
            affected_range=observation.affected_range,
            fixed_version=observation.fixed_version,
            resolution_status=observation.resolution_status,
            symbol_id=str(observation.symbol_id) if observation.symbol_id else None,
        )


class BlastRadiusSymbolResponse(BaseModel):
    symbol_id: str
    qualified_name: str
    kind: str
    file_path: str
    depth: int
    relationship_types: list[str]


class BlastRadiusFileResponse(BaseModel):
    file_id: str
    path: str
    symbol_count: int
    minimum_depth: int


class BlastRadiusResponse(BaseModel):
    seed_symbol_id: str
    direct_dependents: list[BlastRadiusSymbolResponse]
    transitive_dependents: list[BlastRadiusSymbolResponse]
    impacted_files: list[BlastRadiusFileResponse]
    max_depth_reached: int
    truncated: bool

    @classmethod
    def from_model(cls, result: BlastRadiusResult) -> "BlastRadiusResponse":
        def _symbol(s: BlastRadiusSymbol) -> BlastRadiusSymbolResponse:
            return BlastRadiusSymbolResponse(
                symbol_id=str(s.symbol_id),
                qualified_name=s.qualified_name,
                kind=s.kind,
                file_path=s.file_path,
                depth=s.depth,
                relationship_types=list(s.relationship_types),
            )

        return cls(
            seed_symbol_id=str(result.seed_symbol_id),
            direct_dependents=[_symbol(s) for s in result.direct_dependents],
            transitive_dependents=[_symbol(s) for s in result.transitive_dependents],
            impacted_files=[
                BlastRadiusFileResponse(
                    file_id=str(f.file_id),
                    path=f.path,
                    symbol_count=f.symbol_count,
                    minimum_depth=f.minimum_depth,
                )
                for f in result.impacted_files
            ],
            max_depth_reached=result.max_depth_reached,
            truncated=result.truncated,
        )


class SecurityExplanationResponse(BaseModel):
    summary: str
    why_it_matters: str
    blast_radius_interpretation: str
    remediation_direction: str
    evidence: list[str]
    limitations: str

    @classmethod
    def from_model(cls, explanation: SecurityExplanation) -> "SecurityExplanationResponse":
        return cls(
            summary=explanation.summary,
            why_it_matters=explanation.why_it_matters,
            blast_radius_interpretation=explanation.blast_radius_interpretation,
            remediation_direction=explanation.remediation_direction,
            evidence=explanation.evidence,
            limitations=explanation.limitations,
        )


class FindingWithContextResponse(BaseModel):
    finding: FindingResponse
    observations: list[ObservationResponse]
    blast_radius: BlastRadiusResponse | None
    explanation: SecurityExplanationResponse | None

    @classmethod
    def from_model(cls, result: FindingResult) -> "FindingWithContextResponse":
        return cls(
            finding=FindingResponse.from_model(result.finding),
            observations=[ObservationResponse.from_model(o) for o in result.observations],
            blast_radius=(
                BlastRadiusResponse.from_model(result.blast_radius)
                if result.blast_radius is not None
                else None
            ),
            explanation=(
                SecurityExplanationResponse.from_model(result.explanation)
                if result.explanation is not None
                else None
            ),
        )


class SecurityAnalysisResponse(BaseModel):
    repository_id: str
    revision_id: str
    analysis_run_id: str | None
    analysis_status: str
    scanner_results: list[ScannerStatusResponse]
    findings: list[FindingWithContextResponse]
    limitations: list[str]


@router.post(
    "/repositories/{repository_id}/security/analyze",
    response_model=SecurityAnalysisResponse,
    dependencies=[Depends(rate_limited("analysis"))],
)
def analyze_security(
    repository_id: str,
    payload: SecurityAnalysisRequest,
    db: Annotated[Session, Depends(get_db)],
) -> SecurityAnalysisResponse:
    try:
        repo_uuid = uuid.UUID(repository_id)
        revision_uuid = uuid.UUID(payload.revision_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid uuid") from exc

    if payload.scanners is not None:
        unknown = set(payload.scanners) - _VALID_SCANNERS
        if unknown:
            raise HTTPException(status_code=422, detail=f"unknown scanner(s): {sorted(unknown)}")
        scanners = tuple(payload.scanners)
    else:
        scanners = ALL_SCANNERS

    settings = get_settings()
    # Never called at import/startup time — only here, on an explicit
    # request. Returns None (not an error) when unconfigured.
    llm_provider = get_provider(settings) if payload.include_explanation else None

    try:
        result = run_security_analysis(
            db,
            repo_uuid,
            revision_uuid,
            scanners=scanners,
            llm_provider=llm_provider,
            include_explanation=payload.include_explanation,
            max_depth=payload.max_depth,
            max_symbols=payload.max_symbols,
            max_files=payload.max_files,
            osv_timeout_seconds=settings.security_osv_timeout_seconds,
            semgrep_timeout_seconds=settings.security_semgrep_timeout_seconds,
        )
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except SecurityAnalysisError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return SecurityAnalysisResponse(
        repository_id=repository_id,
        revision_id=payload.revision_id,
        analysis_run_id=str(result.analysis_run_id) if result.analysis_run_id else None,
        analysis_status=result.analysis_status,
        scanner_results=[ScannerStatusResponse.from_model(s) for s in result.scanner_results],
        findings=[FindingWithContextResponse.from_model(f) for f in result.findings],
        limitations=list(result.limitations),
    )
