"""Testing Intelligence v1.1: existing-test discovery hardening. Live
PostgreSQL (same `db_session` fixture and explicit skip as every DB suite),
using the real ingestion -> parsing -> graph pipeline on
tests/testing/discovery_fixture.py, plus pure tests of the signature rules.

Contract: a test relates to a target only through qualifying evidence (a
`calls` edge test -> target, or the test's file importing the target's
module); file-name/directory resemblance and name substrings only annotate
an already-qualified test. Test identity follows the runners' own
collection rules. Everything is repository- and revision-scoped."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.testing import existing_tests
from app.testing.existing_tests import (
    _TESTCASE_BASE,
    _signature_bases,
    find_existing_tests_for_targets,
)
from app.testing.models import ExistingTestRef
from tests.ingestion.conftest import run_git
from tests.testing.conftest import ingest_parse_and_graph, symbol_id
from tests.testing.discovery_fixture import write_discovery_repo


@pytest.fixture
def discovery_repo(tmp_path: Path) -> Path:
    return write_discovery_repo(tmp_path / "discovery-repo")


def _ids(db: Session, repo: Path, owner: str = "discovery") -> tuple[uuid.UUID, uuid.UUID]:
    result = ingest_parse_and_graph(db, repo, owner=owner)
    return uuid.UUID(result.repository_id), uuid.UUID(result.revision_id)


def _refs(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, qualified_name: str
) -> dict[tuple[str, str], ExistingTestRef]:
    target = symbol_id(db, revision_id, qualified_name)
    refs = find_existing_tests_for_targets(db, repository_id, revision_id, {target})[target]
    return {(r.file_path, r.test_identifier): r for r in refs}


@pytest.fixture
def unbounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """See every qualifying candidate (the default bound is tested separately)."""
    monkeypatch.setattr(existing_tests, "MAX_EXISTING_TESTS_PER_TARGET", 50)


# ---- Identity: which symbols are tests ----


def test_module_level_pytest_functions_are_discovered_with_graph_evidence(
    db_session: Session, discovery_repo: Path, unbounded: None
) -> None:
    repository_id, revision_id = _ids(db_session, discovery_repo)
    refs = _refs(db_session, repository_id, revision_id, "payments.service.charge")
    ref = refs[("tests/test_payments.py", "test_charge")]
    assert ref.kind == "pytest_function"
    assert ref.relevance_reasons[0] == "graph_relationship"


def test_pytest_class_methods_count_but_non_test_classes_and_nested_functions_do_not(
    db_session: Session, discovery_repo: Path, unbounded: None
) -> None:
    repository_id, revision_id = _ids(db_session, discovery_repo)
    refs = _refs(db_session, repository_id, revision_id, "payments.service.charge")
    assert refs[("tests/test_payments.py", "TestCharge.test_method")].kind == "pytest_method"
    identifiers = {identifier for _, identifier in refs}
    # Helper is not a Test* class; test_inner is nested inside a function.
    # Both genuinely call charge(), yet pytest would collect neither.
    assert "Helper.test_not_collected" not in identifiers
    assert "test_inner" not in identifiers and "test_outer.test_inner" not in identifiers
    # test_outer does not call charge itself: only its file's import qualifies it.
    assert refs[("tests/test_payments.py", "test_outer")].relevance_reasons == (
        "imports_target_module",
    )


def test_unittest_class_via_from_import_with_non_test_class_name(
    db_session: Session, discovery_repo: Path, unbounded: None
) -> None:
    """`from unittest import TestCase` yields only an import edge to
    unittest.TestCase (no inherits edge), so classification must come from
    the persisted class signature."""
    repository_id, revision_id = _ids(db_session, discovery_repo)
    refs = _refs(db_session, repository_id, revision_id, "payments.service.charge")
    ref = refs[("tests/test_unit.py", "ChargeCase.test_positive")]
    assert ref.kind == "unittest_method"
    assert "graph_relationship" in ref.relevance_reasons


def test_duplicate_test_names_in_different_files_stay_distinct(
    db_session: Session, discovery_repo: Path, unbounded: None
) -> None:
    repository_id, revision_id = _ids(db_session, discovery_repo)
    refs = _refs(db_session, repository_id, revision_id, "payments.service.charge")
    nested = refs[("tests/payments/test_service.py", "test_charge")]
    top = refs[("tests/test_payments.py", "test_charge")]
    assert nested.symbol_id != top.symbol_id and nested.file_id != top.file_id
    # The nested one never calls charge: no graph evidence is claimed for it.
    assert "graph_relationship" not in nested.relevance_reasons


# ---- Evidence ----


def test_importing_the_target_module_symbol_is_import_evidence_only(
    db_session: Session, discovery_repo: Path, unbounded: None
) -> None:
    """`import payments.service` targets the *module* symbol. The attribute
    call payments.service.validate(1) is not resolved by graph construction,
    so discovery reports import evidence and claims nothing stronger."""
    repository_id, revision_id = _ids(db_session, discovery_repo)
    refs = _refs(db_session, repository_id, revision_id, "payments.service.validate")
    ref = refs[("tests/payments/test_service.py", "test_charge")]
    assert "imports_target_module" in ref.relevance_reasons
    assert "graph_relationship" not in ref.relevance_reasons


def test_a_test_of_a_caller_is_linked_by_import_never_by_graph(
    db_session: Session, discovery_repo: Path, unbounded: None
) -> None:
    repository_id, revision_id = _ids(db_session, discovery_repo)
    charge = _refs(db_session, repository_id, revision_id, "payments.service.charge")
    assert charge[("tests/test_refund.py", "test_refund")].relevance_reasons == (
        "imports_target_module",
    )
    refund = _refs(db_session, repository_id, revision_id, "payments.service.refund")
    assert refund[("tests/test_refund.py", "test_refund")].relevance_reasons[0] == (
        "graph_relationship"
    )


# ---- Negative / adversarial ----


def test_name_and_path_resemblance_alone_never_link_a_test(
    db_session: Session, discovery_repo: Path, unbounded: None
) -> None:
    repository_id, revision_id = _ids(db_session, discovery_repo)
    refs = _refs(db_session, repository_id, revision_id, "payments.service.charge")
    files = {path for path, _ in refs}
    # tests/test_service.py is named after the target's module and contains
    # test_charge_unrelated; tests/test_strings.py mentions charge only in a
    # comment and a string literal. Neither imports nor calls it.
    assert "tests/test_service.py" not in files
    assert "tests/test_strings.py" not in files


def test_same_named_symbol_in_another_module_gets_nothing(
    db_session: Session, discovery_repo: Path, unbounded: None
) -> None:
    repository_id, revision_id = _ids(db_session, discovery_repo)
    assert _refs(db_session, repository_id, revision_id, "other.service.charge") == {}
    assert _refs(db_session, repository_id, revision_id, "payments.similar.charge_card") == {}


def test_import_evidence_does_not_leak_between_targets_queried_together(
    db_session: Session, discovery_repo: Path, unbounded: None
) -> None:
    repository_id, revision_id = _ids(db_session, discovery_repo)
    real = symbol_id(db_session, revision_id, "payments.service.charge")
    other = symbol_id(db_session, revision_id, "other.service.charge")
    result = find_existing_tests_for_targets(db_session, repository_id, revision_id, {real, other})
    assert result[real]
    assert result[other] == ()


def test_results_are_bounded_and_deterministic(db_session: Session, discovery_repo: Path) -> None:
    repository_id, revision_id = _ids(db_session, discovery_repo)
    target = symbol_id(db_session, revision_id, "payments.service.charge")
    first = find_existing_tests_for_targets(db_session, repository_id, revision_id, {target})
    second = find_existing_tests_for_targets(db_session, repository_id, revision_id, {target})
    assert first == second
    refs = first[target]
    assert len(refs) == existing_tests.MAX_EXISTING_TESTS_PER_TARGET
    strengths = [
        min(existing_tests._REASON_STRENGTH[r] for r in ref.relevance_reasons) for ref in refs
    ]
    assert strengths == sorted(strengths)  # strongest evidence first


# ---- Isolation ----


def test_discovery_is_revision_and_repository_scoped(
    db_session: Session, discovery_repo: Path, unbounded: None
) -> None:
    repository_id, rev1 = _ids(db_session, discovery_repo)
    rev1_charge = symbol_id(db_session, rev1, "payments.service.charge")

    # Revision 2: same file and test names, but test_charge no longer
    # touches the target at all.
    (discovery_repo / "tests" / "test_payments.py").write_text(
        "def test_charge():\n    assert True\n"
    )
    run_git(discovery_repo, "add", "-A")
    run_git(discovery_repo, "commit", "-q", "-m", "second")
    _, rev2 = _ids(db_session, discovery_repo)
    rev2_charge = symbol_id(db_session, rev2, "payments.service.charge")

    rev2_refs = _refs(db_session, repository_id, rev2, "payments.service.charge")
    assert ("tests/test_payments.py", "test_charge") not in rev2_refs
    rev1_refs = _refs(db_session, repository_id, rev1, "payments.service.charge")
    assert (
        "graph_relationship"
        in rev1_refs[("tests/test_payments.py", "test_charge")].relevance_reasons
    )
    rev2_symbol_ids = {r.symbol_id for r in rev2_refs.values()}
    assert rev2_symbol_ids.isdisjoint({r.symbol_id for r in rev1_refs.values()})

    # A target id from another revision, or another repository, resolves nothing.
    assert find_existing_tests_for_targets(db_session, repository_id, rev2, {rev1_charge}) == {
        rev1_charge: ()
    }
    other_repo, other_rev = _ids(db_session, discovery_repo, owner="discovery-other")
    assert find_existing_tests_for_targets(db_session, other_repo, other_rev, {rev2_charge}) == {
        rev2_charge: ()
    }


# ---- Pure signature rules ----


@pytest.mark.parametrize(
    ("signature", "bases"),
    [
        ("class A(unittest.TestCase)", ["unittest.TestCase"]),
        ("@decorator\nclass A(Base, metaclass=Meta)", ["Base"]),
        ("class A(Generic[T, U], TestCase)", ["Generic[T, U]", "TestCase"]),
        ("class A", []),
        (None, []),
    ],
)
def test_signature_bases(signature: str | None, bases: list[str]) -> None:
    assert _signature_bases(signature) == bases


@pytest.mark.parametrize(
    ("base", "is_testcase"),
    [
        ("unittest.TestCase", True),
        ("TestCase", True),
        ("unittest.IsolatedAsyncioTestCase", True),
        ("django.test.SimpleTestCase", True),
        ("TestCaseMixin", False),
        ("object", False),
    ],
)
def test_testcase_base_rule(base: str, is_testcase: bool) -> None:
    assert bool(_TESTCASE_BASE.search(base)) is is_testcase
