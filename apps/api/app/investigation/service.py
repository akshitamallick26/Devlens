"""Investigation Sessions & Recents v1 service layer. Every decision lives
here — app/api/routes/investigation.py only translates typed exceptions
into HTTP and serializes the pure result shapes from app/investigation/
models.py. See docs/architecture/investigation-sessions.md for the full
session contract this module implements.

Revision semantics (section 5 of the contract, already settled by
docs/architecture/domain-model.md section 15): a session's
`current_revision_id` only ever moves via an explicit call to
`update_session(..., revision_id=...)`. Nothing in this module — not
`get_session`, not `append_message`, not `add_context_ref` — ever reads
`Repository.current_revision_id` to silently advance an existing session.
`create_session` is the one place a repository's current revision is
consulted, and only as a one-time default when the caller supplies no
explicit `revision_id` — never as an ongoing sync.

Context-reference repository consistency (section 7): `SessionContextRef`
has no FK that can enforce "this file/symbol/run/finding belongs to the
session's own repository" at the database layer (see
app/models/investigation.py's docstring — the schema is deliberately
preserved as-is, per the hardening brief's "if the existing one-hot
constraints are already sound, preserve them"). `add_context_ref` is the
single, deliberate enforcement point instead: it loads the referenced row
and compares its own `repository_id` column (every one of
SourceFile/Symbol/AnalysisRun/SecurityFinding carries one) against the
session's `repository_id` before ever constructing the reference row.
Cross-*revision* references are intentionally NOT rejected here — an older
revision's symbol remains a legitimate breadcrumb of "what was true at the
time" (see the existing, deliberate test
tests/db/test_session_context.py::test_context_ref_may_point_at_an_older_revision_than_the_sessions_current_one).
Revision consistency for revision-scoped entities is therefore fully
captured by the repository check: every revision belongs to exactly one
repository, so once repository identity matches, there is no further
"wrong revision" case left to reject for a historical breadcrumb.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.investigation.models import (
    MAX_CONTEXT_REFS_IN_SNAPSHOT,
    MAX_MESSAGE_CONTENT_LENGTH,
    MAX_MESSAGE_PAGE_SIZE,
    MAX_RECENTS_PAGE_SIZE,
    MAX_TITLE_LENGTH,
    MESSAGE_ROLES,
    REF_TYPES,
    SESSION_STATUSES,
    ContextRefView,
    MessagePage,
    MessageView,
    RecentSessionsPage,
    SessionSnapshot,
    SessionSummary,
)
from app.models import (
    AnalysisRun,
    InvestigationSession,
    Repository,
    RepositoryRevision,
    SecurityFinding,
    SessionContextRef,
    SessionMessage,
    SourceFile,
    Symbol,
)


class InvestigationError(Exception):
    pass


class RepositoryNotFoundError(InvestigationError):
    pass


class SessionNotFoundError(InvestigationError):
    pass


class RevisionNotFoundError(InvestigationError):
    pass


class RevisionConflictError(InvestigationError):
    """The requested revision exists but belongs to a different repository."""


class InvalidUpdateError(InvestigationError):
    pass


class InvalidRoleError(InvestigationError):
    pass


class InvalidContextRefError(InvestigationError):
    pass


class ReferencedEntityNotFoundError(InvestigationError):
    pass


class CrossRepositoryReferenceError(InvestigationError):
    """The referenced file/symbol/run/finding exists but belongs to a
    different repository than the session (section 7's core invariant)."""


class CrossSessionReferenceError(InvestigationError):
    """A supplied message_id exists but belongs to a different session."""


class ContextRefNotFoundError(InvestigationError):
    pass


def _get_repository_or_raise(db: Session, repository_id: uuid.UUID) -> Repository:
    repo = db.get(Repository, repository_id)
    if repo is None:
        raise RepositoryNotFoundError(f"no repository {repository_id}")
    return repo


def _get_session_or_raise(
    db: Session, repository_id: uuid.UUID, session_id: uuid.UUID
) -> InvestigationSession:
    session_row = db.get(InvestigationSession, session_id)
    if session_row is None or session_row.repository_id != repository_id:
        raise SessionNotFoundError(f"no session {session_id} for repository {repository_id}")
    return session_row


def _resolve_revision_for_repository(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> RepositoryRevision:
    revision = db.get(RepositoryRevision, revision_id)
    if revision is None:
        raise RevisionNotFoundError(f"no revision {revision_id}")
    if revision.repository_id != repository_id:
        raise RevisionConflictError(
            f"revision {revision_id} belongs to a different repository than {repository_id}"
        )
    return revision


def _iso(value: datetime) -> str:
    return value.isoformat()


def _message_view(message: SessionMessage) -> MessageView:
    return MessageView(
        id=message.id,
        session_id=message.session_id,
        role=message.role,
        content=message.content,
        sequence_number=message.sequence_number,
        created_at=_iso(message.created_at),
    )


def _context_ref_view(ref: SessionContextRef) -> ContextRefView:
    return ContextRefView(
        id=ref.id,
        session_id=ref.session_id,
        message_id=ref.message_id,
        ref_type=ref.ref_type,
        file_id=ref.file_id,
        symbol_id=ref.symbol_id,
        analysis_run_id=ref.analysis_run_id,
        finding_id=ref.finding_id,
        graph_camera_state=ref.graph_camera_state,
        created_at=_iso(ref.created_at),
    )


def _summary_view(session_row: InvestigationSession, message_count: int) -> SessionSummary:
    return SessionSummary(
        id=session_row.id,
        repository_id=session_row.repository_id,
        title=session_row.title,
        status=session_row.status,
        started_at_revision_id=session_row.started_at_revision_id,
        current_revision_id=session_row.current_revision_id,
        created_at=_iso(session_row.created_at),
        last_active_at=_iso(session_row.last_active_at),
        message_count=message_count,
    )


def _load_message_page(
    db: Session,
    session_id: uuid.UUID,
    limit: int,
    before_sequence: int | None,
) -> MessagePage:
    bounded_limit = max(1, min(limit, MAX_MESSAGE_PAGE_SIZE))
    stmt = select(SessionMessage).where(SessionMessage.session_id == session_id)
    if before_sequence is not None:
        stmt = stmt.where(SessionMessage.sequence_number < before_sequence)
    stmt = stmt.order_by(SessionMessage.sequence_number.desc()).limit(bounded_limit + 1)

    rows = list(db.execute(stmt).scalars().all())
    has_more = len(rows) > bounded_limit
    rows = rows[:bounded_limit]
    rows.reverse()  # chronological (ascending sequence_number) for the returned window

    next_before_sequence = rows[0].sequence_number if has_more and rows else None
    return MessagePage(
        messages=tuple(_message_view(m) for m in rows),
        has_more=has_more,
        next_before_sequence=next_before_sequence,
    )


def _load_context_refs(db: Session, session_id: uuid.UUID) -> tuple[ContextRefView, ...]:
    # Context refs are reference-only breadcrumbs (ids + short metadata, never
    # duplicated source/graph/analysis payloads), so a far higher ceiling
    # than messages is appropriate — this cap exists only to keep the
    # snapshot response bounded against a pathological outlier session, not
    # because normal usage approaches it.
    rows = (
        db.execute(
            select(SessionContextRef)
            .where(SessionContextRef.session_id == session_id)
            .order_by(SessionContextRef.created_at, SessionContextRef.id)
            .limit(MAX_CONTEXT_REFS_IN_SNAPSHOT)
        )
        .scalars()
        .all()
    )
    return tuple(_context_ref_view(r) for r in rows)


def _to_snapshot(
    session_row: InvestigationSession,
    message_page: MessagePage,
    context_refs: tuple[ContextRefView, ...],
) -> SessionSnapshot:
    return SessionSnapshot(
        id=session_row.id,
        repository_id=session_row.repository_id,
        title=session_row.title,
        status=session_row.status,
        started_at_revision_id=session_row.started_at_revision_id,
        current_revision_id=session_row.current_revision_id,
        created_at=_iso(session_row.created_at),
        last_active_at=_iso(session_row.last_active_at),
        message_page=message_page,
        context_refs=context_refs,
    )


def _reject_nul(field: str, value: str) -> None:
    """PostgreSQL `text` cannot store U+0000, so such input is rejected
    before any database work rather than surfacing as a DataError at
    commit. The message names the field only and never echoes the
    (untrusted) value."""
    if "\x00" in value:
        raise InvalidUpdateError(f"{field} must not contain NUL (U+0000) characters")


def _reject_nul_in_json(field: str, value: object) -> None:
    """Applies `_reject_nul` to every key and string value of a JSON-shaped
    value (PostgreSQL JSONB rejects \u0000 anywhere, keys included).
    Iterative, so a deeply nested payload cannot exhaust the stack."""
    pending: list[object] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            _reject_nul(field, item)
        elif isinstance(item, dict):
            for key, nested in item.items():
                if isinstance(key, str):
                    _reject_nul(field, key)
                pending.append(nested)
        elif isinstance(item, (list, tuple)):
            pending.extend(item)


def create_session(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID | None = None,
    title: str | None = None,
) -> SessionSnapshot:
    # Before any database work (see _reject_nul).
    if title is not None:
        _reject_nul("title", title)
    repo = _get_repository_or_raise(db, repository_id)
    if title is not None and len(title) > MAX_TITLE_LENGTH:
        raise InvalidUpdateError(f"title exceeds {MAX_TITLE_LENGTH} characters")

    if revision_id is not None:
        resolved_revision_id: uuid.UUID | None = _resolve_revision_for_repository(
            db, repository_id, revision_id
        ).id
    else:
        # One-time default at creation only — never an ongoing sync. See
        # this module's docstring.
        resolved_revision_id = repo.current_revision_id

    session_row = InvestigationSession(
        repository_id=repository_id,
        started_at_revision_id=resolved_revision_id,
        current_revision_id=resolved_revision_id,
        title=title,
    )
    db.add(session_row)
    db.commit()
    db.refresh(session_row)
    empty_page = MessagePage(messages=(), has_more=False, next_before_sequence=None)
    return _to_snapshot(session_row, empty_page, ())


def get_session(
    db: Session,
    repository_id: uuid.UUID,
    session_id: uuid.UUID,
    message_limit: int = 50,
    message_before_sequence: int | None = None,
) -> SessionSnapshot:
    session_row = _get_session_or_raise(db, repository_id, session_id)
    message_page = _load_message_page(db, session_row.id, message_limit, message_before_sequence)
    context_refs = _load_context_refs(db, session_row.id)
    return _to_snapshot(session_row, message_page, context_refs)


def list_recent_sessions(
    db: Session,
    repository_id: uuid.UUID,
    limit: int = 20,
    offset: int = 0,
) -> RecentSessionsPage:
    _get_repository_or_raise(db, repository_id)
    bounded_limit = max(1, min(limit, MAX_RECENTS_PAGE_SIZE))
    bounded_offset = max(0, offset)

    total_count = db.execute(
        select(func.count())
        .select_from(InvestigationSession)
        .where(InvestigationSession.repository_id == repository_id)
    ).scalar_one()

    # One aggregated query for message counts (not one query per session) —
    # Task 9's explicit "avoid N+1 queries when listing recents."
    count_subquery = (
        select(
            SessionMessage.session_id.label("session_id"),
            func.count(SessionMessage.id).label("message_count"),
        )
        .join(InvestigationSession, InvestigationSession.id == SessionMessage.session_id)
        .where(InvestigationSession.repository_id == repository_id)
        .group_by(SessionMessage.session_id)
        .subquery()
    )
    stmt = (
        select(InvestigationSession, func.coalesce(count_subquery.c.message_count, 0))
        .outerjoin(count_subquery, count_subquery.c.session_id == InvestigationSession.id)
        .where(InvestigationSession.repository_id == repository_id)
        .order_by(InvestigationSession.last_active_at.desc(), InvestigationSession.id.desc())
        .limit(bounded_limit)
        .offset(bounded_offset)
    )
    rows = db.execute(stmt).all()
    summaries = tuple(_summary_view(row[0], row[1]) for row in rows)
    return RecentSessionsPage(
        sessions=summaries, total_count=total_count, limit=bounded_limit, offset=bounded_offset
    )


def update_session(
    db: Session,
    repository_id: uuid.UUID,
    session_id: uuid.UUID,
    *,
    title: str | None = None,
    title_set: bool = False,
    status: str | None = None,
    status_set: bool = False,
    revision_id: uuid.UUID | None = None,
    revision_id_set: bool = False,
) -> SessionSnapshot:
    """One deterministic orchestrator for every PATCH field (title/status/
    revision) rather than three separately-committing functions — guarantees
    a single `last_active_at` bump and a single transaction per request, no
    matter how many fields were supplied together. `*_set` flags distinguish
    "field omitted" from "field explicitly provided" (title may legitimately
    be set to `None`)."""
    if not (title_set or status_set or revision_id_set):
        raise InvalidUpdateError("at least one of title, status, revision_id must be provided")
    # Before any database work: the session is not even loaded, so nothing
    # can be partially assigned or bump recency.
    if title_set and title is not None:
        _reject_nul("title", title)

    session_row = _get_session_or_raise(db, repository_id, session_id)
    bump_recency = False

    if title_set:
        if title is not None and len(title) > MAX_TITLE_LENGTH:
            raise InvalidUpdateError(f"title exceeds {MAX_TITLE_LENGTH} characters")
        session_row.title = title
        bump_recency = True

    if revision_id_set:
        assert revision_id is not None
        revision = _resolve_revision_for_repository(db, repository_id, revision_id)
        session_row.current_revision_id = revision.id
        bump_recency = True

    if status_set:
        assert status is not None
        if status not in SESSION_STATUSES:
            raise InvalidUpdateError(f"status must be one of {SESSION_STATUSES}")
        session_row.status = status
        # Archiving deliberately does NOT bump recency — putting a session
        # away should not bubble it to the top of "recent" (Task 9).
        # Reactivating is a genuine resumption, so it does.
        if status != "archived":
            bump_recency = True

    if bump_recency:
        session_row.last_active_at = datetime.now(UTC)
    db.commit()
    db.refresh(session_row)

    message_page = _load_message_page(db, session_row.id, 50, None)
    context_refs = _load_context_refs(db, session_row.id)
    return _to_snapshot(session_row, message_page, context_refs)


def append_message(
    db: Session,
    repository_id: uuid.UUID,
    session_id: uuid.UUID,
    role: str,
    content: str,
) -> MessageView:
    if role not in MESSAGE_ROLES:
        raise InvalidRoleError(f"role must be one of {MESSAGE_ROLES}")
    if len(content) > MAX_MESSAGE_CONTENT_LENGTH:
        raise InvalidUpdateError(f"content exceeds {MAX_MESSAGE_CONTENT_LENGTH} characters")
    # Before the session row lock below: an unstorable message takes no
    # lock, consumes no sequence number and leaves recency untouched.
    _reject_nul("content", content)

    # Row-lock the session for the duration of this transaction so two
    # concurrent appends to the SAME session serialize on sequence-number
    # assignment — an ordinary Postgres row lock, not a distributed lock
    # (Task 14). The UniqueConstraint(session_id, sequence_number) already
    # on this table remains the real backstop either way.
    session_row = db.execute(
        select(InvestigationSession).where(InvestigationSession.id == session_id).with_for_update()
    ).scalar_one_or_none()
    if session_row is None or session_row.repository_id != repository_id:
        raise SessionNotFoundError(f"no session {session_id} for repository {repository_id}")

    next_sequence = db.execute(
        select(func.coalesce(func.max(SessionMessage.sequence_number), 0) + 1).where(
            SessionMessage.session_id == session_id
        )
    ).scalar_one()

    message = SessionMessage(
        session_id=session_id, role=role, content=content, sequence_number=next_sequence
    )
    db.add(message)
    session_row.last_active_at = datetime.now(UTC)
    db.commit()
    db.refresh(message)
    return _message_view(message)


def add_context_ref(
    db: Session,
    repository_id: uuid.UUID,
    session_id: uuid.UUID,
    ref_type: str,
    *,
    file_id: uuid.UUID | None = None,
    symbol_id: uuid.UUID | None = None,
    analysis_run_id: uuid.UUID | None = None,
    finding_id: uuid.UUID | None = None,
    graph_camera_state: dict[str, object] | None = None,
    message_id: uuid.UUID | None = None,
) -> ContextRefView:
    if ref_type not in REF_TYPES:
        raise InvalidContextRefError(f"ref_type must be one of {REF_TYPES}")
    # Camera state is persisted only for graph_focus refs; checked before
    # any database work (session lookup, recency, ref insert).
    if ref_type == "graph_focus" and graph_camera_state is not None:
        _reject_nul_in_json("graph_camera_state", graph_camera_state)
    session_row = _get_session_or_raise(db, repository_id, session_id)

    if message_id is not None:
        message = db.get(SessionMessage, message_id)
        if message is None:
            raise ReferencedEntityNotFoundError(f"no message {message_id}")
        if message.session_id != session_id:
            raise CrossSessionReferenceError(f"message {message_id} belongs to a different session")

    if ref_type == "graph_focus":
        if graph_camera_state is None:
            raise InvalidContextRefError("graph_focus requires graph_camera_state")
        ref = SessionContextRef(
            session_id=session_id,
            message_id=message_id,
            ref_type=ref_type,
            graph_camera_state=graph_camera_state,
        )
        db.add(ref)
        session_row.last_active_at = datetime.now(UTC)
        db.commit()
        db.refresh(ref)
        return _context_ref_view(ref)

    if ref_type == "selected_file":
        if file_id is None:
            raise InvalidContextRefError("selected_file requires file_id")
        referenced_file = db.get(SourceFile, file_id)
        if referenced_file is None:
            raise ReferencedEntityNotFoundError(f"no SourceFile {file_id}")
        if referenced_file.repository_id != repository_id:
            raise CrossRepositoryReferenceError(
                f"SourceFile {file_id} belongs to a different repository"
            )
        index_elements = ["session_id", "file_id"]
        index_where = text("ref_type = 'selected_file'")
    elif ref_type == "selected_symbol":
        if symbol_id is None:
            raise InvalidContextRefError("selected_symbol requires symbol_id")
        referenced_symbol = db.get(Symbol, symbol_id)
        if referenced_symbol is None:
            raise ReferencedEntityNotFoundError(f"no Symbol {symbol_id}")
        if referenced_symbol.repository_id != repository_id:
            raise CrossRepositoryReferenceError(
                f"Symbol {symbol_id} belongs to a different repository"
            )
        index_elements = ["session_id", "symbol_id"]
        index_where = text("ref_type = 'selected_symbol'")
    elif ref_type == "analysis_reference":
        if analysis_run_id is None:
            raise InvalidContextRefError("analysis_reference requires analysis_run_id")
        referenced_run = db.get(AnalysisRun, analysis_run_id)
        if referenced_run is None:
            raise ReferencedEntityNotFoundError(f"no AnalysisRun {analysis_run_id}")
        if referenced_run.repository_id != repository_id:
            raise CrossRepositoryReferenceError(
                f"AnalysisRun {analysis_run_id} belongs to a different repository"
            )
        index_elements = ["session_id", "analysis_run_id"]
        index_where = text("ref_type = 'analysis_reference'")
    else:  # finding_reference
        if finding_id is None:
            raise InvalidContextRefError("finding_reference requires finding_id")
        referenced_finding = db.get(SecurityFinding, finding_id)
        if referenced_finding is None:
            raise ReferencedEntityNotFoundError(f"no SecurityFinding {finding_id}")
        if referenced_finding.repository_id != repository_id:
            raise CrossRepositoryReferenceError(
                f"SecurityFinding {finding_id} belongs to a different repository"
            )
        index_elements = ["session_id", "finding_id"]
        index_where = text("ref_type = 'finding_reference'")

    stmt = (
        pg_insert(SessionContextRef)
        .values(
            session_id=session_id,
            message_id=message_id,
            ref_type=ref_type,
            file_id=file_id,
            symbol_id=symbol_id,
            analysis_run_id=analysis_run_id,
            finding_id=finding_id,
            graph_camera_state=None,
        )
        .on_conflict_do_nothing(index_elements=index_elements, index_where=index_where)
        .returning(SessionContextRef.id)
    )
    inserted_id = db.execute(stmt).scalar_one_or_none()

    if inserted_id is None:
        # Already exists for this (session, ref_type, entity) pair — adding
        # the same context reference twice is idempotent (Task 14): return
        # the existing row, never a duplicate, never an error.
        column = {
            "selected_file": SessionContextRef.file_id,
            "selected_symbol": SessionContextRef.symbol_id,
            "analysis_reference": SessionContextRef.analysis_run_id,
            "finding_reference": SessionContextRef.finding_id,
        }[ref_type]
        existing_id = file_id or symbol_id or analysis_run_id or finding_id
        existing = db.execute(
            select(SessionContextRef).where(
                SessionContextRef.session_id == session_id,
                SessionContextRef.ref_type == ref_type,
                column == existing_id,
            )
        ).scalar_one()
        db.commit()
        return _context_ref_view(existing)

    session_row.last_active_at = datetime.now(UTC)
    db.commit()
    created_ref = db.get(SessionContextRef, inserted_id)
    assert created_ref is not None
    return _context_ref_view(created_ref)


def remove_context_ref(
    db: Session,
    repository_id: uuid.UUID,
    session_id: uuid.UUID,
    context_ref_id: uuid.UUID,
) -> None:
    session_row = _get_session_or_raise(db, repository_id, session_id)
    ref = db.get(SessionContextRef, context_ref_id)
    if ref is None or ref.session_id != session_row.id:
        raise ContextRefNotFoundError(f"no context ref {context_ref_id} for session {session_id}")
    db.delete(ref)
    session_row.last_active_at = datetime.now(UTC)
    db.commit()
