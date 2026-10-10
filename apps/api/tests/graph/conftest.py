import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.graph.service import GraphConstructionResult, run_graph_construction
from app.ingestion.service import IngestionResult, ingest_identity
from app.ingestion.url_policy import RepositoryIdentity
from app.models import GraphEdge, Symbol
from app.parsing.service import run_parsing
from tests.ingestion.conftest import run_git


def local_identity(path: Path, owner: str) -> RepositoryIdentity:
    return RepositoryIdentity(
        host="local-test",
        owner=owner,
        name=path.name,
        canonical_url=str(path),
        clone_url=str(path),
    )


def ingest(db: Session, repo_path: Path, owner: str = "graph-test") -> IngestionResult:
    return ingest_identity(db, local_identity(repo_path, owner))


def ingest_parse_and_build_graph(
    db: Session, repo_path: Path, owner: str = "graph-test"
) -> tuple[IngestionResult, GraphConstructionResult]:
    """Shared full-pipeline helper: ingestion -> parsing -> graph
    construction, used by test_service.py, test_false_positives.py, and
    test_revision_isolation.py."""
    ingestion_result = ingest(db, repo_path, owner)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    run_parsing(db, repository_id, revision_id)
    graph_result = run_graph_construction(db, repository_id, revision_id)
    return ingestion_result, graph_result


def symbols_by_qualified_name(db: Session, revision_id: uuid.UUID) -> dict[str, Symbol]:
    symbols = db.execute(select(Symbol).where(Symbol.revision_id == revision_id)).scalars().all()
    return {s.qualified_name: s for s in symbols}


def edges_for_revision(db: Session, revision_id: uuid.UUID) -> list[GraphEdge]:
    return db.execute(select(GraphEdge).where(GraphEdge.revision_id == revision_id)).scalars().all()


@pytest.fixture
def graph_fixture_repo(tmp_path: Path) -> Path:
    """A repository exercising every import form, call-resolution case, and
    false-positive guard this phase is responsible for (Tasks 18/19/20).

    - app/__init__.py: an empty package init (no module-level symbols).
    - app/payments.py: a free function, a base class with a self-method
      call, and a subclass that inherits it (internal `calls`/`inherits`
      edges, self-method resolution without MRO).
    - app/utils.py: a nested function (lexical-scope call resolution).
    - main.py: an aliased dotted import, an aliased from-import, and a
      stdlib import, each used at a call site — covers internal
      module-attribute calls, internal bare calls via from-import, and
      external attribute calls, plus all three import-edge kinds.
    - false_positives.py: the named false-positive scenarios from Task 18.
    """
    repo_dir = tmp_path / "graph-fixture-repo"
    repo_dir.mkdir()

    pkg = repo_dir / "app"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "payments.py").write_text(
        "def charge(amount):\n"
        "    return amount\n\n\n"
        "class Payment:\n"
        "    def process(self):\n"
        "        return self.validate()\n\n"
        "    def validate(self):\n"
        "        return True\n\n\n"
        "class PremiumPayment(Payment):\n"
        "    def process(self):\n"
        "        return super().process()\n"
    )
    (pkg / "utils.py").write_text(
        "def helper():\n    def inner():\n        return 1\n    return inner()\n"
    )

    (repo_dir / "main.py").write_text(
        "import app.payments as payments\n"
        "from app.payments import charge as do_charge\n"
        "import os\n\n\n"
        "def run():\n"
        "    payments.charge(5)\n"
        "    do_charge(5)\n"
        "    os.getcwd()\n"
    )

    (repo_dir / "false_positives.py").write_text(
        "from app.payments import charge\n\n\n"
        "def checkout():\n"
        "    charge = get_handler()\n"
        "    charge()\n\n\n"
        "def unknown_receiver(service):\n"
        "    service.charge()\n\n\n"
        "def unrelated_textual_match():\n"
        "    helper()\n\n\n"
        "def instance_without_type_inference():\n"
        "    p = make_payment()\n"
        "    p.process()\n\n\n"
        "class Unrelated:\n"
        "    def run(self):\n"
        "        return self.process()\n\n\n"
        "import app.payments\n\n\n"
        "def three_segment_chain_is_not_guessed():\n"
        "    app.payments.charge(5)\n"
    )

    run_git(repo_dir, "init", "-q", "-b", "main")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "graph fixture")
    return repo_dir


@pytest.fixture
def colliding_name_repo_a(tmp_path: Path) -> Path:
    """Defines `mod.helper` — paired with `colliding_name_repo_b`, which
    defines the identical qualified name, for Task 16's revision-isolation
    test: two revisions sharing a same-named symbol must never cross-link."""
    repo_dir = tmp_path / "colliding-repo-a"
    repo_dir.mkdir()
    (repo_dir / "mod.py").write_text(
        "def helper():\n    return 'a'\n\ndef caller():\n    return helper()\n"
    )
    run_git(repo_dir, "init", "-q", "-b", "main")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "repo a")
    return repo_dir


@pytest.fixture
def colliding_name_repo_b(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "colliding-repo-b"
    repo_dir.mkdir()
    (repo_dir / "mod.py").write_text(
        "def helper():\n    return 'b'\n\ndef caller():\n    return helper()\n"
    )
    run_git(repo_dir, "init", "-q", "-b", "main")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "repo b")
    return repo_dir
