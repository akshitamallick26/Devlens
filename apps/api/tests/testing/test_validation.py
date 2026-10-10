"""Pure pre-execution validation tests — no DB, no subprocess, always run."""

import uuid

from app.testing.citations import Evidence, EvidenceSet
from app.testing.models import GeneratedTestOutput, TestTarget
from app.testing.validation import validate_generated_test

TARGET_ID = uuid.uuid4()


def _target() -> TestTarget:
    return TestTarget(
        target_symbol_id=TARGET_ID,
        qualified_name="checkout.pricing.apply_discount",
        kind="function",
        file_id=uuid.uuid4(),
        file_path="checkout/pricing.py",
        start_line=1,
        end_line=2,
        selection_reason="explicit_target",
        impact_depth=None,
        callers=(),
        callees=(),
        existing_tests=(),
    )


def _evidence_set() -> EvidenceSet:
    return EvidenceSet(
        items=(
            Evidence(
                evidence_id="T1",
                repository_id=uuid.uuid4(),
                revision_id=uuid.uuid4(),
                file_path="checkout/pricing.py",
                symbol_id=TARGET_ID,
                qualified_name="checkout.pricing.apply_discount",
                line_start=1,
                line_end=2,
                kind="target_source",
                content="def apply_discount(amount): return amount * 0.9",
            ),
        )
    )


def _output(
    *,
    source: str = "def test_apply_discount():\n    assert True\n",
    file_path: str = "test_apply_discount.py",
    target_symbol_id: uuid.UUID | None = None,
    cited_evidence_ids: tuple[str, ...] = ("T1",),
) -> GeneratedTestOutput:
    return GeneratedTestOutput(
        file_path=file_path,
        source=source,
        target_symbol_id=target_symbol_id or TARGET_ID,
        rationale="Exercises the target directly [T1].",
        cited_evidence_ids=cited_evidence_ids,
    )


def test_valid_output_passes() -> None:
    result = validate_generated_test(_output(), _target(), _evidence_set(), frozenset())
    assert result.passed
    assert result.issues == ()


def test_syntax_error_fails_closed() -> None:
    result = validate_generated_test(
        _output(source="def test_x(:\n    pass\n"), _target(), _evidence_set(), frozenset()
    )
    assert not result.passed
    assert any(issue.code == "syntax_error" for issue in result.issues)


def test_no_test_function_found() -> None:
    result = validate_generated_test(
        _output(source="def helper():\n    return 1\n"), _target(), _evidence_set(), frozenset()
    )
    assert not result.passed
    assert any(issue.code == "no_test_found" for issue in result.issues)


def test_unittest_style_test_class_is_discoverable() -> None:
    source = (
        "import unittest\n\n\n"
        "class TestApplyDiscount(unittest.TestCase):\n"
        "    def test_it(self):\n"
        "        self.assertTrue(True)\n"
    )
    result = validate_generated_test(
        _output(source=source), _target(), _evidence_set(), frozenset()
    )
    assert result.passed


def test_path_traversal_is_rejected() -> None:
    result = validate_generated_test(
        _output(file_path="../../etc/test_evil.py"), _target(), _evidence_set(), frozenset()
    )
    assert not result.passed
    assert any(issue.code == "unsafe_path" for issue in result.issues)


def test_absolute_path_is_rejected() -> None:
    result = validate_generated_test(
        _output(file_path="/etc/test_evil.py"), _target(), _evidence_set(), frozenset()
    )
    assert not result.passed
    assert any(issue.code == "unsafe_path" for issue in result.issues)


def test_non_test_filename_is_rejected() -> None:
    result = validate_generated_test(
        _output(file_path="helper.py"), _target(), _evidence_set(), frozenset()
    )
    assert not result.passed
    assert any(issue.code == "unsafe_path" for issue in result.issues)


def test_path_colliding_with_existing_source_is_rejected() -> None:
    result = validate_generated_test(
        _output(file_path="test_apply_discount.py"),
        _target(),
        _evidence_set(),
        frozenset({"test_apply_discount.py"}),
    )
    assert not result.passed
    assert any(issue.code == "overwrites_source" for issue in result.issues)


def test_unknown_target_reference_is_rejected() -> None:
    result = validate_generated_test(
        _output(target_symbol_id=uuid.uuid4()), _target(), _evidence_set(), frozenset()
    )
    assert not result.passed
    assert any(issue.code == "target_mismatch" for issue in result.issues)


def test_unknown_evidence_citation_is_rejected() -> None:
    result = validate_generated_test(
        _output(cited_evidence_ids=("T999",)), _target(), _evidence_set(), frozenset()
    )
    assert not result.passed
    assert any(issue.code == "unknown_evidence_id" for issue in result.issues)


def test_forbidden_subprocess_import_is_rejected() -> None:
    source = "import subprocess\n\n\ndef test_x():\n    subprocess.run(['ls'])\n"
    result = validate_generated_test(
        _output(source=source), _target(), _evidence_set(), frozenset()
    )
    assert not result.passed
    assert any(issue.code == "forbidden_import" for issue in result.issues)


def test_forbidden_os_system_call_is_rejected() -> None:
    source = "import os\n\n\ndef test_x():\n    os.system('ls')\n"
    result = validate_generated_test(
        _output(source=source), _target(), _evidence_set(), frozenset()
    )
    assert not result.passed
    assert any(issue.code == "forbidden_call" for issue in result.issues)


def test_forbidden_eval_call_is_rejected() -> None:
    source = "def test_x():\n    eval('1 + 1')\n"
    result = validate_generated_test(
        _output(source=source), _target(), _evidence_set(), frozenset()
    )
    assert not result.passed
    assert any(issue.code == "forbidden_call" for issue in result.issues)


def test_ordinary_os_path_usage_is_not_flagged() -> None:
    """os.path is routine and legitimate — only the specific
    process/exec-spawning attributes are forbidden, not the whole module."""
    source = "import os\n\n\ndef test_x():\n    assert os.path.join('a', 'b') == 'a/b'\n"
    result = validate_generated_test(
        _output(source=source), _target(), _evidence_set(), frozenset()
    )
    assert result.passed


def test_oversized_source_fails_closed() -> None:
    huge_source = "def test_x():\n    assert True  # " + ("x" * 300_000) + "\n"
    result = validate_generated_test(
        _output(source=huge_source), _target(), _evidence_set(), frozenset()
    )
    assert not result.passed
    assert any(issue.code == "source_too_large" for issue in result.issues)
