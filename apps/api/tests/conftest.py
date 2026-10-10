"""Shared fixtures for the database invariant test suite.

These tests require a live PostgreSQL instance (they exercise real
CHECK/UNIQUE/FK constraints, which only a real database engine enforces).
If PostgreSQL isn't reachable — e.g. Docker isn't installed — the engine
fixture skips the whole session with a clear reason rather than failing or
silently passing. See docs/architecture/database-design.md for which parts
of this schema have only been validated statically, without a live database.

The suite runs under the explicit development configuration below, set
before any application module reads Settings: the API's default is
DEPLOYMENT_MODE=public_demo (docs/architecture/deployment-safety.md), and
the behavior these tests pin down is development behavior. Tests of
public_demo behavior switch modes with the `override_settings` fixture.
"""

import os
from collections.abc import Callable, Generator
from datetime import UTC, datetime

os.environ["DEPLOYMENT_MODE"] = "development"
os.environ["TEST_EXECUTION_ENABLED"] = "true"
os.environ["RATE_LIMIT_ENABLED"] = "false"

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401  (registers all tables on Base.metadata)
from app.core.config import get_settings
from app.db.base import Base
from app.models import AnalysisRun, Repository, RepositoryRevision, SourceBlob, SourceFile, Symbol


@pytest.fixture
def override_settings(monkeypatch: pytest.MonkeyPatch) -> Generator[Callable[..., None]]:
    """Returns `apply(**env)`: sets each name as an environment variable
    (None removes it) and rebuilds the cached Settings, so every module that
    calls get_settings() sees the new configuration. Restored afterwards."""

    def apply(**env: str | None) -> None:
        for name, value in env.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def engine() -> Generator[Engine]:
    database_url = get_settings().database_url
    eng = create_engine(database_url)
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL not reachable at {database_url!r}: {exc}")

    with eng.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()

    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Generator[Session]:
    """A session bound to a single connection/transaction, rolled back after
    each test for isolation — no test's data leaks into another's."""
    connection = engine.connect()
    transaction = connection.begin()
    # create_savepoint: each session transaction is a SAVEPOINT inside the
    # outer test transaction, so a service's commit() keeps its work and a
    # later rollback() undoes only uncommitted work, exactly as in
    # production. (The default mode lets a service's rollback() discard the
    # whole outer transaction, which made rollback paths untestable.)
    session_factory = sessionmaker(bind=connection, join_transaction_mode="create_savepoint")
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def repository(db_session: Session) -> Repository:
    repo = Repository(
        owner="octocat",
        name="hello-world",
        url="https://github.com/octocat/hello-world",
        default_branch="main",
    )
    db_session.add(repo)
    db_session.flush()
    return repo


@pytest.fixture
def revision(db_session: Session, repository: Repository) -> RepositoryRevision:
    rev = RepositoryRevision(
        repository_id=repository.id,
        commit_sha="a" * 40,
        committed_at=datetime.now(UTC),
    )
    db_session.add(rev)
    db_session.flush()
    return rev


@pytest.fixture
def source_file(
    db_session: Session, repository: Repository, revision: RepositoryRevision
) -> SourceFile:
    blob = SourceBlob(content_hash="b" * 40, inline_content="print('hi')", size_bytes=12)
    db_session.add(blob)
    db_session.flush()

    file = SourceFile(
        repository_id=repository.id,
        revision_id=revision.id,
        path="src/main.py",
        content_hash=blob.content_hash,
        size_bytes=12,
    )
    db_session.add(file)
    db_session.flush()
    return file


@pytest.fixture
def symbol(
    db_session: Session,
    repository: Repository,
    revision: RepositoryRevision,
    source_file: SourceFile,
) -> Symbol:
    sym = Symbol(
        repository_id=repository.id,
        revision_id=revision.id,
        file_id=source_file.id,
        kind="function",
        name="main",
        qualified_name="src.main.main",
        start_line=1,
        end_line=3,
        analyzer="tree-sitter-python@0.21",
    )
    db_session.add(sym)
    db_session.flush()
    return sym


@pytest.fixture
def analysis_run(
    db_session: Session, repository: Repository, revision: RepositoryRevision
) -> AnalysisRun:
    run = AnalysisRun(
        repository_id=repository.id,
        revision_id=revision.id,
        analysis_type="parsing",
        analyzer_name="tree-sitter-python",
        analyzer_version="0.21.0",
    )
    db_session.add(run)
    db_session.flush()
    return run


def make_repo_and_revision(
    db_session: Session, owner: str
) -> tuple[Repository, RepositoryRevision]:
    """Helper (not a fixture — call directly) for tests that need a SECOND,
    unrelated repository/revision pair to prove cross-repository/cross-revision
    references are rejected."""
    repo = Repository(
        owner=owner, name="repo", url=f"https://x/{owner}/repo", default_branch="main"
    )
    db_session.add(repo)
    db_session.flush()
    rev = RepositoryRevision(
        repository_id=repo.id, commit_sha="f" * 40, committed_at=datetime.now(UTC)
    )
    db_session.add(rev)
    db_session.flush()
    return repo, rev
