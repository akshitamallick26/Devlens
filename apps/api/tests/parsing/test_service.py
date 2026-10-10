"""Full-pipeline parsing tests: a realistic local Git repo -> ingestion ->
parsing -> real Symbol/SourceFile/AnalysisRun rows. Requires live PostgreSQL
— same `db_session` fixture and explicit-skip behavior as tests/db/ and
tests/ingestion/. Never substitutes SQLite.
"""

import uuid
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ingestion.service import IngestionResult, ingest_identity
from app.ingestion.url_policy import RepositoryIdentity
from app.models import AnalysisRun, GraphEdge, SourceFile, Symbol
from app.parsing.service import ParsingRunResult, run_parsing


def _local_identity(path: Path) -> RepositoryIdentity:
    return RepositoryIdentity(
        host="local-test",
        owner="parsing-test",
        name="realistic-repo",
        canonical_url=str(path),
        clone_url=str(path),
    )


def _ingest(db: Session, repo_path: Path) -> IngestionResult:
    return ingest_identity(db, _local_identity(repo_path))


def _parse(db: Session, ingestion_result: IngestionResult) -> ParsingRunResult:
    return run_parsing(
        db,
        uuid.UUID(ingestion_result.repository_id),
        uuid.UUID(ingestion_result.revision_id),
    )


def _ingest_and_parse(db: Session, repo_path: Path) -> ParsingRunResult:
    return _parse(db, _ingest(db, repo_path))


def test_source_files_are_parsed_and_the_invalid_file_fails_cleanly(
    db_session: Session, realistic_python_repo: Path
) -> None:
    result = _ingest_and_parse(db_session, realistic_python_repo)

    assert result.files_parsed == 4  # __init__.py, utils.py, models.py, app.py
    assert result.files_failed == 1  # broken.py
    assert result.files_skipped == 0

    files = db_session.execute(select(SourceFile)).scalars().all()
    by_path = {f.path: f for f in files}
    assert by_path["broken.py"].parsing_status == "failed"
    for path in ("mypkg/__init__.py", "mypkg/utils.py", "mypkg/models.py", "app.py"):
        assert by_path[path].parsing_status == "parsed"


def test_a_failed_file_has_no_partial_symbols(
    db_session: Session, realistic_python_repo: Path
) -> None:
    _ingest_and_parse(db_session, realistic_python_repo)

    broken = db_session.execute(
        select(SourceFile).where(SourceFile.path == "broken.py")
    ).scalar_one()
    symbols_for_broken_file = (
        db_session.execute(select(Symbol).where(Symbol.file_id == broken.id)).scalars().all()
    )
    assert symbols_for_broken_file == []


def test_symbols_belong_to_the_correct_source_file(
    db_session: Session, realistic_python_repo: Path
) -> None:
    _ingest_and_parse(db_session, realistic_python_repo)

    utils_file = db_session.execute(
        select(SourceFile).where(SourceFile.path == "mypkg/utils.py")
    ).scalar_one()
    models_file = db_session.execute(
        select(SourceFile).where(SourceFile.path == "mypkg/models.py")
    ).scalar_one()

    utils_symbols = {
        s.qualified_name
        for s in db_session.execute(select(Symbol).where(Symbol.file_id == utils_file.id))
        .scalars()
        .all()
    }
    models_symbols = {
        s.qualified_name
        for s in db_session.execute(select(Symbol).where(Symbol.file_id == models_file.id))
        .scalars()
        .all()
    }
    assert "mypkg.utils.helper" in utils_symbols
    assert "mypkg.utils.helper.double" in utils_symbols
    assert "mypkg.models.User" in models_symbols
    assert "mypkg.models.Admin" not in utils_symbols  # no cross-file leakage
    assert utils_symbols.isdisjoint(models_symbols)


def test_qualified_names_and_parent_links_are_correct_across_the_package(
    db_session: Session, realistic_python_repo: Path
) -> None:
    _ingest_and_parse(db_session, realistic_python_repo)

    symbols = db_session.execute(select(Symbol)).scalars().all()
    by_qname = {s.qualified_name: s for s in symbols}

    assert "mypkg.models.Admin.greet" in by_qname
    admin_greet = by_qname["mypkg.models.Admin.greet"]
    admin = by_qname["mypkg.models.Admin"]
    assert admin_greet.parent_symbol_id == admin.id
    assert admin_greet.kind == "method"

    user = by_qname["mypkg.models.User"]
    user_greet = by_qname["mypkg.models.User.greet"]
    assert user_greet.parent_symbol_id == user.id
    # Two methods named "greet" in different classes are distinct symbols.
    assert admin_greet.id != user_greet.id


def test_source_locations_are_populated(db_session: Session, realistic_python_repo: Path) -> None:
    _ingest_and_parse(db_session, realistic_python_repo)
    symbols = db_session.execute(select(Symbol)).scalars().all()
    for symbol in symbols:
        assert symbol.start_line >= 1
        assert symbol.end_line >= symbol.start_line


def test_repeated_parsing_is_idempotent(db_session: Session, realistic_python_repo: Path) -> None:
    ingestion_result = _ingest(db_session, realistic_python_repo)
    first = _parse(db_session, ingestion_result)
    symbol_count_after_first = db_session.scalar(select(func.count()).select_from(Symbol))

    second = _parse(db_session, ingestion_result)
    symbol_count_after_second = db_session.scalar(select(func.count()).select_from(Symbol))

    assert second.symbol_count == first.symbol_count
    assert symbol_count_after_first == symbol_count_after_second

    analysis_runs = (
        db_session.execute(select(AnalysisRun).where(AnalysisRun.analysis_type == "parsing"))
        .scalars()
        .all()
    )
    assert len(analysis_runs) == 2  # one run per parse attempt — history preserved
    assert all(run.status == "completed" for run in analysis_runs)


def test_no_graph_edges_are_created_by_parsing(
    db_session: Session, realistic_python_repo: Path
) -> None:
    _ingest_and_parse(db_session, realistic_python_repo)
    edge_count = db_session.scalar(select(func.count()).select_from(GraphEdge))
    assert edge_count == 0
