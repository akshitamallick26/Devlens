import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.graph.service import run_graph_construction
from app.ingestion.service import IngestionResult, ingest_identity
from app.parsing.service import run_parsing
from tests.graph.conftest import local_identity
from tests.ingestion.conftest import run_git


def ingest_revision(db: Session, repo_path: Path, owner: str) -> IngestionResult:
    """Ingests whatever the repository's current HEAD commit is — calling
    this twice for the same `owner` against a repo that has since received
    a new commit produces a second RepositoryRevision whose `parent_sha`
    correctly points at the first commit (a real git fact, not a test
    fixture invention)."""
    return ingest_identity(db, local_identity(repo_path, owner))


def analyze_revision(db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID) -> None:
    """Simulates "this revision was already fully analyzed at some point in
    the past" — used to set up the *parent* revision's own Symbol/GraphEdge
    data before automation runs against a new *current* revision. Automation
    itself is responsible for analyzing the current revision; tests never
    pre-analyze the current revision before calling it."""
    run_parsing(db, repository_id, revision_id)
    run_graph_construction(db, repository_id, revision_id)


def modify_and_commit(repo_dir: Path, relative_path: str, content: str, message: str) -> None:
    (repo_dir / relative_path).write_text(content)
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", message)


def delete_and_commit(repo_dir: Path, relative_path: str, message: str) -> None:
    (repo_dir / relative_path).unlink()
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", message)


@pytest.fixture
def automation_fixture_repo(tmp_path: Path) -> Path:
    """A small repository with a real call relationship (app.caller.compute
    calls app.math_utils.add) and an unused function (app.math_utils.unused)
    — enough to exercise file/symbol delta, change impact (direct dependent
    via `add`), and an unchanged-file baseline (app/caller.py stays
    untouched across the adversarial tests that only modify math_utils.py)."""
    repo_dir = tmp_path / "automation-fixture-repo"
    repo_dir.mkdir()
    app_dir = repo_dir / "app"
    app_dir.mkdir()
    (app_dir / "__init__.py").write_text("")
    (app_dir / "math_utils.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef unused():\n    return 1\n"
    )
    (app_dir / "caller.py").write_text(
        "from app.math_utils import add\n\n\ndef compute():\n    return add(1, 2)\n"
    )
    run_git(repo_dir, "init", "-q", "-b", "main")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "initial")
    return repo_dir
