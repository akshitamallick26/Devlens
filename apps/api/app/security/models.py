"""Pure data shapes shared across app/security/ — no SQLAlchemy, no DB, no
subprocess, no AI provider.
"""

import uuid
from dataclasses import dataclass

# Mirrors app/models/security.py::SEVERITIES exactly — kept as a separate
# constant (not imported from the model) so this package never needs to
# import SQLAlchemy models to describe scanner output shapes.
SEVERITIES = ("critical", "high", "medium", "low", "info")

SCANNER_STATUSES = ("completed", "unavailable", "timeout", "malformed_output", "error")
RESOLUTION_STATUSES = ("unresolved", "exact_symbol", "exact_file", "ambiguous")


@dataclass(frozen=True)
class NormalizedFinding:
    """Advisory/rule-level identity — never occurrence-specific. Every
    field left `None` that the scanner did not actually supply; nothing
    here is computed, inferred, or defaulted to a non-null placeholder."""

    analyzer: str  # "osv-scanner" | "semgrep"
    rule_id: str  # CVE/GHSA id, or Semgrep rule id
    fingerprint: str  # see app/security/identity.py for the exact function
    severity: str | None  # one of SEVERITIES, or None if unsupplied
    title: str
    message: str
    cvss_score: float | None = None
    cvss_vector: str | None = None
    scanner_confidence: str | None = None
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class NormalizedObservation:
    """One occurrence of a NormalizedFinding's fingerprint — a specific
    file/line or package/version. `raw_finding` is the scanner's own output
    fragment for this occurrence, preserved verbatim for audit, never
    reinterpreted."""

    fingerprint: str  # links back to the NormalizedFinding with the same fingerprint
    file_path: str | None
    start_line: int | None
    end_line: int | None
    affected_package: str | None
    affected_ecosystem: str | None  # e.g. "PyPI" — needed for graph resolution, never guessed
    installed_version: str | None
    affected_range: str | None
    fixed_version: str | None
    raw_finding: dict[str, object]


@dataclass(frozen=True)
class ScannerRunResult:
    """The result of attempting to run one scanner. `status="completed"`
    is the only status under which `findings`/`observations` should be
    trusted as a real (possibly empty) result — every other status means
    the scanner did not produce a trustworthy result, and `findings`/
    `observations` are always empty in that case (never partially
    fabricated)."""

    scanner: str  # "osv-scanner" | "semgrep"
    status: str  # one of SCANNER_STATUSES
    findings: tuple[NormalizedFinding, ...] = ()
    observations: tuple[NormalizedObservation, ...] = ()
    detail: str | None = None  # human-readable reason for a non-"completed" status
    scanner_version: str | None = None


@dataclass(frozen=True)
class FindingView:
    """A pure, DB-agnostic view of a persisted SecurityFinding — used for
    context assembly and API serialization without leaking the ORM object."""

    id: uuid.UUID
    analyzer: str
    rule_id: str
    severity: str | None
    title: str
    message: str
    status: str
    cvss_score: float | None
    cvss_vector: str | None
    scanner_confidence: str | None
    references: tuple[str, ...]


@dataclass(frozen=True)
class ObservationView:
    """A pure, DB-agnostic view of a persisted SecurityFindingObservation."""

    id: uuid.UUID
    finding_id: uuid.UUID
    file_path: str | None
    start_line: int | None
    end_line: int | None
    affected_package: str | None
    installed_version: str | None
    affected_range: str | None
    fixed_version: str | None
    resolution_status: str
    symbol_id: uuid.UUID | None


@dataclass(frozen=True)
class GraphResolution:
    """Task 8's explicit record of whether/how an observation was mapped
    onto the code graph. `symbol_id`/`file_id` are only ever set together
    with `status in ("exact_symbol", "exact_file")` — never a guess."""

    status: str  # one of RESOLUTION_STATUSES
    file_id: uuid.UUID | None = None
    symbol_id: uuid.UUID | None = None


@dataclass(frozen=True)
class BlastRadiusSymbol:
    symbol_id: uuid.UUID
    qualified_name: str
    kind: str
    file_id: uuid.UUID
    file_path: str
    depth: int
    relationship_types: tuple[str, ...]


@dataclass(frozen=True)
class BlastRadiusFile:
    file_id: uuid.UUID
    path: str
    symbol_count: int
    minimum_depth: int


@dataclass(frozen=True)
class EvidencePath:
    """One deterministic shortest path from the vulnerable seed symbol to
    a dependent symbol — `symbol_ids[0]` is always the seed,
    `symbol_ids[-1]` is always the dependent. `relationship_types[i]` is
    the edge between `symbol_ids[i]` and `symbol_ids[i+1]`."""

    symbol_ids: tuple[uuid.UUID, ...]
    relationship_types: tuple[str, ...]


@dataclass(frozen=True)
class BlastRadiusResult:
    seed_symbol_id: uuid.UUID
    direct_dependents: tuple[BlastRadiusSymbol, ...]
    transitive_dependents: tuple[BlastRadiusSymbol, ...]
    impacted_files: tuple[BlastRadiusFile, ...]
    max_depth_reached: int
    # True if the traversal stopped because it hit a bound (depth, symbol
    # count, or file count) rather than because the reachable graph was
    # fully exhausted — an honest signal that this blast radius is a
    # bounded lower bound, not necessarily the complete picture.
    truncated: bool
    evidence_paths: tuple[EvidencePath, ...]
