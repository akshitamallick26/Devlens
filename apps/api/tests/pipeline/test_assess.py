"""Pure tests for app/pipeline/models.py: stage current-ness and overall
status. No DB, no provider."""

import uuid
from datetime import UTC, datetime, timedelta

from app.pipeline.models import (
    ACTION_RAN,
    ACTION_REUSED,
    ACTION_SKIPPED,
    NOT_RUN,
    OVERALL_COMPLETED,
    OVERALL_FAILED,
    OVERALL_PARTIAL,
    UNAVAILABLE,
    RunInfo,
    StageResult,
    assess,
    overall_status,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
MODEL = "text-embedding-3-small"


def run(
    start_minutes: int, status: str = "completed", config: dict[str, object] | None = None
) -> RunInfo:
    start = T0 + timedelta(minutes=start_minutes)
    return RunInfo(
        analysis_run_id=uuid.uuid4(),
        status=status,
        started_at=start,
        finished_at=start + timedelta(seconds=30) if status != "running" else None,
        error_message="boom" if status == "failed" else None,
        config=config,
    )


def _assess(latest: dict[str, RunInfo | None], available: bool = True) -> dict[str, object]:
    return assess(  # type: ignore[return-value]
        latest,
        embedding_available=available,
        embedding_unavailable_reason=None if available else "no embedding provider is configured",
        embedding_model=MODEL,
    )


def test_nothing_run_is_not_run_everywhere() -> None:
    result = _assess({})
    for stage in ("parsing", "graph", "rag_chunking", "rag_embedding"):
        assert result[stage].status == NOT_RUN  # type: ignore[attr-defined]
        assert result[stage].up_to_date is False  # type: ignore[attr-defined]


def test_full_chain_in_order_is_current() -> None:
    result = assess(
        {
            "parsing": run(0),
            "graph": run(1),
            "rag_chunking": run(2),
            "rag_embedding": run(3, config={"model_name": MODEL}),
        },
        embedding_available=True,
        embedding_unavailable_reason=None,
        embedding_model=MODEL,
    )
    assert all(a.up_to_date for a in result.values())


def test_reparse_after_graph_makes_graph_and_chunks_outdated() -> None:
    result = assess(
        {"parsing": run(10), "graph": run(1), "rag_chunking": run(2)},
        embedding_available=True,
        embedding_unavailable_reason=None,
        embedding_model=MODEL,
    )
    assert result["parsing"].up_to_date
    assert result["graph"].status == "completed" and not result["graph"].up_to_date
    assert result["graph"].detail is not None and "outdated" in result["graph"].detail
    assert not result["rag_chunking"].up_to_date


def test_failed_latest_parse_invalidates_everything_downstream() -> None:
    result = assess(
        {"parsing": run(10, status="failed"), "graph": run(11)},
        embedding_available=True,
        embedding_unavailable_reason=None,
        embedding_model=MODEL,
    )
    assert result["parsing"].status == "failed"
    assert result["parsing"].detail == "boom"
    assert not result["graph"].up_to_date


def test_embedding_unavailable_is_reported_never_complete() -> None:
    result = assess(
        {
            "parsing": run(0),
            "rag_chunking": run(1),
            "rag_embedding": run(2, config={"model_name": MODEL}),
        },
        embedding_available=False,
        embedding_unavailable_reason="no embedding provider is configured",
        embedding_model=MODEL,
    )
    assert result["rag_embedding"].status == UNAVAILABLE
    assert result["rag_embedding"].up_to_date is False
    assert result["rag_chunking"].up_to_date


def test_embeddings_from_another_model_are_not_current() -> None:
    result = assess(
        {
            "parsing": run(0),
            "rag_chunking": run(1),
            "rag_embedding": run(2, config={"model_name": "other-model"}),
        },
        embedding_available=True,
        embedding_unavailable_reason=None,
        embedding_model=MODEL,
    )
    assert not result["rag_embedding"].up_to_date
    assert (
        result["rag_embedding"].detail
        == "embedded with a different model than the one configured now"
    )


def test_running_stage_is_running_and_not_current() -> None:
    result = assess(
        {"parsing": run(0, status="running")},
        embedding_available=True,
        embedding_unavailable_reason=None,
        embedding_model=MODEL,
    )
    assert result["parsing"].status == "running"
    assert not result["parsing"].up_to_date


def _stage(stage: str, requested: bool, up_to_date: bool, action: str) -> StageResult:
    return StageResult(
        stage=stage,
        requested=requested,
        action=action,
        status="completed" if up_to_date else "failed",
        up_to_date=up_to_date,
        analysis_run_id=None,
        started_at=None,
        finished_at=None,
        detail=None,
    )


def test_overall_status() -> None:
    assert overall_status((_stage("parsing", True, True, ACTION_REUSED),)) == OVERALL_COMPLETED
    assert (
        overall_status(
            (_stage("parsing", True, True, ACTION_RAN), _stage("graph", True, False, ACTION_RAN))
        )
        == OVERALL_PARTIAL
    )
    assert (
        overall_status(
            (
                _stage("parsing", True, False, ACTION_RAN),
                _stage("graph", True, False, ACTION_SKIPPED),
            )
        )
        == OVERALL_FAILED
    )
    # A stage that was not requested never affects the outcome.
    assert (
        overall_status(
            (
                _stage("parsing", True, True, ACTION_RAN),
                _stage("graph", False, False, "not_requested"),
            )
        )
        == OVERALL_COMPLETED
    )


def test_a_failure_in_this_request_is_never_completed_even_if_an_old_run_looks_current() -> None:
    stage = StageResult(
        stage="parsing",
        requested=True,
        action=ACTION_RAN,
        status="completed",
        up_to_date=True,
        analysis_run_id=None,
        started_at=None,
        finished_at=None,
        detail="unexpected error: boom",
        error="unexpected error: boom",
    )
    assert overall_status((stage,)) == OVERALL_FAILED
