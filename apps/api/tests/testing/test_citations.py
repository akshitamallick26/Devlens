"""Pure citation tests for the "T<n>" scheme — no DB, always run. Mirrors
tests/rag/test_citations.py's rigor for the "Q<n>" scheme.
"""

import uuid

from app.testing.citations import (
    Evidence,
    EvidenceSet,
    extract_cited_ids,
    sanitize_citations,
    validate_citations,
)


def _evidence(evidence_id: str) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        repository_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        file_path="a.py",
        symbol_id=uuid.uuid4(),
        qualified_name="a.f",
        line_start=1,
        line_end=2,
        kind="target_source",
        content="def f(): pass",
    )


def test_extract_cited_ids_finds_markers_in_order() -> None:
    assert extract_cited_ids("See [T1] and [T3], also [T1].") == ["T1", "T3", "T1"]


def test_unknown_t_id_is_sanitized() -> None:
    evidence = EvidenceSet(items=(_evidence("T1"),))
    assert sanitize_citations("see [T999]", evidence) == "see [unverified]"


def test_valid_citation_is_untouched() -> None:
    evidence = EvidenceSet(items=(_evidence("T1"),))
    assert sanitize_citations("see [T1]", evidence) == "see [T1]"


def test_lowercase_t_is_flagged_not_silently_ignored_or_equated() -> None:
    evidence = EvidenceSet(items=(_evidence("T1"),))
    assert extract_cited_ids("[t1]") == ["t1"]
    assert sanitize_citations("see [t1]", evidence) == "see [unverified]"


def test_q_prefix_is_never_confused_with_t_prefix() -> None:
    evidence = EvidenceSet(items=(_evidence("T1"),))
    assert extract_cited_ids("[Q1]") == []
    assert sanitize_citations("see [Q1]", evidence) == "see [Q1]"


def test_validate_citations_splits_valid_and_invalid() -> None:
    evidence = EvidenceSet(items=(_evidence("T1"), _evidence("T2")))
    valid, invalid = validate_citations(["T1", "T2", "T999"], evidence)
    assert valid == ["T1", "T2"]
    assert invalid == ["T999"]


def test_duplicate_citations_of_a_valid_id_are_all_preserved() -> None:
    evidence = EvidenceSet(items=(_evidence("T1"),))
    valid, invalid = validate_citations(["T1", "T1"], evidence)
    assert valid == ["T1", "T1"]
    assert invalid == []
