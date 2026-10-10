"""Pure tests for app/documentation/citations.py — no DB, no AI provider."""

import uuid

from app.documentation.citations import extract_cited_ids, sanitize_citations, validate_citations
from app.documentation.models import EvidenceItem, EvidenceSet


def _evidence(evidence_id: str) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        repository_id=uuid.uuid4(),
        revision_id=uuid.uuid4(),
        file_path="app/foo.py",
        symbol_id=None,
        qualified_name=None,
        graph_edge_relationship=None,
        line_start=1,
        line_end=2,
        kind="symbol_source",
        content="def foo(): pass",
    )


def test_extract_cited_ids_finds_bracketed_markers() -> None:
    text = "This claim is backed by [D1] and also [D3]."
    assert extract_cited_ids(text) == ["D1", "D3"]


def test_extract_cited_ids_is_case_insensitive_in_matching() -> None:
    assert extract_cited_ids("see [d1]") == ["d1"]


def test_validate_citations_splits_valid_and_invalid() -> None:
    evidence = EvidenceSet(items=(_evidence("D1"), _evidence("D2")))
    valid, invalid = validate_citations(["D1", "D2", "D9"], evidence)
    assert valid == ["D1", "D2"]
    assert invalid == ["D9"]


def test_validate_citations_rejects_lowercase_as_unknown() -> None:
    evidence = EvidenceSet(items=(_evidence("D1"),))
    valid, invalid = validate_citations(["d1"], evidence)
    assert valid == []
    assert invalid == ["d1"]


def test_sanitize_citations_replaces_unverified_markers() -> None:
    evidence = EvidenceSet(items=(_evidence("D1"),))
    text = "Backed by [D1] and also [D99], which does not exist."
    sanitized = sanitize_citations(text, evidence)
    assert "[D1]" in sanitized
    assert "[D99]" not in sanitized
    assert "[unverified]" in sanitized


def test_sanitize_citations_leaves_plain_text_untouched() -> None:
    evidence = EvidenceSet(items=())
    text = "No citations here at all."
    assert sanitize_citations(text, evidence) == text


def test_empty_evidence_set_rejects_every_citation() -> None:
    evidence = EvidenceSet(items=())
    valid, invalid = validate_citations(["D1"], evidence)
    assert valid == []
    assert invalid == ["D1"]
