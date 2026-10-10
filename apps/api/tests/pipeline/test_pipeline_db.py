"""DB tests for app/pipeline/service.py against a real git fixture repository
ingested through the real ingestion service. Requires live PostgreSQL (same
`db_session` fixture and explicit skip as every other DB suite). No
embedding provider is configured, so the embedding stage must report
"unavailable" and never "completed"."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.revision_lock import revision_rebuild_lock
from app.graph.service import GraphConstructionError
from app.models import AnalysisRun, CodeChunk, GraphEdge, Symbol
from app.pipeline import service
from app.pipeline.models import ACTION_RAN, ACTION_REUSED, ACTION_SKIPPED, UNAVAILABLE
from tests.automation.conftest import ingest_revision, modify_and_commit
from tests.conftest import make_repo_and_revision

NO_EMBEDDINGS = get_settings().model_copy(update={"embedding_api_key": None})


def _ids(db: Session, repo: Path, owner: str = "pipeline") -> tuple[uuid.UUID, uuid.UUID]:
    result = ingest_revision(db, repo, owner=owner)
    return uuid.UUID(result.repository_id), uuid.UUID(result.revision_id)


def _run_count(db: Session, revision_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count()).select_from(AnalysisRun).where(AnalysisRun.revision_id == revision_id)
    ).scalar_one()


def _by_stage(result: service.PipelineResult) -> dict[str, object]:
    return {s.stage: s for s in result.stages}


def test_full_pipeline_builds_every_available_stage(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    repository_id, revision_id = _ids(db_session, automation_fixture_repo)
    result = service.analyze_revision(
        db_session, repository_id, revision_id, settings=NO_EMBEDDINGS
    )
    stages = _by_stage(result)

    for stage in ("parsing", "graph", "rag_chunking"):
        assert stages[stage].action == ACTION_RAN  # type: ignore[attr-defined]
        assert stages[stage].status == "completed"  # type: ignore[attr-defined]
        assert stages[stage].up_to_date  # type: ignore[attr-defined]
    embedding = stages["rag_embedding"]
    assert embedding.action == ACTION_SKIPPED  # type: ignore[attr-defined]
    assert embedding.status == UNAVAILABLE  # type: ignore[attr-defined]
    assert result.overall_status == "partial"
    assert result.execution == "synchronous"

    def count(model: type) -> int:
        return db_session.execute(
            select(func.count()).select_from(model).where(model.revision_id == revision_id)  # type: ignore[attr-defined]
        ).scalar_one()

    assert count(Symbol) > 0
    assert count(GraphEdge) > 0
    assert count(CodeChunk) > 0


def test_repeat_analysis_reuses_current_stages_without_new_runs(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    repository_id, revision_id = _ids(db_session, automation_fixture_repo)
    first = service.analyze_revision(db_session, repository_id, revision_id, settings=NO_EMBEDDINGS)
    runs_after_first = _run_count(db_session, revision_id)

    second = service.analyze_revision(
        db_session, repository_id, revision_id, settings=NO_EMBEDDINGS
    )
    assert _run_count(db_session, revision_id) == runs_after_first
    for stage in ("parsing", "graph", "rag_chunking"):
        assert _by_stage(second)[stage].action == ACTION_REUSED  # type: ignore[attr-defined]
        assert (
            _by_stage(second)[stage].analysis_run_id  # type: ignore[attr-defined]
            == _by_stage(first)[stage].analysis_run_id  # type: ignore[attr-defined]
        )


def test_force_rebuilds_and_appends_runs(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    repository_id, revision_id = _ids(db_session, automation_fixture_repo)
    service.analyze_revision(db_session, repository_id, revision_id, settings=NO_EMBEDDINGS)
    before = _run_count(db_session, revision_id)
    forced = service.analyze_revision(
        db_session, repository_id, revision_id, force=True, settings=NO_EMBEDDINGS
    )
    assert _run_count(db_session, revision_id) == before + 3
    assert all(
        _by_stage(forced)[s].up_to_date
        for s in ("parsing", "graph", "rag_chunking")  # type: ignore[attr-defined]
    )


def test_reparse_alone_forces_graph_and_chunks_to_rebuild(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    repository_id, revision_id = _ids(db_session, automation_fixture_repo)
    service.analyze_revision(db_session, repository_id, revision_id, settings=NO_EMBEDDINGS)
    service.analyze_revision(
        db_session,
        repository_id,
        revision_id,
        run_graph_stage=False,
        run_rag_stage=False,
        force=True,
        settings=NO_EMBEDDINGS,
    )
    status = service.get_pipeline_status(db_session, repository_id, revision_id, NO_EMBEDDINGS)
    by_stage = {s.stage: s for s in status.stages}
    assert by_stage["parsing"].up_to_date
    assert not by_stage["graph"].up_to_date  # graph predates the latest parse
    assert not by_stage["rag_chunking"].up_to_date

    rerun = service.analyze_revision(db_session, repository_id, revision_id, settings=NO_EMBEDDINGS)
    assert _by_stage(rerun)["parsing"].action == ACTION_REUSED  # type: ignore[attr-defined]
    assert _by_stage(rerun)["graph"].action == ACTION_RAN  # type: ignore[attr-defined]
    assert _by_stage(rerun)["rag_chunking"].action == ACTION_RAN  # type: ignore[attr-defined]


def test_analysis_is_revision_isolated(db_session: Session, automation_fixture_repo: Path) -> None:
    repository_id, r1 = _ids(db_session, automation_fixture_repo)
    modify_and_commit(
        automation_fixture_repo, "app/extra.py", "def extra():\n    return 2\n", "second"
    )
    _, r2 = _ids(db_session, automation_fixture_repo)
    assert r1 != r2

    service.analyze_revision(db_session, repository_id, r2, settings=NO_EMBEDDINGS)
    assert _run_count(db_session, r1) == 1  # only R1's own ingestion run
    r1_symbols = db_session.execute(
        select(func.count()).select_from(Symbol).where(Symbol.revision_id == r1)
    ).scalar_one()
    assert r1_symbols == 0
    status_r1 = service.get_pipeline_status(db_session, repository_id, r1, NO_EMBEDDINGS)
    assert all(s.status in ("not_run", UNAVAILABLE) for s in status_r1.stages)


def test_graph_failure_yields_partial_and_skips_nothing_unrelated(
    db_session: Session, automation_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_id, revision_id = _ids(db_session, automation_fixture_repo)

    def broken_graph(*args: object, **kwargs: object) -> None:
        raise GraphConstructionError("simulated graph failure")

    monkeypatch.setattr(service, "run_graph_construction", broken_graph)
    result = service.analyze_revision(
        db_session, repository_id, revision_id, settings=NO_EMBEDDINGS
    )
    stages = _by_stage(result)
    assert stages["parsing"].up_to_date  # type: ignore[attr-defined]
    assert stages["graph"].action == ACTION_RAN  # type: ignore[attr-defined]
    assert not stages["graph"].up_to_date  # type: ignore[attr-defined]
    assert stages["graph"].detail == "simulated graph failure"  # type: ignore[attr-defined]
    # Chunking depends on parsing only, so it still runs.
    assert stages["rag_chunking"].up_to_date  # type: ignore[attr-defined]
    assert result.overall_status == "partial"


def test_repository_and_revision_ownership_are_enforced(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    repository_id, revision_id = _ids(db_session, automation_fixture_repo)
    _, other_revision = make_repo_and_revision(db_session, "someone-else")
    with pytest.raises(service.RevisionConflictError):
        service.analyze_revision(
            db_session, repository_id, other_revision.id, settings=NO_EMBEDDINGS
        )
    with pytest.raises(service.RevisionNotFoundError):
        service.analyze_revision(db_session, repository_id, uuid.uuid4(), settings=NO_EMBEDDINGS)
    with pytest.raises(service.RepositoryNotFoundError):
        service.analyze_revision(db_session, uuid.uuid4(), revision_id, settings=NO_EMBEDDINGS)


def test_concurrent_analysis_of_one_revision_is_rejected(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    repository_id, revision_id = _ids(db_session, automation_fixture_repo)
    with revision_rebuild_lock(db_session, revision_id):
        assert service.get_pipeline_status(
            db_session, repository_id, revision_id, NO_EMBEDDINGS
        ).analysis_in_progress
        with pytest.raises(service.AnalysisInProgressError):
            service.analyze_revision(db_session, repository_id, revision_id, settings=NO_EMBEDDINGS)
    assert not service.get_pipeline_status(
        db_session, repository_id, revision_id, NO_EMBEDDINGS
    ).analysis_in_progress


def test_status_endpoint_is_read_only(db_session: Session, automation_fixture_repo: Path) -> None:
    repository_id, revision_id = _ids(db_session, automation_fixture_repo)
    before = _run_count(db_session, revision_id)
    service.get_pipeline_status(db_session, repository_id, revision_id, NO_EMBEDDINGS)
    assert _run_count(db_session, revision_id) == before


def test_failed_stage_is_rerun_as_a_new_run_on_the_next_request(
    db_session: Session, automation_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_id, revision_id = _ids(db_session, automation_fixture_repo)
    real_graph = service.run_graph_construction

    def broken_graph(*args: object, **kwargs: object) -> None:
        raise GraphConstructionError("simulated graph failure")

    monkeypatch.setattr(service, "run_graph_construction", broken_graph)
    service.analyze_revision(db_session, repository_id, revision_id, settings=NO_EMBEDDINGS)
    monkeypatch.setattr(service, "run_graph_construction", real_graph)

    before = _run_count(db_session, revision_id)
    retry = service.analyze_revision(db_session, repository_id, revision_id, settings=NO_EMBEDDINGS)
    graph = _by_stage(retry)["graph"]
    assert graph.action == ACTION_RAN  # type: ignore[attr-defined]
    assert graph.up_to_date  # type: ignore[attr-defined]
    assert _by_stage(retry)["parsing"].action == ACTION_REUSED  # type: ignore[attr-defined]
    assert _run_count(db_session, revision_id) == before + 1  # only the graph run


def test_unexpected_stage_exception_is_reported_as_failure_not_success(
    db_session: Session, automation_fixture_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository_id, revision_id = _ids(db_session, automation_fixture_repo)

    def exploding_chunker(*args: object, **kwargs: object) -> None:
        raise KeyError("not a ChunkGenerationError")

    monkeypatch.setattr(service, "run_chunk_generation", exploding_chunker)
    result = service.analyze_revision(
        db_session, repository_id, revision_id, settings=NO_EMBEDDINGS
    )
    chunking = _by_stage(result)["rag_chunking"]
    assert chunking.error is not None and "unexpected error" in chunking.error  # type: ignore[attr-defined]
    assert result.overall_status == "partial"
    stuck = db_session.execute(
        select(func.count())
        .select_from(AnalysisRun)
        .where(AnalysisRun.revision_id == revision_id)
        .where(AnalysisRun.status == "running")
    ).scalar_one()
    assert stuck == 0


def test_automation_and_pipeline_share_the_rebuild_lock(
    db_session: Session, automation_fixture_repo: Path
) -> None:
    from app.automation.service import run_automation_analysis
    from app.db.revision_lock import RevisionBusyError

    repository_id, revision_id = _ids(db_session, automation_fixture_repo)
    before = _run_count(db_session, revision_id)
    with revision_rebuild_lock(db_session, revision_id):
        with pytest.raises(RevisionBusyError):
            run_automation_analysis(db_session, repository_id, revision_id)
    assert _run_count(db_session, revision_id) == before  # no automation run row created
