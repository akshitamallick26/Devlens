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
    db: Session, repo_path: Path, owner: str = "security-test"
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
def security_fixture_repo(tmp_path: Path) -> Path:
    """A small repository exercising Task 8/19's graph-resolution matrix:
    a package ("flask") imported from exactly one place (exact_symbol), a
    package ("jinja2") imported from two places (ambiguous), a source
    pattern (eval) at a known, callable location (exact_symbol + a real
    caller for blast radius), and a non-Python manifest file with no
    Symbol rows at all (exact_file, since Semgrep/OSV can reference a file
    this project's own parser never produced symbols for)."""
    repo_dir = tmp_path / "security-fixture-repo"
    repo_dir.mkdir()

    (repo_dir / "requirements.txt").write_text("jinja2==2.11.2\nflask==1.0.0\n")

    app = repo_dir / "app"
    app.mkdir()
    (app / "__init__.py").write_text("")
    (app / "render.py").write_text(
        "import flask\n\n\ndef render_template(name):\n    return flask.render_template(name)\n"
    )
    (app / "jinja_a.py").write_text(
        "import jinja2\n\n\ndef render_a():\n    return jinja2.Template('a').render()\n"
    )
    (app / "jinja_b.py").write_text(
        "import jinja2\n\n\ndef render_b():\n    return jinja2.Template('b').render()\n"
    )
    (app / "unsafe.py").write_text("def run_user_code(code):\n    return eval(code)\n")
    (app / "unsafe_caller.py").write_text(
        "from app.unsafe import run_user_code\n\n\n"
        "def process(data):\n    return run_user_code(data)\n"
    )

    run_git(repo_dir, "init", "-q", "-b", "main")
    run_git(repo_dir, "add", "-A")
    run_git(repo_dir, "commit", "-q", "-m", "security fixture")
    return repo_dir
