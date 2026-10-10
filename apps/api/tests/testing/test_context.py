"""Pure test-generation context assembly tests — no DB, always run."""

import uuid

from app.testing.context import (
    MAX_EVIDENCE_ITEMS,
    MAX_TOTAL_CONTENT_CHARS,
    ConfigurationFile,
    assemble_test_context,
)
from app.testing.models import ExistingTestRef, RelatedSymbol, TestTarget

REPO_ID = uuid.uuid4()
REVISION_ID = uuid.uuid4()


def _target(
    callers: tuple[RelatedSymbol, ...] = (),
    callees: tuple[RelatedSymbol, ...] = (),
    existing_tests: tuple[ExistingTestRef, ...] = (),
) -> TestTarget:
    return TestTarget(
        target_symbol_id=uuid.uuid4(),
        qualified_name="checkout.pricing.apply_discount",
        kind="function",
        file_id=uuid.uuid4(),
        file_path="checkout/pricing.py",
        start_line=1,
        end_line=2,
        selection_reason="explicit_target",
        impact_depth=None,
        callers=callers,
        callees=callees,
        existing_tests=existing_tests,
    )


def _related(name: str) -> RelatedSymbol:
    return RelatedSymbol(
        symbol_id=uuid.uuid4(),
        qualified_name=name,
        kind="function",
        file_id=uuid.uuid4(),
        file_path=f"{name}.py",
        start_line=1,
        end_line=2,
        relationship_type="calls",
    )


def test_target_source_always_comes_first() -> None:
    target = _target(callers=(_related("caller"),))
    package = assemble_test_context(
        REPO_ID, REVISION_ID, target, "def apply_discount(): ...", {}, {}, {}
    )
    assert package.evidence.items[0].kind == "target_source"
    assert package.evidence.items[0].evidence_id == "T1"


def test_priority_order_is_target_then_caller_then_callee_then_existing_test() -> None:
    caller = _related("caller")
    callee = _related("callee")
    existing_test = ExistingTestRef(
        file_id=uuid.uuid4(),
        file_path="test_pricing.py",
        symbol_id=uuid.uuid4(),
        test_identifier="test_apply_discount",
        kind="pytest_function",
        relevance_reasons=("graph_relationship",),
    )
    target = _target(callers=(caller,), callees=(callee,), existing_tests=(existing_test,))
    package = assemble_test_context(REPO_ID, REVISION_ID, target, "x", {}, {}, {})
    kinds = [item.kind for item in package.evidence.items]
    assert kinds == ["target_source", "caller", "callee", "existing_test"]


def test_configuration_file_is_included_last() -> None:
    target = _target()
    config = (ConfigurationFile(file_path="conftest.py", content="import pytest"),)
    package = assemble_test_context(REPO_ID, REVISION_ID, target, "x", {}, {}, {}, config)
    assert package.evidence.items[-1].kind == "configuration"
    assert package.evidence.items[-1].file_path == "conftest.py"


def test_missing_content_degrades_to_metadata_never_fabricated() -> None:
    target = _target()
    package = assemble_test_context(REPO_ID, REVISION_ID, target, None, {}, {}, {})
    assert package.evidence.items[0].content is None
    assert package.evidence.items[0].file_path == "checkout/pricing.py"


def test_context_is_bounded_by_max_evidence_items() -> None:
    callers = tuple(_related(f"caller_{i}") for i in range(MAX_EVIDENCE_ITEMS + 5))
    target = _target(callers=callers)
    package = assemble_test_context(REPO_ID, REVISION_ID, target, "x", {}, {}, {})
    assert len(package.evidence.items) <= MAX_EVIDENCE_ITEMS


def test_oversized_content_degrades_to_metadata_not_omitted() -> None:
    target = _target()
    huge = "x" * (MAX_TOTAL_CONTENT_CHARS + 1)
    package = assemble_test_context(REPO_ID, REVISION_ID, target, huge, {}, {}, {})
    assert len(package.evidence.items) == 1
    assert package.evidence.items[0].content is None


def test_deterministic_repeated_assembly_is_identical() -> None:
    target = _target(callers=(_related("caller"),))
    first = assemble_test_context(REPO_ID, REVISION_ID, target, "x", {}, {}, {})
    second = assemble_test_context(REPO_ID, REVISION_ID, target, "x", {}, {}, {})
    assert first == second


def test_evidence_ids_are_sequential_and_stable() -> None:
    target = _target(callers=(_related("caller"), _related("caller2")))
    package = assemble_test_context(REPO_ID, REVISION_ID, target, "x", {}, {}, {})
    assert [item.evidence_id for item in package.evidence.items] == ["T1", "T2", "T3"]
