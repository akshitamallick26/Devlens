"""DB-dependent target-discovery tests. Requires live PostgreSQL — same
`db_session` fixture and explicit-skip behavior as every other DB-dependent
suite in this project. Never substitutes SQLite.
"""

import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.testing.discovery import discover_test_targets
from tests.testing.conftest import ingest_parse_and_graph, symbol_id


def test_no_input_produces_no_targets(db_session: Session, testing_fixture_repo: Path) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    result = discover_test_targets(db_session, repository_id, revision_id)
    assert result.targets == ()


def test_single_explicit_target_symbol_id(db_session: Session, testing_fixture_repo: Path) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    target_id = symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")

    result = discover_test_targets(
        db_session, repository_id, revision_id, target_symbol_id=target_id
    )
    assert len(result.targets) == 1
    target = result.targets[0]
    assert target.target_symbol_id == target_id
    assert target.selection_reason == "explicit_target"


def test_multiple_impacted_symbol_ids_produce_multiple_targets(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    apply_discount_id = symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")
    calculate_total_id = symbol_id(db_session, revision_id, "checkout.service.calculate_total")

    result = discover_test_targets(
        db_session,
        repository_id,
        revision_id,
        impacted_symbol_ids={apply_discount_id, calculate_total_id},
    )
    assert {t.target_symbol_id for t in result.targets} == {apply_discount_id, calculate_total_id}
    assert all(t.selection_reason == "impacted_symbol" for t in result.targets)


def test_explicit_target_outranks_impacted_symbol_for_the_same_symbol(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    target_id = symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")

    result = discover_test_targets(
        db_session,
        repository_id,
        revision_id,
        target_symbol_id=target_id,
        impacted_symbol_ids={target_id},
    )
    assert len(result.targets) == 1
    assert result.targets[0].selection_reason == "explicit_target"


def test_ordering_is_deterministic_across_repeated_calls(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    apply_discount_id = symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")
    calculate_total_id = symbol_id(db_session, revision_id, "checkout.service.calculate_total")

    symbol_ids = {apply_discount_id, calculate_total_id}
    first = discover_test_targets(
        db_session, repository_id, revision_id, impacted_symbol_ids=symbol_ids
    )
    second = discover_test_targets(
        db_session, repository_id, revision_id, impacted_symbol_ids=symbol_ids
    )
    assert [t.target_symbol_id for t in first.targets] == [
        t.target_symbol_id for t in second.targets
    ]


def test_callers_and_callees_are_populated(db_session: Session, testing_fixture_repo: Path) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    target_id = symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")

    result = discover_test_targets(
        db_session, repository_id, revision_id, target_symbol_id=target_id
    )
    target = result.targets[0]
    caller_names = {c.qualified_name for c in target.callers}
    assert "checkout.service.calculate_total" in caller_names


def test_existing_tests_are_attached_to_the_target(
    db_session: Session, testing_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    target_id = symbol_id(db_session, revision_id, "checkout.pricing.apply_discount")

    result = discover_test_targets(
        db_session, repository_id, revision_id, target_symbol_id=target_id
    )
    target = result.targets[0]
    identifiers = {et.test_identifier for et in target.existing_tests}
    assert "test_apply_discount" in identifiers
    assert "TestPricingLegacy.test_apply_discount_legacy" in identifiers
    # The unrelated, misleadingly-named test file must not be linked.
    assert "test_something_else" not in identifiers


def test_cyclic_graph_does_not_hang_or_crash(
    db_session: Session, testing_cyclic_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, testing_cyclic_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    is_even_id = symbol_id(db_session, revision_id, "mutual.is_even")

    result = discover_test_targets(
        db_session, repository_id, revision_id, target_symbol_id=is_even_id
    )
    assert len(result.targets) == 1
    target = result.targets[0]
    callee_names = {c.qualified_name for c in target.callees}
    caller_names = {c.qualified_name for c in target.callers}
    assert "mutual.is_odd" in callee_names
    assert "mutual.is_odd" in caller_names


def test_cross_revision_isolation(db_session: Session, testing_fixture_repo: Path) -> None:
    first_result = ingest_parse_and_graph(db_session, testing_fixture_repo, owner="owner-a")
    second_result = ingest_parse_and_graph(db_session, testing_fixture_repo, owner="owner-b")
    first_repo = uuid.UUID(first_result.repository_id)
    first_rev = uuid.UUID(first_result.revision_id)
    second_rev = uuid.UUID(second_result.revision_id)

    target_id_first = symbol_id(db_session, first_rev, "checkout.pricing.apply_discount")
    target_id_second = symbol_id(db_session, second_rev, "checkout.pricing.apply_discount")
    assert target_id_first != target_id_second

    result = discover_test_targets(
        db_session, first_repo, first_rev, target_symbol_id=target_id_first
    )
    assert len(result.targets) == 1
    assert result.targets[0].target_symbol_id != target_id_second
    for existing_test in result.targets[0].existing_tests:
        assert existing_test.symbol_id != target_id_second
