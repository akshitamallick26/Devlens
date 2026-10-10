"""Pure security-context assembly tests — no DB, always run."""

import uuid

from app.security.context import (
    MAX_EVIDENCE_ITEMS,
    MAX_TOTAL_CONTENT_CHARS,
    assemble_security_context,
)
from app.security.models import FindingView, ObservationView

REPO_ID = uuid.uuid4()
REVISION_ID = uuid.uuid4()


def _finding() -> FindingView:
    return FindingView(
        id=uuid.uuid4(),
        analyzer="osv-scanner",
        rule_id="GHSA-abcd",
        severity="high",
        title="Vulnerable dependency",
        message="details",
        status="open",
        cvss_score=None,
        cvss_vector=None,
        scanner_confidence=None,
        references=("https://example.com",),
    )


def _observation(
    symbol_id: uuid.UUID | None = None, affected_package: str | None = "jinja2"
) -> ObservationView:
    return ObservationView(
        id=uuid.uuid4(),
        finding_id=uuid.uuid4(),
        file_path="requirements.txt",
        start_line=None,
        end_line=None,
        affected_package=affected_package,
        installed_version="2.11.2",
        affected_range=None,
        fixed_version="2.11.3",
        resolution_status="unresolved",
        symbol_id=symbol_id,
    )


def test_finding_evidence_comes_first() -> None:
    package = assemble_security_context(REPO_ID, REVISION_ID, _finding(), (), {}, None, {})
    assert package.evidence.items[0].kind == "finding"
    assert package.evidence.items[0].evidence_id == "S1"


def test_priority_order_is_finding_then_observation_then_dependency_metadata() -> None:
    observation = _observation()
    package = assemble_security_context(
        REPO_ID, REVISION_ID, _finding(), (observation,), {}, None, {}
    )
    kinds = [item.kind for item in package.evidence.items]
    assert kinds == ["finding", "observation", "dependency_metadata"]


def test_affected_source_evidence_is_included_when_available() -> None:
    observation = _observation(affected_package=None)
    package = assemble_security_context(
        REPO_ID,
        REVISION_ID,
        _finding(),
        (observation,),
        {observation.id: "def vulnerable(): ..."},
        None,
        {},
    )
    kinds = [item.kind for item in package.evidence.items]
    assert "affected_source" in kinds


def test_missing_source_degrades_to_metadata_never_fabricated() -> None:
    observation = _observation(affected_package=None)
    package = assemble_security_context(
        REPO_ID, REVISION_ID, _finding(), (observation,), {}, None, {}
    )
    kinds = [item.kind for item in package.evidence.items]
    assert "affected_source" not in kinds  # no content supplied -> no fabricated evidence item


def test_context_is_bounded_by_max_evidence_items() -> None:
    observations = tuple(_observation() for _ in range(MAX_EVIDENCE_ITEMS + 5))
    package = assemble_security_context(
        REPO_ID, REVISION_ID, _finding(), observations, {}, None, {}
    )
    assert len(package.evidence.items) <= MAX_EVIDENCE_ITEMS


def test_oversized_content_degrades_to_metadata_not_omitted() -> None:
    observation = _observation(affected_package=None)
    huge = "x" * (MAX_TOTAL_CONTENT_CHARS + 1)
    package = assemble_security_context(
        REPO_ID, REVISION_ID, _finding(), (observation,), {observation.id: huge}, None, {}
    )
    source_items = [i for i in package.evidence.items if i.kind == "affected_source"]
    assert len(source_items) == 1
    assert source_items[0].content is None


def test_evidence_ids_are_sequential_and_stable() -> None:
    package = assemble_security_context(
        REPO_ID, REVISION_ID, _finding(), (_observation(),), {}, None, {}
    )
    assert [item.evidence_id for item in package.evidence.items] == ["S1", "S2", "S3"]


def test_deterministic_repeated_assembly_is_identical() -> None:
    observation = _observation()
    first = assemble_security_context(
        REPO_ID, REVISION_ID, _finding(), (observation,), {}, None, {}
    )
    second = assemble_security_context(
        REPO_ID, REVISION_ID, _finding(), (observation,), {}, None, {}
    )
    assert first == second
