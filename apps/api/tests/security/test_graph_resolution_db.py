"""DB-dependent graph-resolution tests (Task 8/19). Requires live
PostgreSQL — same `db_session` fixture and explicit-skip behavior as every
other DB-dependent suite in this project. Never substitutes SQLite.
"""

import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app.security.graph_resolution import (
    fold_dependency_resolutions,
    resolve_dependency_finding,
    resolve_source_finding,
)
from tests.security.conftest import ingest_parse_and_graph, symbol_id


def test_exact_symbol_resolution_for_a_sole_importer(
    db_session: Session, security_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, security_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    resolutions = resolve_dependency_finding(
        db_session, repository_id, revision_id, "flask", "PyPI"
    )
    folded = fold_dependency_resolutions(resolutions)
    assert folded.status == "exact_symbol"
    assert folded.symbol_id is not None


def test_ambiguous_resolution_for_multiple_importers(
    db_session: Session, security_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, security_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    resolutions = resolve_dependency_finding(
        db_session, repository_id, revision_id, "jinja2", "PyPI"
    )
    assert len(resolutions) == 2  # app/jinja_a.py and app/jinja_b.py both import it
    folded = fold_dependency_resolutions(resolutions)
    assert folded.status == "ambiguous"
    assert folded.symbol_id is None


def test_unresolved_dependency_with_no_importers(
    db_session: Session, security_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, security_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    resolutions = resolve_dependency_finding(
        db_session, repository_id, revision_id, "some-unimported-package", "PyPI"
    )
    assert resolutions == ()
    assert fold_dependency_resolutions(resolutions).status == "unresolved"


def test_non_pypi_ecosystem_is_never_resolved_via_python_import_graph(
    db_session: Session, security_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, security_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    resolutions = resolve_dependency_finding(db_session, repository_id, revision_id, "flask", "npm")
    assert resolutions == ()


def test_source_finding_resolves_to_the_containing_function(
    db_session: Session, security_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, security_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)
    expected_symbol_id = symbol_id(db_session, revision_id, "app.unsafe.run_user_code")

    resolution = resolve_source_finding(db_session, repository_id, revision_id, "app/unsafe.py", 2)
    assert resolution.status == "exact_symbol"
    assert resolution.symbol_id == expected_symbol_id


def test_source_finding_in_a_file_with_no_symbols_is_exact_file(
    db_session: Session, security_fixture_repo: Path
) -> None:
    """requirements.txt is ingested as a SourceFile but is not Python —
    this project's own parser never produces Symbol rows for it, exactly
    the situation where a scanner can point at a real file our graph has
    no symbol data for."""
    ingestion_result = ingest_parse_and_graph(db_session, security_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    resolution = resolve_source_finding(
        db_session, repository_id, revision_id, "requirements.txt", 1
    )
    assert resolution.status == "exact_file"
    assert resolution.symbol_id is None
    assert resolution.file_id is not None


def test_source_finding_in_a_nonexistent_file_is_unresolved(
    db_session: Session, security_fixture_repo: Path
) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, security_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    resolution = resolve_source_finding(
        db_session, repository_id, revision_id, "does/not/exist.py", 1
    )
    assert resolution.status == "unresolved"


def test_cross_revision_isolation(db_session: Session, security_fixture_repo: Path) -> None:
    first_result = ingest_parse_and_graph(db_session, security_fixture_repo, owner="owner-a")
    second_result = ingest_parse_and_graph(db_session, security_fixture_repo, owner="owner-b")
    first_repo = uuid.UUID(first_result.repository_id)
    first_rev = uuid.UUID(first_result.revision_id)
    second_rev = uuid.UUID(second_result.revision_id)

    resolutions = resolve_dependency_finding(db_session, first_repo, first_rev, "flask", "PyPI")
    assert len(resolutions) == 1
    second_rev_symbol_id = symbol_id(db_session, second_rev, "app.render.render_template")
    assert resolutions[0].symbol_id != second_rev_symbol_id


def test_cross_repository_isolation(db_session: Session, security_fixture_repo: Path) -> None:
    first_result = ingest_parse_and_graph(db_session, security_fixture_repo, owner="owner-a")
    second_result = ingest_parse_and_graph(db_session, security_fixture_repo, owner="owner-b")
    second_repo = uuid.UUID(second_result.repository_id)
    first_rev = uuid.UUID(first_result.revision_id)

    # Querying repository A's resolution with repository B's id must find nothing.
    resolutions = resolve_dependency_finding(db_session, second_repo, first_rev, "flask", "PyPI")
    assert resolutions == ()
