"""The RAG/Codebase Q&A HTTP surface. Deliberately thin, same convention as
app/api/routes/{ingestion,impact}.py: every decision lives in
app/rag/qa.py — this module only translates the result into HTTP and
serializes the pure result model. No ORM object is ever returned directly.
See docs/architecture/rag-codebase-qa.md section 13.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.ai.embedding_provider import DEFAULT_EMBEDDING_MODEL_VERSION, get_embedding_provider
from app.ai.provider import get_provider
from app.core.config import get_settings
from app.db.session import get_db
from app.rag.qa import QAAnswer, QAError, ResolvedEvidence, RevisionNotFoundError, run_qa
from app.rag.query import MAX_QUESTION_CHARS

router = APIRouter()

MAX_TOP_K = 25


class QARequest(BaseModel):
    revision_id: str
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    top_k: int = Field(default=10, ge=1, le=MAX_TOP_K)

    @field_validator("question")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value


class EvidenceLocationResponse(BaseModel):
    file_path: str
    line_start: int | None
    line_end: int | None


class QAAnswerResponse(BaseModel):
    answer: str
    evidence: list[str]
    limitations: str
    related_symbols: list[str]
    suggested_next_questions: list[str]

    @classmethod
    def from_model(cls, answer: QAAnswer) -> "QAAnswerResponse":
        return cls(
            answer=answer.answer,
            evidence=answer.evidence,
            limitations=answer.limitations,
            related_symbols=answer.related_symbols,
            suggested_next_questions=answer.suggested_next_questions,
        )


class EvidenceItemResponse(BaseModel):
    evidence_id: str
    file_path: str
    symbol_id: str | None
    qualified_name: str | None
    line_start: int | None
    line_end: int | None
    retrieval_reason: str
    retrieval_source: str
    relationship_type: str | None


class QAResponse(BaseModel):
    revision_id: str
    question: str
    analysis_status: str  # "retrieval_only" | "answered"
    evidence_items: list[EvidenceItemResponse]
    answer: QAAnswerResponse | None = None
    evidence: dict[str, EvidenceLocationResponse] | None = None


def _resolved_to_response(
    resolved: dict[str, ResolvedEvidence],
) -> dict[str, EvidenceLocationResponse]:
    return {
        evidence_id: EvidenceLocationResponse(
            file_path=located.file_path, line_start=located.line_start, line_end=located.line_end
        )
        for evidence_id, located in resolved.items()
    }


@router.post("/repositories/{repository_id}/qa", response_model=QAResponse)
def answer_question(
    repository_id: str,
    payload: QARequest,
    db: Annotated[Session, Depends(get_db)],
) -> QAResponse:
    try:
        repo_uuid = uuid.UUID(repository_id)
        revision_uuid = uuid.UUID(payload.revision_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid uuid") from exc

    settings = get_settings()
    # Never called at import/startup time — only here, on an explicit
    # request. Both return None (not an error) when unconfigured.
    llm_provider = get_provider(settings)
    embedding_provider = get_embedding_provider(settings)

    try:
        result = run_qa(
            db,
            llm_provider,
            embedding_provider,
            settings.embedding_provider,
            settings.embedding_model,
            DEFAULT_EMBEDDING_MODEL_VERSION,
            repo_uuid,
            revision_uuid,
            payload.question,
            top_k=payload.top_k,
        )
    except RevisionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except QAError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    evidence_items = [
        EvidenceItemResponse(
            evidence_id=item.evidence_id,
            file_path=item.file_path,
            symbol_id=str(item.symbol_id) if item.symbol_id else None,
            qualified_name=item.qualified_name,
            line_start=item.line_start,
            line_end=item.line_end,
            retrieval_reason=item.retrieval_reason,
            retrieval_source=item.retrieval_source,
            relationship_type=item.relationship_type,
        )
        for item in result.evidence_set.items
    ]

    return QAResponse(
        revision_id=payload.revision_id,
        question=payload.question,
        analysis_status=result.analysis_status,
        evidence_items=evidence_items,
        answer=QAAnswerResponse.from_model(result.answer) if result.answer is not None else None,
        evidence=_resolved_to_response(result.evidence_map) if result.evidence_map else None,
    )
