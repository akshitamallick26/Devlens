from pathlib import Path

import pytest

from tests.ingestion.conftest import run_git


@pytest.fixture
def realistic_python_repo(tmp_path: Path) -> Path:
    """A small but structurally real repository: a package with an
    `__init__.py`, a module with a relative import, a module with class
    inheritance and a method, a top-level app module with an absolute
    import, and one intentionally invalid Python file."""
    repo_dir = tmp_path / "realistic-repo"
    repo_dir.mkdir()

    pkg = repo_dir / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("from .utils import helper\n")
    (pkg / "utils.py").write_text(
        "def helper(value: int) -> int:\n"
        "    def double(x):\n"
        "        return x * 2\n"
        "    return double(value)\n"
    )
    (pkg / "models.py").write_text(
        "class User:\n"
        "    def greet(self):\n"
        "        return 'hi'\n\n"
        "class Admin(User):\n"
        "    def greet(self):\n"
        "        return super().greet() + '!'\n"
    )

    (repo_dir / "app.py").write_text(
        "import mypkg\nfrom mypkg.utils import helper\n\ndef main():\n    return helper(1)\n"
    )

    (repo_dir / "broken.py").write_text("def broken(:\n    pass\n")

    run_git(repo_dir, "init", "-q", "-b", "main")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "realistic fixture")
    return repo_dir
