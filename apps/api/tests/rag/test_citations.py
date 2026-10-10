"""Pure citation tests for the "Q<n>" scheme — no DB, always run. Mirrors
tests/impact/test_evidence.py's rigor for the "E<n>" scheme.
"""

import uuid

from app.rag.citations import (
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
        chunk_id=uuid.uuid4(),
        retrieval_reason="matched",
        retrieval_source="lexical",
        relationship_type=None,
        content="def f(): pass",
    )


def test_extract_cited_ids_finds_markers_in_order() -> None:
    assert extract_cited_ids("See [Q1] and [Q3], also [Q1].") == ["Q1", "Q3", "Q1"]


def test_unknown_q_id_is_sanitized() -> None:
    evidence = EvidenceSet(items=(_evidence("Q1"),))
    assert sanitize_citations("see [Q999]", evidence) == "see [unverified]"


def test_valid_citation_is_untouched() -> None:
    evidence = EvidenceSet(items=(_evidence("Q1"),))
    assert sanitize_citations("see [Q1]", evidence) == "see [Q1]"


def test_lowercase_q_is_flagged_not_silently_ignored_or_equated() -> None:
    evidence = EvidenceSet(items=(_evidence("Q1"),))
    assert extract_cited_ids("[q1]") == ["q1"]
    assert sanitize_citations("see [q1]", evidence) == "see [unverified]"


def test_e_prefix_is_never_confused_with_q_prefix() -> None:
    """A stray "[E1]"-style (Impact Intelligence's own prefix) citation in
    QA output must not be mistaken for a valid Q-id — the two evidence
    namespaces must never cross-validate."""
    evidence = EvidenceSet(items=(_evidence("Q1"),))
    assert extract_cited_ids("[E1]") == []  # not even recognized as a citation attempt
    assert (
        sanitize_citations("see [E1]", evidence) == "see [E1]"
    )  # left untouched, not a Q-citation


def test_validate_citations_splits_valid_and_invalid() -> None:
    evidence = EvidenceSet(items=(_evidence("Q1"), _evidence("Q2")))
    valid, invalid = validate_citations(["Q1", "Q2", "Q999"], evidence)
    assert valid == ["Q1", "Q2"]
    assert invalid == ["Q999"]


def test_sanitization_never_makes_an_unsupported_statement_appear_supported() -> None:
    evidence = EvidenceSet(items=(_evidence("Q1"),))
    unsupported = sanitize_citations("Auth is handled here [Q404].", evidence)
    supported = sanitize_citations("Auth is handled here [Q1].", evidence)
    assert "[unverified]" in unsupported
    assert unsupported != supported


def test_q0_is_not_a_valid_citation_unless_actually_in_the_evidence_set() -> None:
    evidence = EvidenceSet(items=(_evidence("Q1"),))
    assert extract_cited_ids("[Q0]") == ["Q0"]
    assert sanitize_citations("see [Q0]", evidence) == "see [unverified]"


def test_q_negative_number_is_not_recognized_as_a_citation_marker() -> None:
    """ "-" is not a digit, so `Q\\d+` cannot match "Q-1" — this is not a
    citation attempt at all, just literal text, and must pass through
    unmodified rather than crash or be coerced into some other id."""
    evidence = EvidenceSet(items=(_evidence("Q1"),))
    assert extract_cited_ids("[Q-1]") == []
    assert sanitize_citations("see [Q-1]", evidence) == "see [Q-1]"


def test_leading_zero_id_is_a_distinct_string_not_equivalent_to_q1() -> None:
    evidence = EvidenceSet(items=(_evidence("Q1"),))
    assert extract_cited_ids("[Q01]") == ["Q01"]
    # "Q01" != "Q1" as strings — never fuzzily equated with a real evidence id.
    assert sanitize_citations("see [Q01]", evidence) == "see [unverified]"


def test_bracket_with_internal_whitespace_is_not_recognized_as_a_citation() -> None:
    """The pattern requires "[" immediately followed by "Q<digits>"
    immediately followed by "]" — "[ Q1 ]" does not match, and must be left
    as plain, inert text rather than crash or falsely validate."""
    evidence = EvidenceSet(items=(_evidence("Q1"),))
    assert extract_cited_ids("[ Q1 ]") == []
    assert sanitize_citations("see [ Q1 ]", evidence) == "see [ Q1 ]"


def test_duplicate_citations_of_the_same_valid_id_are_all_preserved() -> None:
    evidence = EvidenceSet(items=(_evidence("Q1"),))
    assert sanitize_citations("[Q1] and again [Q1]", evidence) == "[Q1] and again [Q1]"
    valid, invalid = validate_citations(["Q1", "Q1", "Q1"], evidence)
    assert valid == ["Q1", "Q1", "Q1"]
    assert invalid == []


def test_duplicate_citations_of_the_same_invalid_id_are_all_sanitized() -> None:
    evidence = EvidenceSet(items=(_evidence("Q1"),))
    assert (
        sanitize_citations("[Q9] and again [Q9]", evidence) == "[unverified] and again [unverified]"
    )
