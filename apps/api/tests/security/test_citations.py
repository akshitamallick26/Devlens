"""Pure citation tests for the "S<n>" scheme — no DB, always run. Mirrors
the equivalent suites for "Q<n>"/"E<n>"/"T<n>"."""

import uuid

from app.security.citations import (
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
        kind="finding",
        file_path=None,
        symbol_id=None,
        qualified_name=None,
        line_start=None,
        line_end=None,
        content="x",
    )


def test_extract_cited_ids_finds_markers_in_order() -> None:
    assert extract_cited_ids("See [S1] and [S3], also [S1].") == ["S1", "S3", "S1"]


def test_unknown_s_id_is_sanitized() -> None:
    evidence = EvidenceSet(items=(_evidence("S1"),))
    assert sanitize_citations("see [S999]", evidence) == "see [unverified]"


def test_valid_citation_is_untouched() -> None:
    evidence = EvidenceSet(items=(_evidence("S1"),))
    assert sanitize_citations("see [S1]", evidence) == "see [S1]"


def test_lowercase_s_is_flagged_not_silently_ignored_or_equated() -> None:
    evidence = EvidenceSet(items=(_evidence("S1"),))
    assert extract_cited_ids("[s1]") == ["s1"]
    assert sanitize_citations("see [s1]", evidence) == "see [unverified]"


def test_t_prefix_is_never_confused_with_s_prefix() -> None:
    evidence = EvidenceSet(items=(_evidence("S1"),))
    assert extract_cited_ids("[T1]") == []
    assert sanitize_citations("see [T1]", evidence) == "see [T1]"


def test_validate_citations_splits_valid_and_invalid() -> None:
    evidence = EvidenceSet(items=(_evidence("S1"), _evidence("S2")))
    valid, invalid = validate_citations(["S1", "S2", "S999"], evidence)
    assert valid == ["S1", "S2"]
    assert invalid == ["S999"]
