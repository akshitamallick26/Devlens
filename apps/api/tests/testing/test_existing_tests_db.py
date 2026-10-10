"""DB-dependent existing-test discovery tests, calling
app/testing/existing_tests.py directly (Task 17's exact matrix). Requires
live PostgreSQL — same `db_session` fixture and explicit-skip behavior as
every other DB-dependent suite. Never substitutes SQLite.
"""

import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.testing.existing_tests import find_existing_tests_for_targets
from tests.testing.conftest import ingest_parse_and_graph, symbol_id


def test_pytest_style_function_is_discovered_via_graph_relationship(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    target_id = symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")

    result = find_existing_tests_for_targets(db_session, repository_id, revision_id, {target_id})
    refs = result[target_id]
    pytest_ref = next(r for r in refs if r.test_identifier == "test_apply_discount")
    assert pytest_ref.kind == "pytest_function"
    assert "graph_relationship" in pytest_ref.relevance_reasons


def test_unittest_style_method_is_discovered_and_classified(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    target_id = symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")

    result = find_existing_tests_for_targets(db_session, repository_id, revision_id, {target_id})
    refs = result[target_id]
    unittest_ref = next(
        r for r in refs if r.test_identifier == "TestPricingLegacy.test_apply_discount_legacy"
    )
    assert unittest_ref.kind == "unittest_method"
    assert "graph_relationship" in unittest_ref.relevance_reasons


def test_unrelated_misleadingly_named_test_is_not_linked(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    target_id = symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")

    result = find_existing_tests_for_targets(db_session, repository_id, revision_id, {target_id})
    identifiers = {r.test_identifier for r in result[target_id]}
    assert "test_something_else" not in identifiers


def test_same_symbol_name_in_a_different_module_gets_no_strong_evidence(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    """`unrelated.py` also defines `apply_discount`. Nothing actually calls
    or imports that one, so the strong, precise evidence kinds
    (graph_relationship, imports_target_module) must never fire for it —
    even though the weakest kind (identifier_overlap, name-only) may still
    match, since `test_apply_discount` genuinely does share that bare name.
    This is exactly why identifier_overlap is documented as the weakest
    signal: a shared name alone is not proof of a real relationship."""
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    unrelated_target_id = symbol_id(db_session, revision_id, "unrelated.apply_discount")

    result = find_existing_tests_for_targets(
        db_session, repository_id, revision_id, {unrelated_target_id}
    )
    for ref in result[unrelated_target_id]:
        assert "graph_relationship" not in ref.relevance_reasons
        assert "imports_target_module" not in ref.relevance_reasons


def test_target_with_no_relevant_existing_test_returns_empty_tuple_not_absent(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    target_id = symbol_id(db_session, revision_id, "checkout.service.calculate_total")

    result = find_existing_tests_for_targets(db_session, repository_id, revision_id, {target_id})
    assert target_id in result
    assert result[target_id] == ()


def test_no_target_symbol_ids_returns_empty_dict(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    assert find_existing_tests_for_targets(db_session, repository_id, revision_id, set()) == {}
