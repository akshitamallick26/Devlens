import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingestion.service import IngestionResult
from app.models import Symbol
from tests.graph.conftest import ingest_parse_and_build_graph
from tests.ingestion.conftest import run_git


def ingest_parse_and_graph(
    db: Session, repo_path: Path, owner: str = "testing-test"
) -> IngestionResult:
    ingestion_result, _ = ingest_parse_and_build_graph(db, repo_path, owner)
    return ingestion_result


def symbol_id(db: Session, revision_id: uuid.UUID, qualified_name: str) -> uuid.UUID:
    symbol = (
        db.execute(
            select(Symbol)
            .where(Symbol.revision_id == revision_id)
            .where(Symbol.qualified_name == qualified_name)
        )
        .scalars()
        .one()
    )
    return symbol.id


@pytest.fixture
def testing_fixture_repo(tmp_path: Path) -> Path:
    """A small repository exercising Task 4/17's existing-test discovery
    matrix: a pytest-style test and a unittest-style test that both
    genuinely call the target (graph_relationship evidence), an unrelated
    test with a misleading filename that does NOT reference the target, and
    a same-named symbol in a different module the discovery must not
    conflate with the real target."""
    repo_dir = tmp_path / "testing-fixture-repo"
    repo_dir.mkdir()

    checkout = repo_dir / "checkout"
    checkout.mkdir()
    (checkout / "__init__.py").write_text("")
    (checkout / "pricing.py").write_text("def apply_discount(amount):\n    return amount * 0.9\n")
    (checkout / "service.py").write_text(
        "from checkout.pricing import apply_discount\n\n\n"
        "def calculate_total(amount):\n"
        "    return apply_discount(amount)\n"
    )
    # Same-named symbol in an unrelated module — must never be conflated
    # with checkout.pricing.apply_discount by identifier-overlap alone.
    (repo_dir / "unrelated.py").write_text("def apply_discount(amount):\n    return amount\n")

    (repo_dir / "test_pricing.py").write_text(
        "from checkout.pricing import apply_discount\n\n\n"
        "def test_apply_discount():\n"
        "    assert apply_discount(100) == 90\n"
    )
    (repo_dir / "test_pricing_legacy.py").write_text(
        "import unittest\n"
        "from checkout.pricing import apply_discount\n\n\n"
        "class TestPricingLegacy(unittest.TestCase):\n"
        "    def test_apply_discount_legacy(self):\n"
        "        self.assertEqual(apply_discount(100), 90)\n"
    )
    # Misleading filename: named like a pricing test, but imports/calls
    # nothing related to checkout.pricing.apply_discount — must not be
    # linked to it on filename resemblance alone.
    (repo_dir / "test_pricing_unrelated.py").write_text(
        "def test_something_else():\n    assert 1 + 1 == 2\n"
    )

    run_git(repo_dir, "init", "-q", "-b", "main")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "testing fixture")
    return repo_dir


@pytest.fixture
def testing_cyclic_repo(tmp_path: Path) -> Path:
    """Two mutually-recursive functions — a genuine cycle in the call
    graph, for target discovery's adversarial cyclic-graph case."""
    repo_dir = tmp_path / "testing-cyclic-repo"
    repo_dir.mkdir()
    (repo_dir / "mutual.py").write_text(
        "def is_even(n):\n"
        "    if n == 0:\n"
        "        return True\n"
        "    return is_odd(n - 1)\n\n\n"
        "def is_odd(n):\n"
        "    if n == 0:\n"
        "        return False\n"
        "    return is_even(n - 1)\n"
    )
    run_git(repo_dir, "init", "-q", "-b", "main")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "cyclic fixture")
    return repo_dir
