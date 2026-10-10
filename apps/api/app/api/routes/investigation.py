"""The Investigation Sessions & Recents HTTP surface. Deliberately thin,
same convention as every other app/api/routes/*.py: every decision lives in
app/investigation/service.py — this module only translates typed exceptions
into HTTP and serializes the pure result shapes. No ORM object is ever
returned directly. See docs/architecture/investigation-sessions.md.
"""

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api.rate_limit import rate_limited
from app.db.session import get_db
from app.investigation.models import (
    MAX_GRAPH_CAMERA_STATE_BYTES,
    MAX_MESSAGE_PAGE_SIZE,
    MAX_RECENTS_PAGE_SIZE,
    MAX_TITLE_LENGTH,
    ContextRefView,
    MessagePage,
    MessageView,
    RecentSessionsPage,
    SessionSnapshot,
    SessionSummary,
)
from app.investigation.service import (
    ContextRefNotFoundError,
    CrossRepositoryReferenceError,
    CrossSessionReferenceError,
    InvalidContextRefError,
    InvalidRoleError,
    InvalidUpdateError,
    InvestigationError,
    ReferencedEntityNotFoundError,
    RepositoryNotFoundError,
    RevisionConflictError,
    RevisionNotFoundError,
    SessionNotFoundError,
    add_context_ref,
    append_message,
    create_session,
    get_session,
    list_recent_sessions,
    remove_context_ref,
    update_session,
)

router = APIRouter()

MAX_MESSAGE_CONTENT_LENGTH = 20_000


def _parse_uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid {label}") from exc


def _handle(exc: InvestigationError) -> HTTPException:
    if isinstance(exc, (RepositoryNotFoundError, SessionNotFoundError, RevisionNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ReferencedEntityNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ContextRefNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(
        exc, (RevisionConflictError, CrossRepositoryReferenceError, CrossSessionReferenceError)
    ):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (InvalidUpdateError, InvalidRoleError, InvalidContextRefError)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


class CreateSessionRequest(BaseModel):
    revision_id: str | None = None
    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)


class SessionUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH)
    status: str | None = None
    revision_id: str | None = None


class AppendMessageRequest(BaseModel):
    role: str
    content: str = Field(max_length=MAX_MESSAGE_CONTENT_LENGTH)


class ContextRefRequest(BaseModel):
    ref_type: str
    file_id: str | None = None
    symbol_id: str | None = None
    analysis_run_id: str | None = None
    finding_id: str | None = None
    graph_camera_state: dict[str, object] | None = None
    message_id: str | None = None

    @field_validator("graph_camera_state")
    @classmethod
    def _bounded_camera_state(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        if value is not None:
            size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            if size > MAX_GRAPH_CAMERA_STATE_BYTES:
                raise ValueError(
                    f"graph_camera_state is {size} bytes; at most "
                    f"{MAX_GRAPH_CAMERA_STATE_BYTES} are accepted"
                )
        return value


class MessageResponse(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    sequence_number: int
    created_at: str

    @classmethod
    def from_model(cls, message: MessageView) -> "MessageResponse":
        return cls(
            id=str(message.id),
            session_id=str(message.session_id),
            role=message.role,
            content=message.content,
            sequence_number=message.sequence_number,
            created_at=message.created_at,
        )


class MessagePageResponse(BaseModel):
    messages: list[MessageResponse]
    has_more: bool
    next_before_sequence: int | None

    @classmethod
    def from_model(cls, page: MessagePage) -> "MessagePageResponse":
        return cls(
            messages=[MessageResponse.from_model(m) for m in page.messages],
            has_more=page.has_more,
            next_before_sequence=page.next_before_sequence,
        )


class ContextRefResponse(BaseModel):
    id: str
    session_id: str
    message_id: str | None
    ref_type: str
    file_id: str | None
    symbol_id: str | None
    analysis_run_id: str | None
    finding_id: str | None
    graph_camera_state: dict[str, object] | None
    created_at: str

    @classmethod
    def from_model(cls, ref: ContextRefView) -> "ContextRefResponse":
        return cls(
            id=str(ref.id),
            session_id=str(ref.session_id),
            message_id=str(ref.message_id) if ref.message_id else None,
            ref_type=ref.ref_type,
            file_id=str(ref.file_id) if ref.file_id else None,
            symbol_id=str(ref.symbol_id) if ref.symbol_id else None,
            analysis_run_id=str(ref.analysis_run_id) if ref.analysis_run_id else None,
            finding_id=str(ref.finding_id) if ref.finding_id else None,
            graph_camera_state=ref.graph_camera_state,
            created_at=ref.created_at,
        )


class SessionResponse(BaseModel):
    id: str
    repository_id: str
    title: str | None
    status: str
    started_at_revision_id: str | None
    current_revision_id: str | None
    created_at: str
    last_active_at: str
    messages: MessagePageResponse
    context_refs: list[ContextRefResponse]

    @classmethod
    def from_model(cls, snapshot: SessionSnapshot) -> "SessionResponse":
        return cls(
            id=str(snapshot.id),
            repository_id=str(snapshot.repository_id),
            title=snapshot.title,
            status=snapshot.status,
            started_at_revision_id=(
                str(snapshot.started_at_revision_id) if snapshot.started_at_revision_id else None
            ),
            current_revision_id=(
                str(snapshot.current_revision_id) if snapshot.current_revision_id else None
            ),
            created_at=snapshot.created_at,
            last_active_at=snapshot.last_active_at,
            messages=MessagePageResponse.from_model(snapshot.message_page),
            context_refs=[ContextRefResponse.from_model(c) for c in snapshot.context_refs],
        )


class SessionSummaryResponse(BaseModel):
    id: str
    title: str | None
    status: str
    started_at_revision_id: str | None
    current_revision_id: str | None
    created_at: str
    last_active_at: str
    message_count: int

    @classmethod
    def from_model(cls, summary: SessionSummary) -> "SessionSummaryResponse":
        return cls(
            id=str(summary.id),
            title=summary.title,
            status=summary.status,
            started_at_revision_id=(
                str(summary.started_at_revision_id) if summary.started_at_revision_id else None
            ),
            current_revision_id=(
                str(summary.current_revision_id) if summary.current_revision_id else None
            ),
            created_at=summary.created_at,
            last_active_at=summary.last_active_at,
            message_count=summary.message_count,
        )


class RecentSessionsResponse(BaseModel):
    sessions: list[SessionSummaryResponse]
    total_count: int
    limit: int
    offset: int

    @classmethod
    def from_model(cls, page: RecentSessionsPage) -> "RecentSessionsResponse":
        return cls(
            sessions=[SessionSummaryResponse.from_model(s) for s in page.sessions],
            total_count=page.total_count,
            limit=page.limit,
            offset=page.offset,
        )


@router.post(
    "/repositories/{repository_id}/sessions",
    response_model=SessionResponse,
    dependencies=[Depends(rate_limited("session"))],
)
def create_session_endpoint(
    repository_id: str,
    payload: CreateSessionRequest,
    db: Annotated[Session, Depends(get_db)],
) -> SessionResponse:
    repo_uuid = _parse_uuid(repository_id, "repository_id")
    revision_uuid = (
        _parse_uuid(payload.revision_id, "revision_id") if payload.revision_id is not None else None
    )
    try:
        snapshot = create_session(db, repo_uuid, revision_uuid, payload.title)
    except InvestigationError as exc:
        raise _handle(exc) from exc
    return SessionResponse.from_model(snapshot)


@router.get("/repositories/{repository_id}/sessions", response_model=RecentSessionsResponse)
def list_sessions_endpoint(
    repository_id: str,
    db: Annotated[Session, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=MAX_RECENTS_PAGE_SIZE)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RecentSessionsResponse:
    repo_uuid = _parse_uuid(repository_id, "repository_id")
    try:
        page = list_recent_sessions(db, repo_uuid, limit, offset)
    except InvestigationError as exc:
        raise _handle(exc) from exc
    return RecentSessionsResponse.from_model(page)


@router.get("/repositories/{repository_id}/sessions/{session_id}", response_model=SessionResponse)
def get_session_endpoint(
    repository_id: str,
    session_id: str,
    db: Annotated[Session, Depends(get_db)],
    message_limit: Annotated[int, Query(ge=1, le=MAX_MESSAGE_PAGE_SIZE)] = 50,
    message_before_sequence: Annotated[int | None, Query(ge=1)] = None,
) -> SessionResponse:
    repo_uuid = _parse_uuid(repository_id, "repository_id")
    session_uuid = _parse_uuid(session_id, "session_id")
    try:
        snapshot = get_session(db, repo_uuid, session_uuid, message_limit, message_before_sequence)
    except InvestigationError as exc:
        raise _handle(exc) from exc
    return SessionResponse.from_model(snapshot)


@router.patch(
    "/repositories/{repository_id}/sessions/{session_id}",
    response_model=SessionResponse,
    dependencies=[Depends(rate_limited("session"))],
)
def update_session_endpoint(
    repository_id: str,
    session_id: str,
    payload: SessionUpdateRequest,
    db: Annotated[Session, Depends(get_db)],
) -> SessionResponse:
    repo_uuid = _parse_uuid(repository_id, "repository_id")
    session_uuid = _parse_uuid(session_id, "session_id")
    fields_set = payload.model_fields_set
    revision_uuid = (
        _parse_uuid(payload.revision_id, "revision_id")
        if "revision_id" in fields_set and payload.revision_id is not None
        else None
    )
    try:
        snapshot = update_session(
            db,
            repo_uuid,
            session_uuid,
            title=payload.title,
            title_set="title" in fields_set,
            status=payload.status,
            status_set="status" in fields_set,
            revision_id=revision_uuid,
            revision_id_set="revision_id" in fields_set,
        )
    except InvestigationError as exc:
        raise _handle(exc) from exc
    return SessionResponse.from_model(snapshot)


@router.post(
    "/repositories/{repository_id}/sessions/{session_id}/messages",
    response_model=MessageResponse,
    dependencies=[Depends(rate_limited("session"))],
)
def append_message_endpoint(
    repository_id: str,
    session_id: str,
    payload: AppendMessageRequest,
    db: Annotated[Session, Depends(get_db)],
) -> MessageResponse:
    repo_uuid = _parse_uuid(repository_id, "repository_id")
    session_uuid = _parse_uuid(session_id, "session_id")
    try:
        message = append_message(db, repo_uuid, session_uuid, payload.role, payload.content)
    except InvestigationError as exc:
        raise _handle(exc) from exc
    return MessageResponse.from_model(message)


@router.post(
    "/repositories/{repository_id}/sessions/{session_id}/context",
    response_model=ContextRefResponse,
    dependencies=[Depends(rate_limited("session"))],
)
def add_context_ref_endpoint(
    repository_id: str,
    session_id: str,
    payload: ContextRefRequest,
    db: Annotated[Session, Depends(get_db)],
) -> ContextRefResponse:
    repo_uuid = _parse_uuid(repository_id, "repository_id")
    session_uuid = _parse_uuid(session_id, "session_id")
    file_uuid = _parse_uuid(payload.file_id, "file_id") if payload.file_id is not None else None
    symbol_uuid = (
        _parse_uuid(payload.symbol_id, "symbol_id") if payload.symbol_id is not None else None
    )
    analysis_run_uuid = (
        _parse_uuid(payload.analysis_run_id, "analysis_run_id")
        if payload.analysis_run_id is not None
        else None
    )
    finding_uuid = (
        _parse_uuid(payload.finding_id, "finding_id") if payload.finding_id is not None else None
    )
    message_uuid = (
        _parse_uuid(payload.message_id, "message_id") if payload.message_id is not None else None
    )
    try:
        ref = add_context_ref(
            db,
            repo_uuid,
            session_uuid,
            payload.ref_type,
            file_id=file_uuid,
            symbol_id=symbol_uuid,
            analysis_run_id=analysis_run_uuid,
            finding_id=finding_uuid,
            graph_camera_state=payload.graph_camera_state,
            message_id=message_uuid,
        )
    except InvestigationError as exc:
        raise _handle(exc) from exc
    return ContextRefResponse.from_model(ref)


@router.delete(
    "/repositories/{repository_id}/sessions/{session_id}/context/{context_ref_id}",
    status_code=204,
    dependencies=[Depends(rate_limited("session"))],
)
def remove_context_ref_endpoint(
    repository_id: str,
    session_id: str,
    context_ref_id: str,
    db: Annotated[Session, Depends(get_db)],
) -> None:
    repo_uuid = _parse_uuid(repository_id, "repository_id")
    session_uuid = _parse_uuid(session_id, "session_id")
    context_ref_uuid = _parse_uuid(context_ref_id, "context_ref_id")
    try:
        remove_context_ref(db, repo_uuid, session_uuid, context_ref_uuid)
    except InvestigationError as exc:
        raise _handle(exc) from exc
