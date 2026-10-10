"""The Testing/Generated Tests HTTP surface. Deliberately thin, same
convention as app/api/routes/{impact,qa}.py: every decision lives in
app/testing/service.py — this module only translates the result into HTTP
and serializes the pure result model. No ORM object is ever returned
directly.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.ai.provider import get_provider
from app.core.config import get_settings
from app.db.session import get_db
from app.testing.models import ValidationIssue
from app.testing.service import (
    MAX_TESTS_PER_REQUEST,
    RevisionNotFoundError,
    TargetGenerationOutcome,
    TestGenerationError,
    generate_tests_for_target,
)

router = APIRouter()

MAX_SYMBOL_IDS = 50
MAX_FILE_IDS = 50


class GenerateTestsRequest(BaseModel):
    revision_id: str
    symbol_ids: list[str] = Field(default_factory=list, max_length=MAX_SYMBOL_IDS)
    file_ids: list[str] = Field(default_factory=list, max_length=MAX_FILE_IDS)
    execute: bool = False
    max_tests: int = Field(default=MAX_TESTS_PER_REQUEST, ge=1, le=MAX_TESTS_PER_REQUEST)


class ValidationIssueResponse(BaseModel):
    code: str
    detail: str

    @classmethod
    def from_model(cls, issue: ValidationIssue) -> "ValidationIssueResponse":
        return cls(code=issue.code, detail=issue.detail)


class ExecutionSummaryResponse(BaseModel):
    status: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stdout: str
    stderr: str


class GeneratedTestOutcomeResponse(BaseModel):
    target_symbol_id: str
    target_qualified_name: str
    selection_reason: str
    status: str
    generated_test_id: str | None
    execution_id: str | None
    validation_issues: list[ValidationIssueResponse]
    execution: ExecutionSummaryResponse | None
    error: str | None

    @classmethod
    def from_model(cls, outcome: TargetGenerationOutcome) -> "GeneratedTestOutcomeResponse":
        execution = (
            ExecutionSummaryResponse(
                status=outcome.execution.status,
                exit_code=outcome.execution.exit_code,
                timed_out=outcome.execution.timed_out,
                duration_ms=outcome.execution.duration_ms,
                stdout=outcome.execution.stdout,
                stderr=outcome.execution.stderr,
            )
            if outcome.execution is not None
            else None
        )
        return cls(
            target_symbol_id=str(outcome.target.target_symbol_id),
            target_qualified_name=outcome.target.qualified_name,
            selection_reason=outcome.target.selection_reason,
            status=outcome.status,
            generated_test_id=str(outcome.generated_test_id) if outcome.generated_test_id else None,
            execution_id=str(outcome.execution_id) if outcome.execution_id else None,
            validation_issues=[
                ValidationIssueResponse.from_model(i) for i in outcome.validation_issues
            ],
            execution=execution,
            error=outcome.error,
        )


class GenerateTestsResponse(BaseModel):
    repository_id: str
    revision_id: str
    analysis_run_id: str | None
    analysis_status: str  # no_targets | no_provider | completed
    outcomes: list[GeneratedTestOutcomeResponse]
    limitations: str


_LIMITATIONS = (
    "A passing generated test means it passed under this execution "
    "environment — it does not mean the target is correct, the repository "
    "is bug-free, or coverage is complete. A failed test does not by "
    "itself mean the target code is broken. Execution uses a subprocess "
    "and temporary-directory boundary, not OS-level sandboxing (no "
    "container, no network isolation) — see docs/architecture."
)


def _parse_uuid_list(values: list[str], field_name: str) -> set[uuid.UUID]:
    try:
        return {uuid.UUID(v) for v in values}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid uuid in {field_name}") from exc


@router.post("/repositories/{repository_id}/testing/generate", response_model=GenerateTestsResponse)
def generate_tests(
    repository_id: str,
    payload: GenerateTestsRequest,
    db: Annotated[Session, Depends(get_db)],
) -> GenerateTestsResponse:
    try:
        repo_uuid = uuid.UUID(repository_id)
        revision_uuid = uuid.UUID(payload.revision_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid uuid") from exc

    symbol_ids = _parse_uuid_list(payload.symbol_ids, "symbol_ids")
    file_ids = _parse_uuid_list(payload.file_ids, "file_ids")

    settings = get_settings()
    # Never called at import/startup time — only here, on an explicit
    # request. Returns None (not an error) when unconfigured.
    llm_provider = get_provider(settings)

    try:
        result = generate_tests_for_target(
            db,
            llm_provider,
            repo_uuid,
            revision_uuid,
            impacted_symbol_ids=symbol_ids or None,
            impacted_file_ids=file_ids or None,
            execute=payload.execute,
            max_tests=payload.max_tests,
            execution_timeout_seconds=settings.testing_execution_timeout_seconds,
            max_output_bytes=settings.testing_max_output_bytes,
        )
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TestGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return GenerateTestsResponse(
        repository_id=repository_id,
        revision_id=payload.revision_id,
        analysis_run_id=str(result.analysis_run_id) if result.analysis_run_id else None,
        analysis_status=result.analysis_status,
        outcomes=[GeneratedTestOutcomeResponse.from_model(o) for o in result.outcomes],
        limitations=_LIMITATIONS,
    )
