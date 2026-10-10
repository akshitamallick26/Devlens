"""Pure data shapes shared across app/testing/ — no SQLAlchemy, no DB, no AI
provider. See docs/architecture (testing phase) for the full pipeline this
backs: target discovery -> existing-test discovery -> context assembly ->
LLM generation -> validation -> sandboxed execution -> persistence.
"""

import uuid
from dataclasses import dataclass

# Why a symbol was selected as a test target — explicit, never left to be
# inferred from context. "explicit_target" always outranks the other two
# when a symbol matches more than one (app/testing/discovery.py).
SELECTION_REASONS = ("explicit_target", "impacted_symbol", "impacted_file")

# Deterministic, evidence-based reasons an existing test was linked to a
# target — never "the filename looked similar" on its own (Task 4's
# explicit prohibition). "graph_relationship" is the strongest signal
# (a real `calls` GraphEdge from the test to the target); the others are
# progressively weaker, all still concrete and checkable.
EXISTING_TEST_RELEVANCE_REASONS = (
    "graph_relationship",
    "imports_target_module",
    "same_module",
    "identifier_overlap",
)

TEST_SYMBOL_KINDS = ("pytest_function", "pytest_method", "unittest_method")


@dataclass(frozen=True)
class ExistingTestRef:
    """A deterministically discovered existing test, with an explainable
    reason it was considered relevant to a specific target. No numeric
    confidence score — that would be a fabricated precision this discovery
    method (string/graph matching, not execution or coverage data) cannot
    actually back up."""

    file_id: uuid.UUID
    file_path: str
    symbol_id: uuid.UUID
    test_identifier: str  # e.g. "test_apply_discount" or "TestPricing.test_apply_discount"
    kind: str  # one of TEST_SYMBOL_KINDS
    relevance_reasons: tuple[str, ...]  # one or more of EXISTING_TEST_RELEVANCE_REASONS


@dataclass(frozen=True)
class RelatedSymbol:
    symbol_id: uuid.UUID
    qualified_name: str
    kind: str
    file_id: uuid.UUID
    file_path: str
    start_line: int
    end_line: int
    relationship_type: str  # GraphEdge.relationship_type: calls | imports | inherits | ...


@dataclass(frozen=True)
class TestTarget:
    """Everything deterministically known about one candidate for test
    generation. `impact_depth` is `None` unless this target was selected
    because it appeared in an impact-analysis result the caller supplied —
    this module never computes impact itself (see discovery.py)."""

    # Not a test class — this name collides with pytest's default discovery
    # pattern (Test*) by coincidence, same as app/models/testing.py::TestExecution.
    __test__ = False

    target_symbol_id: uuid.UUID
    qualified_name: str
    kind: str
    file_id: uuid.UUID
    file_path: str
    start_line: int
    end_line: int
    selection_reason: str  # one of SELECTION_REASONS
    impact_depth: int | None
    callers: tuple[RelatedSymbol, ...]
    callees: tuple[RelatedSymbol, ...]
    existing_tests: tuple[ExistingTestRef, ...]


@dataclass(frozen=True)
class TargetDiscoveryResult:
    repository_id: uuid.UUID
    revision_id: uuid.UUID
    targets: tuple[TestTarget, ...]


@dataclass(frozen=True)
class GeneratedTestOutput:
    """Structured output the test-generation provider must return — see
    app/testing/generation.py's response schema. Validated, never trusted
    as-is: `cited_evidence_ids` is sanitized against the real EvidenceSet
    before persistence, and the source itself is never executed before
    app/testing/validation.py has run."""

    file_path: str
    source: str
    target_symbol_id: uuid.UUID
    rationale: str
    cited_evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    detail: str


@dataclass(frozen=True)
class ValidationResult:
    passed: bool
    issues: tuple[ValidationIssue, ...]


@dataclass(frozen=True)
class ExecutionResult:
    status: str  # TEST_EXECUTION_STATUSES: passed | failed | error | timeout
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int
