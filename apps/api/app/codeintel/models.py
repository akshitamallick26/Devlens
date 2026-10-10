"""Pure result shapes for the read-side code intelligence API (Product
Integration v1). Frozen dataclasses only; no SQLAlchemy objects. Every
list is bounded and carries an explicit truncation/pagination signal.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class RevisionEntry:
    revision_id: uuid.UUID
    commit_sha: str
    parent_sha: str | None
    branch_ref: str | None
    committed_at: datetime
    discovered_at: datetime
    latest_analysis_status: str | None
    file_count: int | None
    is_current: bool


@dataclass(frozen=True)
class RevisionPage:
    revisions: tuple[RevisionEntry, ...]
    total_count: int
    limit: int
    offset: int


@dataclass(frozen=True)
class FileEntry:
    file_id: uuid.UUID
    path: str
    language: str | None
    size_bytes: int
    line_count: int | None
    parsing_status: str


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    path: str
    file_count: int  # files anywhere below this directory


@dataclass(frozen=True)
class DirectoryListing:
    directory: str  # "" is the repository root
    directories: tuple[DirectoryEntry, ...]  # name asc
    directories_truncated: bool
    files: tuple[FileEntry, ...]  # files directly in `directory`, path asc
    total_files: int  # files directly in `directory`
    limit: int
    offset: int


@dataclass(frozen=True)
class SymbolEntry:
    symbol_id: uuid.UUID
    name: str
    qualified_name: str
    kind: str
    file_id: uuid.UUID
    file_path: str
    start_line: int
    end_line: int
    parent_symbol_id: uuid.UUID | None
    signature: str | None


@dataclass(frozen=True)
class FileDetail:
    file: FileEntry
    revision_id: uuid.UUID
    symbols: tuple[SymbolEntry, ...]  # start_line asc
    symbols_truncated: bool
    # False when the file's content is stored externally (storage_key) and
    # no source retrieval path exists; source is never fabricated.
    source_available: bool


@dataclass(frozen=True)
class SourceRange:
    file_id: uuid.UUID
    path: str
    start_line: int
    end_line: int  # inclusive; may be < requested when the file is shorter
    total_lines: int
    lines: tuple[str, ...]
    truncated: bool  # the requested range exceeded the maximum and was cut


@dataclass(frozen=True)
class SymbolDetail:
    symbol: SymbolEntry
    revision_id: uuid.UUID
    parent: SymbolEntry | None
    child_count: int
    # Distinct direct dependents / internal dependencies over internal
    # edges (same definition as the overview rankings); external
    # references are counted separately.
    dependent_count: int
    dependency_count: int
    external_reference_count: int


@dataclass(frozen=True)
class SymbolSearchResult:
    symbols: tuple[SymbolEntry, ...]
    truncated: bool


@dataclass(frozen=True)
class GraphNode:
    symbol_id: uuid.UUID
    file_id: uuid.UUID
    file_path: str
    qualified_name: str
    kind: str
    depth: int  # shortest hop count from the focus symbol (focus = 0)
    # Which walk reached the node: "focus", "dependents", "dependencies",
    # or "both" (reached walking each direction).
    reached_via: str


@dataclass(frozen=True)
class GraphEdgeView:
    """source = dependent, target = dependency (the GraphEdge invariant)."""

    source: uuid.UUID
    target: uuid.UUID
    relationship_type: str
    occurrences: int  # GraphEdge rows (distinct evidence locations) collapsed here


@dataclass(frozen=True)
class GraphNeighborhood:
    revision_id: uuid.UUID
    focus_symbol_id: uuid.UUID
    direction: str
    depth: int
    node_limit: int
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdgeView, ...]
    truncated: bool  # node limit or per-step edge cap was reached
    max_depth_reached: int
    # Unresolved targets of the focus symbol (e.g. stdlib imports). Names
    # only; never turned into nodes.
    focus_external_references: tuple[str, ...]


@dataclass(frozen=True)
class FindingEntry:
    finding_id: uuid.UUID
    analyzer: str
    rule_id: str
    severity: str | None
    title: str
    message: str
    triage_status: str  # repository-level SecurityFinding.status
    cvss_score: float | None
    observation_count: int
    first_location: str | None


@dataclass(frozen=True)
class FindingPage:
    findings_run_id: uuid.UUID | None  # None: no completed scan for this revision
    findings: tuple[FindingEntry, ...]
    total_count: int
    limit: int
    offset: int


@dataclass(frozen=True)
class ExecutionEntry:
    execution_id: uuid.UUID
    status: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int | None
    finished_at: datetime | None


@dataclass(frozen=True)
class GeneratedTestEntry:
    generated_test_id: uuid.UUID
    target_symbol_id: uuid.UUID
    target_qualified_name: str | None
    proposed_path: str
    status: str
    validation_status: str
    provider: str | None  # None only for rows whose provenance was never recorded
    model_name: str | None
    created_at: datetime
    latest_execution: ExecutionEntry | None


@dataclass(frozen=True)
class GeneratedTestPage:
    tests: tuple[GeneratedTestEntry, ...]
    total_count: int
    limit: int
    offset: int
