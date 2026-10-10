"""Pure tests for app/impact/explanation.py's logic that doesn't require a
database: the "no provider configured" fallback (returns before any DB
access), and the sanitization helpers applied directly. Full
provider-success/provider-failure flows need real source extraction and
therefore live in tests/impact/test_explanation_db.py (DB-dependent).
"""

import uuid

import pytest
from pydantic import ValidationError

from app.impact.evidence import Evidence, EvidenceSet
from app.impact.explanation import (
    ImpactExplanation,
    _evidence_map,
    _sanitize_explanation,
    generate_impact_explanation,
)
from app.impact.models import ImpactAnalysisResult, SymbolView

_VALID_EXPLANATION_KWARGS = dict(
    summary="s",
    why_it_matters="w",
    affected_areas=[],
    review_recommendations=[],
    test_recommendations=[],
    evidence=[],
    limitations="l",
)


def _target() -> SymbolView:
    return SymbolView(
        id=uuid.uuid4(),
        qualified_name="target",
        kind="function",
        file_id=uuid.uuid4(),
        file_path="target.py",
        parent_symbol_id=None,
    )


def _result() -> ImpactAnalysisResult:
    target = _target()
    return ImpactAnalysisResult(
        revision_id=uuid.uuid4(),
        target_symbol_id=target.id,
        target_symbol=target,
        direct_impacts=(),
        transitive_impacts=(),
        impacted_files=(),
        max_depth=0,
    )


def test_no_provider_configured_returns_deterministic_only_without_touching_db() -> None:
    result = _result()
    # `db=None` would blow up if this code path ever touched the database —
    # it must return before that happens.
    outcome = generate_impact_explanation(db=None, provider=None, result=result)  # type: ignore[arg-type]
    assert outcome.analysis_status == "deterministic_only"
    assert outcome.explanation is None
    assert outcome.evidence_map == {}
    assert outcome.deterministic_result is result


def test_sanitize_explanation_strips_unknown_citations_everywhere() -> None:
    evidence = EvidenceSet(
        items=(
            Evidence(
                evidence_id="E1",
                type="target_source",
                symbol_id=uuid.uuid4(),
                file_path="a.py",
                line_start=1,
                line_end=2,
                relationship_type=None,
                depth=0,
                content="x",
            ),
        )
    )
    raw = ImpactExplanation(
        summary="See [E1] and [E99].",
        why_it_matters="Because [E1].",
        affected_areas=["[E99] area"],
        review_recommendations=["check [E1]"],
        test_recommendations=["test [E99]"],
        evidence=["E1", "E99"],
        limitations="[E99] is unverifiable.",
    )
    sanitized = _sanitize_explanation(raw, evidence)
    assert sanitized.summary == "See [E1] and [unverified]."
    assert sanitized.affected_areas == ["[unverified] area"]
    assert sanitized.evidence == ["E1"]  # E99 dropped, never surfaced as real
    assert sanitized.limitations == "[unverified] is unverifiable."


def test_structured_output_rejects_missing_required_fields() -> None:
    with pytest.raises(ValidationError):
        ImpactExplanation.model_validate({"summary": "s"})


@pytest.mark.parametrize(
    "smuggled_field,value",
    [
        ("risk_score", 0.9),
        ("confidence", 1.0),
        ("probability", 0.87),
        ("impacted_symbols", ["a.b", "a.c"]),
        ("new_relationships", [{"source": "a", "target": "b", "type": "calls"}]),
        ("depth", 3),
    ],
)
def test_structured_output_rejects_every_known_smuggling_attempt(
    smuggled_field: str, value: object
) -> None:
    """The model must not be able to smuggle deterministic graph data (or a
    fabricated numeric score) back through the explanation schema — Task 9.
    Each of these would, if accepted, let a future caller mistake AI output
    for authoritative graph/impact data."""
    with pytest.raises(ValidationError):
        ImpactExplanation.model_validate({**_VALID_EXPLANATION_KWARGS, smuggled_field: value})


def test_structured_output_rejects_wrong_type_for_a_real_field() -> None:
    with pytest.raises(ValidationError):
        ImpactExplanation.model_validate({**_VALID_EXPLANATION_KWARGS, "summary": 12345})


def test_structured_output_rejects_null_for_a_required_field() -> None:
    with pytest.raises(ValidationError):
        ImpactExplanation.model_validate({**_VALID_EXPLANATION_KWARGS, "summary": None})


def test_structured_output_rejects_null_for_a_required_list_field() -> None:
    with pytest.raises(ValidationError):
        ImpactExplanation.model_validate({**_VALID_EXPLANATION_KWARGS, "evidence": None})


def test_structured_output_accepts_valid_payload() -> None:
    explanation = ImpactExplanation.model_validate(_VALID_EXPLANATION_KWARGS)
    assert explanation.summary == "s"


def test_evidence_map_resolves_ids_to_file_locations() -> None:
    evidence = EvidenceSet(
        items=(
            Evidence(
                evidence_id="E1",
                type="target_source",
                symbol_id=uuid.uuid4(),
                file_path="a.py",
                line_start=10,
                line_end=20,
                relationship_type=None,
                depth=0,
                content="x",
            ),
        )
    )
    mapping = _evidence_map(evidence)
    assert mapping["E1"].file_path == "a.py"
    assert mapping["E1"].line_start == 10
    assert mapping["E1"].line_end == 20
