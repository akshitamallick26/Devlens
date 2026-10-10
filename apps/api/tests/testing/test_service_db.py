"""End-to-end Testing/Generated Tests orchestration tests. Requires live
PostgreSQL — same `db_session` fixture and explicit-skip behavior as every
other DB-dependent suite. Never substitutes SQLite. Uses FakeProvider — no
real LLM call.
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GeneratedTest, TestExecution
from app.testing.generation import GeneratedTestResponse
from app.testing.service import RevisionNotFoundError, generate_tests_for_target
from tests.ai.fakes import FakeProvider
from tests.testing.conftest import ingest_parse_and_graph, symbol_id


def _valid_response(target_name: str = "test_apply_discount.py") -> GeneratedTestResponse:
    return GeneratedTestResponse(
        file_path=target_name,
        source=(
            "from checkout.pricing import apply_discount\n\n\n"
            "def test_apply_discount():\n"
            "    assert apply_discount(100) == 90\n"
        ),
        rationale="Exercises the target directly [T1].",
        cited_evidence_ids=["T1"],
    )


def test_no_targets_produces_no_targets_status(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    result = generate_tests_for_target(
        db_session,
        FakeProvider(response=_valid_response()),
        uuid.UUID(ingestion_result.repository_id),
        uuid.UUID(ingestion_result.revision_id),
    )
    assert result.analysis_status == "no_targets"
    assert result.outcomes == ()


def test_no_provider_configured_reports_no_provider_per_target(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    target_id = symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")

    result = generate_tests_for_target(
        db_session, None, repository_id, revision_id, target_symbol_id=target_id
    )
    assert result.analysis_status == "no_provider"
    assert len(result.outcomes) == 1
    assert result.outcomes[0].status == "no_provider"
    assert result.outcomes[0].generated_test_id is None


def test_provider_error_produces_generation_failed_with_no_fake_artifact(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    target_id = symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")

    result = generate_tests_for_target(
        db_session,
        FakeProvider(error=RuntimeError("provider unavailable")),
        repository_id,
        revision_id,
        target_symbol_id=target_id,
    )
    assert result.analysis_status == "completed"
    outcome = result.outcomes[0]
    assert outcome.status == "generation_failed"
    assert outcome.generated_test_id is None
    assert outcome.error is not None

    rows = (
        db_session.execute(select(GeneratedTest).where(GeneratedTest.revision_id == revision_id))
        .scalars()
        .all()
    )
    assert rows == []  # no fake GeneratedTest persisted on a generation failure


def test_malformed_path_fails_validation_and_persists_the_artifact(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    target_id = symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")

    bad_response = GeneratedTestResponse(
        file_path="../../etc/evil.py",
        source="def test_x(): assert True\n",
        rationale="r",
        cited_evidence_ids=[],
    )
    result = generate_tests_for_target(
        db_session,
        FakeProvider(response=bad_response),
        repository_id,
        revision_id,
        target_symbol_id=target_id,
    )
    outcome = result.outcomes[0]
    assert outcome.status == "validation_failed"
    assert outcome.generated_test_id is not None
    assert any(issue.code == "unsafe_path" for issue in outcome.validation_issues)

    generated_test = db_session.get(GeneratedTest, outcome.generated_test_id)
    assert generated_test is not None
    assert generated_test.status == "validation_failed"
    assert generated_test.validation_status == "failed"
    assert generated_test.validation_errors


def test_full_pipeline_with_execution_reports_passed(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    target_id = symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")

    result = generate_tests_for_target(
        db_session,
        FakeProvider(response=_valid_response()),
        repository_id,
        revision_id,
        target_symbol_id=target_id,
        execute=True,
    )
    outcome = result.outcomes[0]
    assert outcome.status == "passed"
    assert outcome.execution is not None
    assert outcome.execution.status == "passed"
    assert outcome.execution_id is not None

    generated_test = db_session.get(GeneratedTest, outcome.generated_test_id)
    assert generated_test is not None
    assert generated_test.status == "passed"
    assert (generated_test.provider, generated_test.model_name) == ("fake", "fake-model")
    assert generated_test.model_version is None  # not reported by the provider

    execution = db_session.get(TestExecution, outcome.execution_id)
    assert execution is not None
    assert execution.status == "passed"
    assert execution.generated_test_id == outcome.generated_test_id
    assert execution.repository_id == repository_id
    assert execution.revision_id == revision_id


def test_ready_without_execute_does_not_create_a_test_execution_row(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    target_id = symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")

    result = generate_tests_for_target(
        db_session,
        FakeProvider(response=_valid_response()),
        repository_id,
        revision_id,
        target_symbol_id=target_id,
        execute=False,
    )
    outcome = result.outcomes[0]
    assert outcome.status == "ready"
    assert outcome.execution is None
    assert outcome.execution_id is None

    count = (
        db_session.execute(
            select(TestExecution).where(
                TestExecution.generated_test_id == outcome.generated_test_id
            )
        )
        .scalars()
        .all()
    )
    assert count == []


def test_revision_not_found_is_explicit(db_session: Session, testing_fixture_repo: Path) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    with pytest.raises(RevisionNotFoundError):
        generate_tests_for_target(db_session, None, repository_id, uuid.uuid4())


def test_cross_revision_isolation(db_session: Session, testing_fixture_repo: Path) -> None:
    first_result = ingest_parse_and_graph(db_session, testing_fixture_repo, owner="owner-a")
    second_result = ingest_parse_and_graph(db_session, testing_fixture_repo, owner="owner-b")
    first_repo = uuid.UUID(first_result.repository_id)
    first_rev = uuid.UUID(first_result.revision_id)
    second_rev = uuid.UUID(second_result.revision_id)
    target_id = symbol_id(db_session, first_rev, "checkout.pricing.apply_discount")

    result = generate_tests_for_target(
        db_session,
        FakeProvider(response=_valid_response()),
        first_repo,
        first_rev,
        target_symbol_id=target_id,
    )
    generated_test_id = result.outcomes[0].generated_test_id
    generated_test = db_session.get(GeneratedTest, generated_test_id)
    assert generated_test is not None
    assert generated_test.revision_id == first_rev
    assert generated_test.revision_id != second_rev
