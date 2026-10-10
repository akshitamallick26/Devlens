"""Full-pipeline Impact Intelligence tests: ingestion -> parsing -> graph
construction -> impact traversal -> explanation, using a FakeProvider (no
real network call — Task 14/19). Requires live PostgreSQL — same
`db_session` fixture and explicit-skip behavior as every other DB-dependent
suite in this project. Never substitutes SQLite.
"""

import uuid
from pathlib import Path
from typing import TypeVar

import httpx
import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.ai.models import AIProviderUnavailableError, AIResponseValidationError
from app.impact.explanation import ImpactExplanation, generate_impact_explanation
from app.impact.models import ImpactAnalysisResult
from app.impact.service import run_impact_analysis_by_qualified_name
from tests.ai.fakes import FakeProvider
from tests.graph.conftest import ingest_parse_and_build_graph

_T = TypeVar("_T", bound=BaseModel)

_VALID_RESPONSE_KWARGS: dict[str, object] = dict(
    summary="`apply_discount` is called directly by `calculate_total` [E2].",
    why_it_matters="Changing its behavior changes the discounted total callers receive [E2].",
    affected_areas=["commerce.total", "commerce.checkout"],
    review_recommendations=["Review commerce/total.py's calculate_total [E2]."],
    test_recommendations=["Add a regression test for calculate_total's discount handling."],
    evidence=["E1", "E2"],
    limitations="Transitive impact source beyond depth 1 was not fully inspected.",
)


def _impact_result(db_session: Session, repo_path: Path) -> ImpactAnalysisResult:
    ingestion_result, _ = ingest_parse_and_build_graph(db_session, repo_path)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    run_result = run_impact_analysis_by_qualified_name(
        db_session, repository_id, revision_id, "commerce.discount.apply_discount"
    )
    assert run_result.status == "completed"
    assert run_result.result is not None
    return run_result.result


def test_provider_success_produces_explained_status_with_sanitized_citations(
    db_session: Session, impact_fixture_repo: Path
) -> None:
    result = _impact_result(db_session, impact_fixture_repo)
    base_summary = str(_VALID_RESPONSE_KWARGS["summary"])
    fake_response = ImpactExplanation.model_validate(
        {**_VALID_RESPONSE_KWARGS, "summary": base_summary + " Also [E999]."}
    )
    provider = FakeProvider(response=fake_response)

    outcome = generate_impact_explanation(db_session, provider, result)

    assert outcome.analysis_status == "explained"
    assert outcome.explanation is not None
    assert "[unverified]" in outcome.explanation.summary  # E999 sanitized
    assert "E999" not in outcome.explanation.evidence
    assert set(outcome.explanation.evidence) <= set(outcome.evidence_map)
    for evidence_id in outcome.explanation.evidence:
        resolved = outcome.evidence_map[evidence_id]
        assert resolved.file_path  # a real path, not invented by the model


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(AIProviderUnavailableError("simulated network failure"), id="unavailable"),
        pytest.param(
            AIProviderUnavailableError(
                "Anthropic request failed: " + str(httpx.ConnectTimeout("timed out"))
            ),
            id="timeout",
        ),
        pytest.param(
            AIProviderUnavailableError("Anthropic request failed: 429 rate limited"), id="429"
        ),
        pytest.param(
            AIProviderUnavailableError("Anthropic request failed: 500 internal error"), id="500"
        ),
        pytest.param(
            AIResponseValidationError("response was not valid JSON"), id="malformed_response"
        ),
        pytest.param(
            AIResponseValidationError("response did not match schema"),
            id="invalid_structured_output",
        ),
        pytest.param(RuntimeError("an unanticipated provider bug"), id="unanticipated_exception"),
    ],
)
def test_every_failure_mode_falls_back_to_deterministic_only(
    db_session: Session, impact_fixture_repo: Path, error: Exception
) -> None:
    """Task 14: for every failure mode, the deterministic result survives
    unchanged and no fabricated explanation ever appears — including an
    *unanticipated* exception type (RuntimeError), proving the broad
    `except Exception` in generate_impact_explanation (added during this
    hardening pass specifically so a provider bug can never cost the
    deterministic result) actually works end-to-end."""
    result = _impact_result(db_session, impact_fixture_repo)
    provider = FakeProvider(error=error)

    outcome = generate_impact_explanation(db_session, provider, result)

    assert outcome.analysis_status == "deterministic_only"
    assert outcome.explanation is None
    assert outcome.evidence_map == {}
    assert outcome.deterministic_result.direct_impacts == result.direct_impacts
    assert outcome.deterministic_result.transitive_impacts == result.transitive_impacts
    assert outcome.deterministic_result.impacted_files == result.impacted_files


def test_structured_response_that_fails_pydantic_validation_falls_back_cleanly(
    db_session: Session, impact_fixture_repo: Path
) -> None:
    """A provider that raises mid-construction (simulating what
    AnthropicProvider does when `response_model.model_validate(parsed)`
    raises ValidationError) must not propagate that ValidationError past
    the orchestration layer."""
    result = _impact_result(db_session, impact_fixture_repo)

    class _RaisingProvider:
        def generate_structured(
            self, *, system_prompt: str, user_content: str, response_model: type[_T]
        ) -> _T:
            try:
                response_model.model_validate({"summary": "incomplete"})
            except ValidationError as exc:
                raise AIResponseValidationError(str(exc)) from exc
            raise AssertionError("unreachable")

    outcome = generate_impact_explanation(db_session, _RaisingProvider(), result)
    assert outcome.analysis_status == "deterministic_only"
    assert outcome.explanation is None
