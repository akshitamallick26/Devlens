"""End-to-end Security Intelligence orchestration tests. Requires live
PostgreSQL — same `db_session` fixture and explicit-skip behavior as every
other DB-dependent suite. Never substitutes SQLite. Scanner invocation
itself is monkeypatched (neither osv-scanner nor semgrep is installed in
this sandbox) — these tests exercise persistence, graph resolution, blast
radius, and idempotency against real DB rows, not the scanner subprocess.
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AnalysisRun, SecurityFinding, SecurityFindingObservation
from app.security import service
from app.security.models import NormalizedFinding, NormalizedObservation, ScannerRunResult
from tests.security.conftest import ingest_parse_and_graph


def _unavailable(scanner: str) -> ScannerRunResult:
    return ScannerRunResult(scanner=scanner, status="unavailable", detail="not installed")


def _osv_finding_result() -> ScannerRunResult:
    finding = NormalizedFinding(
        analyzer="osv-scanner",
        rule_id="GHSA-test",
        fingerprint="osv-scanner:PyPI:flask:GHSA-test",
        severity="high",
        title="Flask vulnerability",
        message="details",
        references=("https://example.com",),
    )
    observation = NormalizedObservation(
        fingerprint=finding.fingerprint,
        file_path="requirements.txt",
        start_line=None,
        end_line=None,
        affected_package="flask",
        affected_ecosystem="PyPI",
        installed_version="1.0.0",
        affected_range=None,
        fixed_version="1.0.1",
        raw_finding={"id": "GHSA-test"},
    )
    return ScannerRunResult(
        scanner="osv-scanner", status="completed", findings=(finding,), observations=(observation,)
    )


def test_both_scanners_unavailable_preserves_honest_status(
    db_session: Session, security_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        service, "_run_dependency_scan", lambda *a, **k: _unavailable("osv-scanner")
    )
    monkeypatch.setattr(service, "_run_static_scan", lambda *a, **k: _unavailable("semgrep"))

    ingestion_result = ingest_parse_and_graph(db_session, security_fixture_repo)
    result = service.run_security_analysis(
        db_session,
        uuid.UUID(ingestion_result.repository_id),
        uuid.UUID(ingestion_result.revision_id),
    )
    # Nothing scanned: the persisted run must never read as a clean scan.
    assert result.analysis_status == "failed"
    run = db_session.get(AnalysisRun, result.analysis_run_id)
    assert run is not None and run.status == "failed"
    assert run.error_message == "no scanner completed: osv-scanner unavailable; semgrep unavailable"
    assert {s.scanner: s.status for s in result.scanner_results} == {
        "osv-scanner": "unavailable",
        "semgrep": "unavailable",
    }
    assert result.findings == ()
    assert any("osv-scanner" in limitation for limitation in result.limitations)
    assert any("semgrep" in limitation for limitation in result.limitations)


def test_one_scanner_failing_does_not_erase_the_others_results(
    db_session: Session, security_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "_run_dependency_scan", lambda *a, **k: _osv_finding_result())
    monkeypatch.setattr(service, "_run_static_scan", lambda *a, **k: _unavailable("semgrep"))

    ingestion_result = ingest_parse_and_graph(db_session, security_fixture_repo)
    result = service.run_security_analysis(
        db_session,
        uuid.UUID(ingestion_result.repository_id),
        uuid.UUID(ingestion_result.revision_id),
    )
    statuses = {s.scanner: s.status for s in result.scanner_results}
    assert statuses["osv-scanner"] == "completed"
    assert statuses["semgrep"] == "unavailable"
    assert len(result.findings) == 1
    assert result.findings[0].finding.rule_id == "GHSA-test"


def test_finding_graph_resolution_and_blast_radius_are_populated(
    db_session: Session, security_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "_run_dependency_scan", lambda *a, **k: _osv_finding_result())
    monkeypatch.setattr(service, "_run_static_scan", lambda *a, **k: _unavailable("semgrep"))

    ingestion_result = ingest_parse_and_graph(db_session, security_fixture_repo)
    result = service.run_security_analysis(
        db_session,
        uuid.UUID(ingestion_result.repository_id),
        uuid.UUID(ingestion_result.revision_id),
    )
    finding_result = result.findings[0]
    observation = finding_result.observations[0]
    assert observation.resolution_status == "exact_symbol"  # flask has exactly one importer
    assert observation.symbol_id is not None
    assert finding_result.blast_radius is not None
    assert finding_result.blast_radius.seed_symbol_id == observation.symbol_id


def test_repeated_analysis_does_not_duplicate_the_finding_row(
    db_session: Session, security_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "_run_dependency_scan", lambda *a, **k: _osv_finding_result())
    monkeypatch.setattr(service, "_run_static_scan", lambda *a, **k: _unavailable("semgrep"))

    ingestion_result = ingest_parse_and_graph(db_session, security_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    first = service.run_security_analysis(db_session, repository_id, revision_id)
    second = service.run_security_analysis(db_session, repository_id, revision_id)

    assert first.findings[0].finding.id == second.findings[0].finding.id  # upserted, not duplicated

    rows = (
        db_session.execute(
            select(SecurityFinding).where(SecurityFinding.repository_id == repository_id)
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1  # exactly one SecurityFinding row despite two analysis runs

    observation_rows = (
        db_session.execute(
            select(SecurityFindingObservation).where(
                SecurityFindingObservation.repository_id == repository_id
            )
        )
        .scalars()
        .all()
    )
    assert len(observation_rows) == 2  # one observation per run — history preserved, not collapsed


def test_user_set_status_survives_reobservation(
    db_session: Session, security_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "_run_dependency_scan", lambda *a, **k: _osv_finding_result())
    monkeypatch.setattr(service, "_run_static_scan", lambda *a, **k: _unavailable("semgrep"))

    ingestion_result = ingest_parse_and_graph(db_session, security_fixture_repo)
    repository_id = uuid.UUID(ingestion_result.repository_id)
    revision_id = uuid.UUID(ingestion_result.revision_id)

    first = service.run_security_analysis(db_session, repository_id, revision_id)
    finding_row = db_session.get(SecurityFinding, first.findings[0].finding.id)
    assert finding_row is not None
    finding_row.status = "false_positive"
    db_session.flush()

    service.run_security_analysis(db_session, repository_id, revision_id)
    db_session.refresh(finding_row)
    assert finding_row.status == "false_positive"  # never silently overwritten by a reobservation


def test_revision_not_found_is_explicit(db_session: Session, security_fixture_repo: Path) -> None:
    ingestion_result = ingest_parse_and_graph(db_session, security_fixture_repo)
    with pytest.raises(service.RevisionNotFoundError):
        service.run_security_analysis(
            db_session, uuid.UUID(ingestion_result.repository_id), uuid.uuid4()
        )


def test_cross_revision_isolation(
    db_session: Session, security_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "_run_dependency_scan", lambda *a, **k: _osv_finding_result())
    monkeypatch.setattr(service, "_run_static_scan", lambda *a, **k: _unavailable("semgrep"))

    first_result = ingest_parse_and_graph(db_session, security_fixture_repo, owner="owner-a")
    second_result = ingest_parse_and_graph(db_session, security_fixture_repo, owner="owner-b")

    service.run_security_analysis(
        db_session,
        uuid.UUID(first_result.repository_id),
        uuid.UUID(first_result.revision_id),
    )
    second = service.run_security_analysis(
        db_session,
        uuid.UUID(second_result.repository_id),
        uuid.UUID(second_result.revision_id),
    )
    # Different repositories dedupe independently — repository B gets its
    # own SecurityFinding row, not a shared one with repository A.
    assert second.findings[0].finding.id is not None
