"""Fixture repository for test-discovery hardening (Testing Intelligence
v1.1). Every case below is a deliberate positive or negative discovery
case; see tests/testing/test_discovery_hardening_db.py."""

from pathlib import Path

from tests.ingestion.conftest import run_git

FILES = {
    "payments/__init__.py": "",
    "payments/service.py": (
        "def charge(amount):\n"
        "    return validate(amount)\n\n\n"
        "def validate(amount):\n"
        "    return amount > 0\n\n\n"
        "def refund(amount):\n"
        "    return charge(-amount)\n"
    ),
    "payments/similar.py": "def charge_card(card):\n    return card\n",
    "other/__init__.py": "",
    "other/service.py": "def charge(amount):\n    return 0\n",
    "tests/__init__.py": "",
    # Direct callers of payments.service.charge: function, pytest class
    # method, plus non-tests (Helper class method, nested function).
    "tests/test_payments.py": (
        "from payments.service import charge\n\n\n"
        "def test_charge():\n"
        "    assert charge(5)\n\n\n"
        "class TestCharge:\n"
        "    def test_method(self):\n"
        "        assert charge(2)\n\n\n"
        "class Helper:\n"
        "    def test_not_collected(self):\n"
        "        return charge(3)\n\n\n"
        "def test_outer():\n"
        "    def test_inner():\n"
        "        return charge(4)\n"
        "    assert test_inner()\n"
    ),
    # Nested package; imports the target module itself (module symbol).
    "tests/payments/__init__.py": "",
    "tests/payments/test_service.py": (
        "import payments.service\n\n\ndef test_charge():\n    assert payments.service.validate(1)\n"
    ),
    # unittest via `from unittest import TestCase`, class not named Test*.
    "tests/test_unit.py": (
        "from unittest import TestCase\n"
        "from payments.service import charge\n\n\n"
        "class ChargeCase(TestCase):\n"
        "    def test_positive(self):\n"
        "        self.assertTrue(charge(1))\n"
    ),
    # Tests a caller (refund) of charge: imports charge's module, never
    # calls charge directly.
    "tests/test_refund.py": (
        "from payments.service import refund\n\n\n"
        "def test_refund():\n"
        "    assert refund(1) is not None\n"
    ),
    # Named after the target's module and test name, related to nothing.
    "tests/test_service.py": (
        "def test_charge_unrelated():\n"
        "    assert True\n\n\n"
        "def test_service_nothing():\n"
        "    assert 1 == 1\n"
    ),
    # Mentions the target only in a string and a comment.
    "tests/test_strings.py": (
        "def test_charge_mentioned():\n"
        "    # charge(5) from payments.service\n"
        "    text = 'payments.service.charge(5)'\n"
        "    assert 'charge' in text\n"
    ),
}


def write_discovery_repo(root: Path) -> Path:
    for relative, content in FILES.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "discovery fixture")
    return root
