"""Pure data shapes shared across app/automation/ — no SQLAlchemy, no DB, no
AI provider. See docs/architecture for the full design.
"""

import uuid
from dataclasses import dataclass

from app.impact.models import ImpactedFile, ImpactedSymbol

PARENT_RESOLUTION_STATUSES = ("resolved", "no_parent", "mismatch", "not_found")
AUTOMATION_STATUSES = ("completed", "partial", "failed")

# Hardening Blocker 2: the only execution strategy this phase ever performs
# for revision-scoped parsing/graph construction/RAG chunking — see
# app/automation/service.py's module docstring. Never "incremental" or
# "partial" — no such mode exists in the underlying services.
EXECUTION_STRATEGIES = ("full_revision_rebuild",)

# Hardening Blocker 3: a deleted symbol's impact is never silently reported
# as zero — it is explicitly marked as not computable under the
# current-revision-only traversal invariant.
IMPACT_STATUSES = ("computed", "unavailable_deleted_symbol")


@dataclass(frozen=True)
class SymbolIdentity:
    """Revision-independent symbol identity (Task 2) — never a random
    Symbol UUID, which is freshly generated every revision even for
    byte-identical content."""

    file_path: str
    kind: str
    qualified_name: str


@dataclass(frozen=True)
class FileEntry:
    path: str
    file_id: uuid.UUID
    content_hash: str


@dataclass(frozen=True)
class FileDelta:
    added: tuple[FileEntry, ...]
    deleted: tuple[FileEntry, ...]
    # (parent_entry, current_entry) — same path, different content_hash.
    modified: tuple[tuple[FileEntry, FileEntry], ...]
    unchanged: tuple[FileEntry, ...]
    # Always "unsupported" in v1 — see app/automation/revision_delta.py's
    # module docstring for exactly why no rename-detection signal exists
    # in the current ingestion model.
    rename_detection: str = "unsupported"


@dataclass(frozen=True)
class SymbolSnapshot:
    identity: SymbolIdentity
    symbol_id: uuid.UUID
    file_id: uuid.UUID
    start_line: int
    end_line: int
    # `signature` (captures decorators/parameters/return type) + the sliced
    # body text, joined — never just the line span, and never just the
    # signature alone. `None` only when the underlying source text is
    # genuinely unavailable (storage_key-backed), in which case this
    # snapshot can never be proven "unchanged" — see symbol_delta.py.
    content: str | None


@dataclass(frozen=True)
class SymbolDelta:
    added: tuple[SymbolSnapshot, ...]
    deleted: tuple[SymbolSnapshot, ...]
    # (parent_snapshot, current_snapshot) — same identity, different content.
    modified: tuple[tuple[SymbolSnapshot, SymbolSnapshot], ...]
    unchanged: tuple[SymbolSnapshot, ...]


@dataclass(frozen=True)
class ParentResolution:
    status: str  # one of PARENT_RESOLUTION_STATUSES
    parent_revision_id: uuid.UUID | None
    detail: str | None


@dataclass(frozen=True)
class DeletedSymbolImpact:
    """Hardening Blocker 3: a deleted symbol has no node in the current
    graph to traverse from — its former dependents (who may still exist
    and may still be calling something that no longer exists) cannot be
    determined without traversing the *parent* graph, which this phase's
    "current revision's graph only" invariant explicitly forbids. This is
    reported as an explicit, honest gap — never silently folded into
    `ImpactSummary.direct`/`transitive` as if it were a symbol with zero
    impact."""

    symbol: SymbolSnapshot  # the parent-revision snapshot of the deleted symbol
    impact_status: str  # always "unavailable_deleted_symbol" for entries in this list
    reason: str


@dataclass(frozen=True)
class ImpactSummary:
    changed_symbol_count: int
    direct: tuple[ImpactedSymbol, ...]
    transitive: tuple[ImpactedSymbol, ...]
    affected_files: tuple[ImpactedFile, ...]
    max_depth: int
    # True if any one changed symbol's own impact traversal was bounded
    # before exhausting the graph (see app/impact's own depth/width
    # behavior — v1 Impact Analysis itself is unbounded, so this is
    # currently always False; kept explicit rather than silently assumed
    # for when that changes).
    truncated: bool
    # Deleted symbols get no entry in `direct`/`transitive` at all (they
    # have no current-graph node) — this is where their explicit
    # "impact could not be computed" status lives instead.
    deleted_symbol_impacts: tuple[DeletedSymbolImpact, ...] = ()


@dataclass(frozen=True)
class StageExecutionPlan:
    """Hardening Blocker 2: separates the INVALIDATION DECISION (did this
    stage's inputs actually change, i.e. is there a real reason derived
    data needs refreshing) from the EXECUTION STRATEGY actually used (in
    v1, always a full revision rebuild — parsing/graph construction/RAG
    chunking have no partial mode, and a brand-new revision has zero rows
    of its own in any of them regardless of how little changed). A caller
    must never infer "unchanged files were selectively skipped" merely
    because `invalidated` is `False` — `execution_strategy` is reported
    unconditionally and explicitly for exactly that reason."""

    stage: str  # "parsing" | "graph_construction" | "rag_chunking"
    invalidated: bool  # True if any added/modified/deleted file gave a real reason to recompute
    execution_strategy: str  # one of EXECUTION_STRATEGIES — always run, regardless of `invalidated`
    status: str  # "completed" | "failed" | "skipped"
    detail: str | None


@dataclass(frozen=True)
class RAGInvalidationPlan:
    status: str  # "rebuilt" | "skipped_no_changes" | "failed"
    chunks_invalidated: int
    embeddings_require_regeneration: bool
    execution_strategy: str  # one of EXECUTION_STRATEGIES
    detail: str | None


@dataclass(frozen=True)
class TestingInvalidationPlan:
    __test__ = False  # not a test class — see app/testing/models.py::TestTarget

    # GeneratedTest ids from the *parent* revision whose target symbol was
    # deleted, modified, or whose symbol identity no longer resolves in the
    # current revision — these must never be presented as valid for the
    # current revision.
    stale_generated_test_ids: tuple[uuid.UUID, ...]
    # Current-revision symbol ids that are new/changed and would need a
    # freshly generated test if one is wanted — identification only, no
    # regeneration is triggered by this phase.
    changed_target_symbol_ids: tuple[uuid.UUID, ...]
    detail: str | None


@dataclass(frozen=True)
class SecurityInvalidationPlan:
    rerun_required: bool
    reason: str
    detail: str | None


@dataclass(frozen=True)
class DeterministicChangeSummary:
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
    deleted_symbols_with_unavailable_impact: int = 0


@dataclass(frozen=True)
class AutomationRunResult:
    repository_id: uuid.UUID
    current_revision_id: uuid.UUID
    parent_revision_id: uuid.UUID | None
    analysis_run_id: uuid.UUID | None
    analysis_status: str  # one of AUTOMATION_STATUSES
    file_delta: FileDelta
    symbol_delta: SymbolDelta
    parsing_execution: StageExecutionPlan
    graph_execution: StageExecutionPlan
    impact_summary: ImpactSummary | None
    rag_invalidation: RAGInvalidationPlan
    testing_invalidation: TestingInvalidationPlan
    security_invalidation: SecurityInvalidationPlan
    summary: DeterministicChangeSummary
    limitations: tuple[str, ...]
