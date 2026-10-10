"""Pure result shapes for Repository Overview v1 — frozen dataclasses, no
SQLAlchemy objects, no DB logic. Every field is either a direct persisted
fact or a deterministic derivation of persisted facts (see
docs/architecture/repository-overview.md section 3); nothing here is
LLM-authored, scored, or confidence-weighted.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

# "not_run" is the only value added on top of AnalysisRun.status's own
# vocabulary — it means "no AnalysisRun row of this type exists for this
# revision", never "healthy".
NOT_RUN = "not_run"


@dataclass(frozen=True)
class RepositorySummary:
    """Repository-current metadata (not revision-local)."""

    repository_id: uuid.UUID
    owner: str
    name: str
    url: str
    default_branch: str
    status: str
    current_revision_id: uuid.UUID | None


@dataclass(frozen=True)
class RevisionSummary:
    revision_id: uuid.UUID
    commit_sha: str
    parent_sha: str | None
    branch_ref: str | None
    committed_at: datetime
    discovered_at: datetime
    # Requested revision == Repository.current_revision_id at read time.
    is_current: bool
    latest_analysis_status: str | None


@dataclass(frozen=True)
class CountByKey:
    key: str
    count: int


@dataclass(frozen=True)
class RepositoryStatistics:
    # SourceFile rows stored for this revision (ingestion stores text files
    # only; the raw git tracked-file count is not persisted anywhere).
    stored_file_count: int
    # RepositoryRevision.file_count as recorded by ingestion (None if
    # ingestion never completed for this revision).
    recorded_file_count: int | None
    language_file_count: int  # stored files with a non-NULL language
    total_source_bytes: int
    files_by_language: tuple[CountByKey, ...]  # count desc, key asc; NULL -> "unknown"
    files_by_parsing_status: tuple[CountByKey, ...]  # key asc
    symbol_count: int
    symbols_by_kind: tuple[CountByKey, ...]  # count desc, key asc


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    file_count: int
    total_bytes: int
    symbol_count: int


@dataclass(frozen=True)
class PackageEntry:
    """A top-level Python package: a parser-emitted kind="module" symbol
    whose file is `<name>/__init__.py`."""

    qualified_name: str
    file_path: str


@dataclass(frozen=True)
class RepositoryStructure:
    root_file_count: int  # stored files with no directory component
    top_level_directory_count: int
    top_level_directories: tuple[DirectoryEntry, ...]  # file_count desc, name asc
    top_level_directories_truncated: bool
    python_module_count: int  # parser-emitted kind="module" symbols
    top_level_packages: tuple[PackageEntry, ...]  # qualified_name asc
    top_level_packages_truncated: bool


@dataclass(frozen=True)
class GraphSummary:
    edge_count: int
    internal_edge_count: int  # target_symbol_id IS NOT NULL
    external_edge_count: int  # target_external_ref only
    edges_by_relationship: tuple[CountByKey, ...]  # count desc, key asc
    node_count: int  # Symbol rows in the revision (every edge end is a Symbol)
    connected_node_count: int  # symbols that appear on at least one internal edge


@dataclass(frozen=True)
class ImportantComponent:
    """in_degree = distinct other symbols with an internal edge targeting
    this symbol (direct dependents). out_degree = distinct other symbols
    this symbol has an internal edge to (direct internal dependencies)."""

    symbol_id: uuid.UUID
    qualified_name: str
    kind: str
    file_path: str
    in_degree: int
    out_degree: int


@dataclass(frozen=True)
class ModuleComponent:
    """Module = SourceFile. in_degree = distinct other files containing a
    symbol with an internal edge into this file; out_degree = distinct other
    files this file's symbols have an internal edge to. `module_name` is the
    parser-emitted kind="module" symbol's qualified_name (None for files the
    parser emitted no module symbol for)."""

    file_id: uuid.UUID
    file_path: str
    module_name: str | None
    in_degree: int
    out_degree: int


@dataclass(frozen=True)
class ImportantComponents:
    most_depended_upon_symbols: tuple[ImportantComponent, ...]
    most_depended_upon_symbols_truncated: bool
    most_dependency_heavy_symbols: tuple[ImportantComponent, ...]
    most_dependency_heavy_symbols_truncated: bool
    most_depended_upon_modules: tuple[ModuleComponent, ...]
    most_depended_upon_modules_truncated: bool
    most_dependency_heavy_modules: tuple[ModuleComponent, ...]
    most_dependency_heavy_modules_truncated: bool


@dataclass(frozen=True)
class AnalysisStatusSummary:
    analysis_type: str
    status: str  # an AnalysisRun.status value, or NOT_RUN
    analysis_run_id: uuid.UUID | None
    analyzer_name: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None


@dataclass(frozen=True)
class SecuritySummary:
    latest_run: AnalysisStatusSummary
    # The latest *completed* security_scan run for this revision whose
    # observations the counts below come from (None -> no counts).
    findings_run_id: uuid.UUID | None
    finding_count: int
    findings_by_severity: tuple[CountByKey, ...]  # fixed severity order; NULL -> "unknown"
    # SecurityFinding.status is repository-level triage state (current, not
    # revision-local), so this breakdown is labelled as such.
    findings_by_current_triage_status: tuple[CountByKey, ...]  # key asc


@dataclass(frozen=True)
class GeneratedTestSummary:
    latest_run: AnalysisStatusSummary
    generated_test_count: int
    generated_tests_by_status: tuple[CountByKey, ...]  # key asc
    execution_count: int
    executions_by_status: tuple[CountByKey, ...]  # key asc


@dataclass(frozen=True)
class DocumentationSummary:
    latest_run: AnalysisStatusSummary
    artifact_count: int
    artifacts_by_type: tuple[CountByKey, ...]  # key asc
    artifacts_by_generation_method: tuple[CountByKey, ...]  # key asc
    latest_artifact_updated_at: datetime | None


@dataclass(frozen=True)
class RagSummary:
    chunking_run: AnalysisStatusSummary
    embedding_run: AnalysisStatusSummary
    chunk_count: int
    indexed_file_count: int
    embedding_count: int


@dataclass(frozen=True)
class AutomationSummary:
    latest_run: AnalysisStatusSummary
    parent_revision_id: uuid.UUID | None
    # Copied from the run's persisted config only when present and integral;
    # None means "not recorded", never zero.
    changed_file_count: int | None
    changed_symbol_count: int | None
    impacted_symbol_count: int | None
    affected_file_count: int | None
    limitation_count: int | None


@dataclass(frozen=True)
class RecentSession:
    session_id: uuid.UUID
    title: str | None
    status: str
    last_active_at: str
    message_count: int


@dataclass(frozen=True)
class RecentSessionSummary:
    """Repository-scoped (sessions are not revision-local)."""

    session_count: int
    recent_sessions: tuple[RecentSession, ...]


@dataclass(frozen=True)
class RepositoryOverview:
    repository: RepositorySummary
    revision: RevisionSummary
    statistics: RepositoryStatistics
    structure: RepositoryStructure
    graph: GraphSummary
    important_components: ImportantComponents
    analysis_statuses: tuple[AnalysisStatusSummary, ...]  # AnalysisRun.ANALYSIS_TYPES order
    security: SecuritySummary
    testing: GeneratedTestSummary
    documentation: DocumentationSummary
    rag: RagSummary
    automation: AutomationSummary
    sessions: RecentSessionSummary
    limitations: tuple[str, ...]
