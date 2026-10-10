"""Pure tests for citation extraction/validation/sanitization — no DB, no
AI provider, always run.
"""

import uuid

from app.impact.evidence import (
    Evidence,
    EvidenceSet,
    extract_cited_ids,
    sanitize_citations,
    validate_citations,
)


def _evidence(evidence_id: str) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        type="target_source",
        symbol_id=uuid.uuid4(),
        file_path="a.py",
        line_start=1,
        line_end=2,
        relationship_type=None,
        depth=0,
        content="def a(): pass",
    )


def test_extract_cited_ids_finds_all_markers_in_order() -> None:
    text = "See [E1] and then [E3], also [E1] again."
    assert extract_cited_ids(text) == ["E1", "E3", "E1"]


def test_extract_cited_ids_returns_empty_for_no_markers() -> None:
    assert extract_cited_ids("no citations here") == []


def test_validate_citations_splits_valid_and_invalid() -> None:
    evidence = EvidenceSet(items=(_evidence("E1"), _evidence("E2")))
    valid, invalid = validate_citations(["E1", "E2", "E999"], evidence)
    assert valid == ["E1", "E2"]
    assert invalid == ["E999"]


def test_sanitize_citations_replaces_only_unknown_ids() -> None:
    evidence = EvidenceSet(items=(_evidence("E1"),))
    text = "The caller [E1] depends on this, per [E47]."
    sanitized = sanitize_citations(text, evidence)
    assert sanitized == "The caller [E1] depends on this, per [unverified]."


def test_sanitize_citations_is_a_no_op_when_all_valid() -> None:
    evidence = EvidenceSet(items=(_evidence("E1"), _evidence("E2")))
    text = "[E1] and [E2] are both real."
    assert sanitize_citations(text, evidence) == text


# ---- Task 7: citation edge cases (fail closed, never silently remapped) ----


def test_unknown_numeric_id_e999_is_sanitized() -> None:
    evidence = EvidenceSet(items=(_evidence("E1"),))
    assert sanitize_citations("see [E999]", evidence) == "see [unverified]"


def test_e0_is_never_valid_since_real_ids_start_at_one() -> None:
    evidence = EvidenceSet(items=(_evidence("E1"),))
    assert sanitize_citations("see [E0]", evidence) == "see [unverified]"


def test_negative_looking_e_minus_1_does_not_match_the_citation_pattern() -> None:
    """Not citation-shaped at all (a non-digit character after "E") — left
    as literal text, never mistaken for a real reference either way."""
    evidence = EvidenceSet(items=(_evidence("E1"),))
    text = "see [E-1]"
    assert sanitize_citations(text, evidence) == text
    assert extract_cited_ids(text) == []


def test_lowercase_e1_is_recognized_as_an_attempt_but_never_equated_to_real_e1() -> None:
    """Case-insensitive recognition (so it gets flagged, not silently
    ignored) but never case-normalized to match a real id (Task 7: unknown
    citation -> explicit removal, never silent remapping)."""
    evidence = EvidenceSet(items=(_evidence("E1"),))
    assert extract_cited_ids("[e1]") == ["e1"]
    assert sanitize_citations("see [e1]", evidence) == "see [unverified]"
    valid, invalid = validate_citations(["e1"], evidence)
    assert valid == []
    assert invalid == ["e1"]


def test_internal_whitespace_in_brackets_does_not_match() -> None:
    evidence = EvidenceSet(items=(_evidence("E1"),))
    text = "see [ E1 ]"
    assert extract_cited_ids(text) == []
    assert sanitize_citations(text, evidence) == text


def test_leading_zero_e01_is_never_equal_to_e1() -> None:
    """Real IDs are never zero-padded (context.py generates "E1", "E2",
    ... — never "E01") — exact string comparison correctly treats "E01" as
    a different, invalid id, never silently matched to "E1"."""
    evidence = EvidenceSet(items=(_evidence("E1"),))
    assert sanitize_citations("see [E01]", evidence) == "see [unverified]"


def test_adjacent_citations_with_no_separator_are_both_detected() -> None:
    evidence = EvidenceSet(items=(_evidence("E1"),))
    text = "[E1][E999]"
    assert extract_cited_ids(text) == ["E1", "E999"]
    assert sanitize_citations(text, evidence) == "[E1][unverified]"


def test_citations_are_validated_in_every_free_text_field_independently() -> None:
    """Each field is sanitized on its own — an invalid citation in one
    field never contaminates another, and a valid one elsewhere doesn't
    make an invalid one elsewhere valid."""
    evidence = EvidenceSet(items=(_evidence("E1"),))
    fields = {
        "summary": "claim [E1]",
        "why_it_matters": "claim [E999]",
        "affected_areas_0": "[E1] area",
        "review_recommendations_0": "[E999] review",
        "test_recommendations_0": "[E1] test",
        "limitations": "unsure about [E999]",
    }
    sanitized = {k: sanitize_citations(v, evidence) for k, v in fields.items()}
    assert sanitized["summary"] == "claim [E1]"
    assert sanitized["why_it_matters"] == "claim [unverified]"
    assert sanitized["affected_areas_0"] == "[E1] area"
    assert sanitized["review_recommendations_0"] == "[unverified] review"
    assert sanitized["test_recommendations_0"] == "[E1] test"
    assert sanitized["limitations"] == "unsure about [unverified]"


def test_sanitization_never_makes_an_unsupported_statement_appear_supported() -> None:
    """The core property under test: after sanitization, a claim that cited
    nothing real must visibly show it has no real citation — it must never
    end up looking identical to a claim backed by a real [E1]."""
    evidence = EvidenceSet(items=(_evidence("E1"),))
    unsupported_claim = sanitize_citations("This will break production [E404].", evidence)
    supported_claim = sanitize_citations("The caller consumes this directly [E1].", evidence)
    assert "[unverified]" in unsupported_claim
    assert "[E1]" not in unsupported_claim
    assert "[E1]" in supported_claim
    assert unsupported_claim != supported_claim
