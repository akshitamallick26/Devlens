import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.ingestion.service import IngestionResult
from app.models import RepositoryRevision
from app.rag.persistence import ChunkGenerationResult, run_chunk_generation
from tests.graph.conftest import ingest_parse_and_build_graph
from tests.ingestion.conftest import run_git


@pytest.fixture
def rag_fixture_repo(tmp_path: Path) -> Path:
    """A small repository with real call/import structure (for graph
    expansion) plus distinctive docstrings/comments (for lexical full-text
    search) and a duplicate-named symbol in another file (for the
    identifier-search / revision-isolation tests)."""
    repo_dir = tmp_path / "rag-fixture-repo"
    repo_dir.mkdir()

    auth = repo_dir / "auth"
    auth.mkdir()
    (auth / "__init__.py").write_text("")
    (auth / "login.py").write_text(
        '"""Handles user authentication for incoming requests."""\n\n\n'
        "def authenticate(token):\n"
        '    """Verify a bearer token and return the authenticated user."""\n'
        "    return _decode(token)\n\n\n"
        "def _decode(token):\n"
        "    return token\n"
    )

    checkout = repo_dir / "checkout"
    checkout.mkdir()
    (checkout / "__init__.py").write_text("")
    (checkout / "pricing.py").write_text("def apply_discount(amount):\n    return amount * 0.9\n")
    (checkout / "service.py").write_text(
        "from checkout.pricing import apply_discount\nfrom auth.login import authenticate\n\n\n"
        "def calculate_total(token, amount):\n"
        '    """Authenticates the request, then applies the active discount."""\n'
        "    authenticate(token)\n"
        "    return apply_discount(amount)\n"
    )

    run_git(repo_dir, "init", "-q", "-b", "main")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "rag fixture")
    return repo_dir


@pytest.fixture
def rag_cyclic_repo(tmp_path: Path) -> Path:
    """Two mutually-recursive functions — a genuine cycle in the call
    graph, not just a seed re-appearing. Exercises graph expansion's
    cycle-safety for an actual cycle, not only the no-seed-reintroduction
    case the main fixture repo happens to cover."""
    repo_dir = tmp_path / "rag-cyclic-repo"
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


@pytest.fixture
def rag_many_chunks_repo(tmp_path: Path) -> Path:
    """A single file with enough distinct functions to produce more
    CodeChunk rows than `app/rag/embeddings.py::EMBEDDING_BATCH_SIZE` (64)
    — the only way to prove batching is actually exercised (a handful of
    chunks would all fit in one batch and the test would pass even if
    batching were silently broken back into one-call-per-chunk)."""
    repo_dir = tmp_path / "rag-many-chunks-repo"
    repo_dir.mkdir()
    functions = "\n\n".join(f"def fn_{i}():\n    return {i}" for i in range(80))
    (repo_dir / "many.py").write_text(functions + "\n")
    run_git(repo_dir, "init", "-q", "-b", "main")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "many chunks fixture")
    return repo_dir


def ingest_parse_graph_and_chunk(
    db: Session, repo_path: Path, owner: str = "rag-test"
) -> tuple[IngestionResult, object, ChunkGenerationResult]:
    """Shared full-pipeline helper: ingestion -> parsing -> graph
    construction -> chunk generation."""
    ingestion_result, graph_result = ingest_parse_and_build_graph(db, repo_path, owner)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    chunk_result = run_chunk_generation(db, repository_id, revision_id)
    return ingestion_result, graph_result, chunk_result


def get_revision(db: Session, revision_id: uuid.UUID) -> RepositoryRevision:
    revision = db.get(RepositoryRevision, revision_id)
    assert revision is not None
    return revision
