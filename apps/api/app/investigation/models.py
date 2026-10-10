"""Pure data shapes for app/investigation/ — no SQLAlchemy, no DB. See
docs/architecture/investigation-sessions.md for the full session contract.
"""

import uuid
from dataclasses import dataclass, field

# Mirrors app.models.investigation's own constants exactly — duplicated
# (not imported) because this module is deliberately ORM-free, the same
# separation app.automation.models keeps from app.models.*.
SESSION_STATUSES = ("active", "archived")
MESSAGE_ROLES = ("user", "assistant", "system")
REF_TYPES = (
    "selected_file",
    "selected_symbol",
    "graph_focus",
    "analysis_reference",
    "finding_reference",
)

MAX_TITLE_LENGTH = 200
MAX_MESSAGE_CONTENT_LENGTH = 20_000
DEFAULT_MESSAGE_PAGE_SIZE = 50
MAX_MESSAGE_PAGE_SIZE = 200
DEFAULT_RECENTS_PAGE_SIZE = 20
MAX_RECENTS_PAGE_SIZE = 100
MAX_CONTEXT_REFS_IN_SNAPSHOT = 500
# graph_camera_state is stored as visitor-supplied JSON. The workstation
# writes four short fields (~200 bytes); this bounds what one request can
# persist, the same role MAX_MESSAGE_CONTENT_LENGTH plays for messages.
MAX_GRAPH_CAMERA_STATE_BYTES = 4_096


@dataclass(frozen=True)
class MessageView:
    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    sequence_number: int
    created_at: str  # ISO 8601 — service layer formats, keeps this module datetime-free


@dataclass(frozen=True)
class MessagePage:
    messages: tuple[MessageView, ...]
    has_more: bool
    next_before_sequence: int | None


@dataclass(frozen=True)
class ContextRefView:
    id: uuid.UUID
    session_id: uuid.UUID
    message_id: uuid.UUID | None
    ref_type: str
    file_id: uuid.UUID | None
    symbol_id: uuid.UUID | None
    analysis_run_id: uuid.UUID | None
    finding_id: uuid.UUID | None
    graph_camera_state: dict[str, object] | None
    created_at: str


@dataclass(frozen=True)
class SessionSummary:
    """One row of a recents listing — deliberately excludes messages/context
    (Task 9: avoid N+1 queries when listing recents; a summary row only
    carries what's cheap to compute in one aggregated query)."""

    id: uuid.UUID
    repository_id: uuid.UUID
    title: str | None
    status: str
    started_at_revision_id: uuid.UUID | None
    current_revision_id: uuid.UUID | None
    created_at: str
    last_active_at: str
    message_count: int


@dataclass(frozen=True)
class RecentSessionsPage:
    sessions: tuple[SessionSummary, ...]
    total_count: int
    limit: int
    offset: int


@dataclass(frozen=True)
class SessionSnapshot:
    """Task 13 (context snapshot): everything a future UI needs to resume a
    session — repository identity, revision context, a bounded message
    page, and typed context-ref breadcrumbs. Never embeds full source text,
    graph payloads, or analysis results — those stay referenced by id only."""

    id: uuid.UUID
    repository_id: uuid.UUID
    title: str | None
    status: str
    started_at_revision_id: uuid.UUID | None
    current_revision_id: uuid.UUID | None
    created_at: str
    last_active_at: str
    message_page: MessagePage
    context_refs: tuple[ContextRefView, ...] = field(default_factory=tuple)
