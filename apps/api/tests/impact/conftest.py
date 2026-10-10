from pathlib import Path

import pytest

from tests.ingestion.conftest import run_git


@pytest.fixture
def impact_fixture_repo(tmp_path: Path) -> Path:
    """A repository built through real Python source, exercising every
    traversal scenario Task 10 lists:

    - commerce/: a straight calls chain (checkout_order -> calculate_total
      -> apply_discount) plus a module-level import (checkout.py imports
      commerce.pricing) — depth-3 transitive impact and an import edge for
      file-level aggregation.
    - diamond/: a_func -> b_func -> d_func and a_func -> c_func -> d_func —
      the diamond-merge / shortest-depth / branch-merging scenario, with
      exactly the depths worked through in impact-analysis.md section 2
      (Impact(d_func): b_func=1, c_func=1, a_func=2).
    - cycle/: x_func -> y_func -> z_func -> x_func — a real three-node
      cycle via (intentionally) circular imports. Never actually executed,
      only statically parsed, so Python's own circular-import restriction
      at runtime is irrelevant here.
    """
    repo_dir = tmp_path / "impact-fixture-repo"
    repo_dir.mkdir()

    commerce = repo_dir / "commerce"
    commerce.mkdir()
    (commerce / "__init__.py").write_text("")
    (commerce / "discount.py").write_text("def apply_discount(amount):\n    return amount * 0.9\n")
    (commerce / "total.py").write_text(
        "from commerce.discount import apply_discount\n\n\n"
        "def calculate_total(amount):\n    return apply_discount(amount)\n"
    )
    (commerce / "pricing.py").write_text("def get_tax_rate():\n    return 0.08\n")
    (commerce / "checkout.py").write_text(
        "from commerce.total import calculate_total\n"
        "import commerce.pricing as pricing\n\n\n"
        "def checkout_order(amount):\n    return calculate_total(amount)\n"
    )

    diamond = repo_dir / "diamond"
    diamond.mkdir()
    (diamond / "__init__.py").write_text("")
    (diamond / "d.py").write_text("def d_func():\n    return 1\n")
    (diamond / "b.py").write_text(
        "from diamond.d import d_func\n\n\ndef b_func():\n    return d_func()\n"
    )
    (diamond / "c.py").write_text(
        "from diamond.d import d_func\n\n\ndef c_func():\n    return d_func()\n"
    )
    (diamond / "a.py").write_text(
        "from diamond.b import b_func\n"
        "from diamond.c import c_func\n\n\n"
        "def a_func():\n    return b_func() + c_func()\n"
    )

    cycle = repo_dir / "cycle"
    cycle.mkdir()
    (cycle / "__init__.py").write_text("")
    (cycle / "x.py").write_text(
        "from cycle.y import y_func\n\n\ndef x_func():\n    return y_func()\n"
    )
    (cycle / "y.py").write_text(
        "from cycle.z import z_func\n\n\ndef y_func():\n    return z_func()\n"
    )
    (cycle / "z.py").write_text(
        "from cycle.x import x_func\n\n\ndef z_func():\n    return x_func()\n"
    )

    run_git(repo_dir, "init", "-q", "-b", "main")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "impact fixture")
    return repo_dir


@pytest.fixture
def other_revision_repo(tmp_path: Path) -> Path:
    """A second, unrelated repository that happens to define a symbol with
    the identical qualified name as `impact_fixture_repo`'s
    "diamond.d.d_func" — and something that depends on it — for the
    cross-revision isolation test (Task 10's "OTHER -> D must not appear")."""
    repo_dir = tmp_path / "other-revision-repo"
    repo_dir.mkdir()

    diamond = repo_dir / "diamond"
    diamond.mkdir()
    (diamond / "__init__.py").write_text("")
    (diamond / "d.py").write_text("def d_func():\n    return 2\n")
    (diamond / "other.py").write_text(
        "from diamond.d import d_func\n\n\ndef other_func():\n    return d_func()\n"
    )

    run_git(repo_dir, "init", "-q", "-b", "main")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "other revision fixture")
    return repo_dir
