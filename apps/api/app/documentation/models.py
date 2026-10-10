"""Pure data shapes for app/documentation/ — no SQLAlchemy, no DB, no AI
provider. See docs/architecture/documentation-intelligence.md for the full
product contract.
"""

import uuid
from dataclasses import dataclass

ARTIFACT_TYPES = (
    "repository_overview",
    "architecture_overview",
    "module_documentation",
    "symbol_documentation",
)
GENERATION_METHODS = ("llm_generated", "deterministic_fallback")
EVIDENCE_KINDS = ("module_source", "symbol_source", "caller", "callee", "existing_documentation")


TARGET_RESOLUTION_STATUSES = (
    "resolved",
    "missing_target",
    "unexpected_target",
    "not_found",
    "wrong_repository",
    "wrong_revision",
)


@dataclass(frozen=True)
class TargetResolution:
    """Pure result of deterministic target discovery (Task 14) — the
    service layer, not this dataclass, decides which HTTP status each
    non-"resolved" status maps to. Same "resolve, don't raise" convention
    app/automation/parent_resolution.py::ParentResolution already
    established."""

    status: str  # one of TARGET_RESOLUTION_STATUSES
    target_file_id: uuid.UUID | None
    target_symbol_id: uuid.UUID | None
    detail: str | None


@dataclass(frozen=True)
class EvidenceItem:
    """ "D<n>" — see app/documentation/citations.py. Deliberately its own
    re-implementation of the same shape app/testing/citations.py::Evidence
    already uses — a cosmetic prefix difference, not worth generalizing
    (see that module's own docstring for the established precedent)."""

    evidence_id: str
    repository_id: uuid.UUID
    revision_id: uuid.UUID
    file_path: str | None
    symbol_id: uuid.UUID | None
    qualified_name: str | None
    graph_edge_relationship: str | None  # populated for kind="caller"/"callee"
    line_start: int | None
    line_end: int | None
    kind: str  # one of EVIDENCE_KINDS
    content: str | None  # None when source genuinely isn't available — never fabricated


@dataclass(frozen=True)
class EvidenceSet:
    items: tuple[EvidenceItem, ...]

    def by_id(self) -> dict[str, EvidenceItem]:
        return {e.evidence_id: e for e in self.items}

    def valid_ids(self) -> frozenset[str]:
        return frozenset(e.evidence_id for e in self.items)


@dataclass(frozen=True)
class SymbolFact:
    symbol_id: uuid.UUID
    qualified_name: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    signature: str | None
    relationship_type: str | None = None  # populated when used as a caller/callee entry


@dataclass(frozen=True)
class RepositoryOverviewFacts:
    repository_name: str
    default_branch: str
    revision_commit_sha: str
    file_count: int
    files_by_language: tuple[tuple[str, int], ...]  # sorted deterministically
    top_level_modules: tuple[str, ...]  # sorted
    symbol_count: int
    graph_edge_count: int
    existing_readme_path: str | None


@dataclass(frozen=True)
class ArchitectureOverviewFacts:
    module_count: int
    key_symbols: tuple[SymbolFact, ...]  # highest fan-in/fan-out, bounded
    relationship_counts: tuple[tuple[str, int], ...]  # relationship_type -> count, sorted
    existing_readme_path: str | None


@dataclass(frozen=True)
class ModuleFacts:
    file_path: str
    language: str | None
    imports: tuple[str, ...]
    symbols: tuple[SymbolFact, ...]
    outbound_dependencies: tuple[str, ...]  # file paths this module's symbols call into
    inbound_dependents: tuple[str, ...]  # file paths that call into this module


@dataclass(frozen=True)
class SymbolFacts:
    symbol_id: uuid.UUID
    qualified_name: str
    kind: str
    file_path: str
    start_line: int
    end_line: int
    signature: str | None
    callers: tuple[SymbolFact, ...]
    callees: tuple[SymbolFact, ...]
    inherits_from: tuple[str, ...]  # qualified names, from "inherits" GraphEdge rows


Facts = RepositoryOverviewFacts | ArchitectureOverviewFacts | ModuleFacts | SymbolFacts


@dataclass(frozen=True)
class DocumentationContextPackage:
    repository_id: uuid.UUID
    revision_id: uuid.UUID
    artifact_type: str  # one of ARTIFACT_TYPES
    target_file_id: uuid.UUID | None
    target_symbol_id: uuid.UUID | None
    facts: Facts
    evidence: EvidenceSet
    truncated: bool  # Task 6 — any budget was hit during assembly


@dataclass(frozen=True)
class DocumentationSection:
    heading: str
    content: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class DocumentationArtifactDraft:
    """The internal, validated representation of generated documentation
    — never arbitrary free-form provider text (Task 9)."""

    title: str
    summary: str
    sections: tuple[DocumentationSection, ...]
    evidence_ids: tuple[str, ...]  # every id cited anywhere in summary/sections, deduplicated


@dataclass(frozen=True)
class DocumentationArtifactResult:
    artifact_id: uuid.UUID
    repository_id: uuid.UUID
    revision_id: uuid.UUID
    artifact_type: str
    target_file_id: uuid.UUID | None
    target_symbol_id: uuid.UUID | None
    title: str
    content: str  # rendered markdown
    generation_method: str  # one of GENERATION_METHODS
    provider: str
    model_name: str
    model_version: str
    evidence_ids: tuple[str, ...]
    cited_evidence_ids: tuple[str, ...]
    context_truncated: bool
    analysis_run_id: uuid.UUID
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DocumentationArtifactSummary:
    """One row of a listing — no content, same "summary excludes the heavy
    payload" discipline as app/investigation/models.py::SessionSummary."""

    artifact_id: uuid.UUID
    repository_id: uuid.UUID
    revision_id: uuid.UUID
    artifact_type: str
    target_file_id: uuid.UUID | None
    target_symbol_id: uuid.UUID | None
    title: str
    generation_method: str
    created_at: str
    updated_at: str
