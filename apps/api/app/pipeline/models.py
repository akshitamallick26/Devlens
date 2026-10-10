"""Pure models and decision rules for the repository-analysis pipeline
(Product Integration v1). No DB, no I/O: given the latest AnalysisRun of
each stage, decide whether that stage's persisted output is current. See
docs/architecture/analysis-pipeline.md.

Why "current" needs more than "latest run completed": every stage is a
revision-scoped delete-and-rebuild, and downstream output is destroyed or
invalidated by an upstream rebuild:

- re-parsing deletes the revision's Symbol rows, which CASCADE-deletes its
  GraphEdge rows and SET NULLs CodeChunk.symbol_id;
- re-chunking deletes CodeChunk rows, which CASCADE-deletes Embeddings.

So a downstream stage is current only if it completed *after* the upstream
run it depends on finished.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

STAGES = ("parsing", "graph", "rag_chunking", "rag_embedding")

# Stage status vocabulary. AnalysisRun.status values pass through
# unchanged; "not_run" means no run exists; "unavailable" means the stage
# cannot run in this deployment (e.g. no embedding provider configured)
# and is never recorded as an AnalysisRun.
NOT_RUN = "not_run"
UNAVAILABLE = "unavailable"

# What the pipeline did with a stage in one request.
ACTION_RAN = "ran"
ACTION_REUSED = "reused"
ACTION_SKIPPED = "skipped"  # a prerequisite is not current, or the stage is unavailable
ACTION_NOT_REQUESTED = "not_requested"

OVERALL_COMPLETED = "completed"  # every requested stage is current
OVERALL_PARTIAL = "partial"  # some requested stage failed, was skipped, or is unavailable
OVERALL_FAILED = "failed"  # nothing requested ended current


@dataclass(frozen=True)
class RunInfo:
    analysis_run_id: uuid.UUID
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    config: dict[str, object] | None = None


@dataclass(frozen=True)
class StageAssessment:
    stage: str
    status: str  # AnalysisRun.status of the latest run, NOT_RUN, or UNAVAILABLE
    up_to_date: bool
    analysis_run_id: uuid.UUID | None
    started_at: datetime | None
    finished_at: datetime | None
    detail: str | None


def _completed_after(run: RunInfo | None, upstream: RunInfo | None) -> bool:
    """`run` completed and started no earlier than `upstream` finished."""
    if run is None or run.status != "completed" or run.started_at is None:
        return False
    if upstream is None or upstream.finished_at is None:
        return False
    return run.started_at >= upstream.finished_at


def assess(
    latest: dict[str, RunInfo | None],
    *,
    embedding_available: bool,
    embedding_unavailable_reason: str | None,
    embedding_model: str | None,
) -> dict[str, StageAssessment]:
    """Current-ness of every stage from its latest run (any status).

    parsing        current iff its latest run completed.
    graph          current iff completed after the latest parsing run finished.
    rag_chunking   current iff completed after the latest parsing run finished.
    rag_embedding  current iff completed after the latest chunking run finished
                   and with the currently configured embedding model.
    """
    parsing = latest.get("parsing")
    graph = latest.get("graph")
    chunking = latest.get("rag_chunking")
    embedding = latest.get("rag_embedding")

    parsing_ok = parsing is not None and parsing.status == "completed"
    graph_ok = parsing_ok and _completed_after(graph, parsing)
    chunking_ok = parsing_ok and _completed_after(chunking, parsing)
    embedding_model_ok = (
        embedding is not None
        and embedding.config is not None
        and embedding.config.get("model_name") == embedding_model
    )
    embedding_ok = (
        embedding_available
        and chunking_ok
        and embedding_model_ok
        and _completed_after(embedding, chunking)
    )

    def detail(stage: str, run: RunInfo | None, ok: bool) -> str | None:
        if run is None:
            return None
        if run.status == "failed":
            return run.error_message
        if run.status == "completed" and not ok:
            if stage == "rag_embedding" and not embedding_model_ok:
                return "embedded with a different model than the one configured now"
            return "outdated: an upstream stage was rebuilt after this run"
        return None

    def build(stage: str, run: RunInfo | None, ok: bool) -> StageAssessment:
        return StageAssessment(
            stage=stage,
            status=run.status if run is not None else NOT_RUN,
            up_to_date=ok,
            analysis_run_id=run.analysis_run_id if run is not None else None,
            started_at=run.started_at if run is not None else None,
            finished_at=run.finished_at if run is not None else None,
            detail=detail(stage, run, ok),
        )

    result = {
        "parsing": build("parsing", parsing, parsing_ok),
        "graph": build("graph", graph, graph_ok),
        "rag_chunking": build("rag_chunking", chunking, chunking_ok),
    }
    if not embedding_available:
        result["rag_embedding"] = StageAssessment(
            stage="rag_embedding",
            status=UNAVAILABLE,
            up_to_date=False,
            analysis_run_id=embedding.analysis_run_id if embedding is not None else None,
            started_at=embedding.started_at if embedding is not None else None,
            finished_at=embedding.finished_at if embedding is not None else None,
            detail=embedding_unavailable_reason,
        )
    else:
        result["rag_embedding"] = build("rag_embedding", embedding, embedding_ok)
    return result


@dataclass(frozen=True)
class StageResult:
    stage: str
    requested: bool
    action: str  # ACTION_*
    status: str
    up_to_date: bool
    analysis_run_id: uuid.UUID | None
    started_at: datetime | None
    finished_at: datetime | None
    detail: str | None
    # Set when the stage ran in *this* request and failed. A failure can
    # leave an older run looking current (e.g. an error before the stage
    # service recorded its run), so success is never inferred from
    # up_to_date alone.
    error: str | None = None


@dataclass(frozen=True)
class PipelineResult:
    repository_id: uuid.UUID
    revision_id: uuid.UUID
    overall_status: str
    stages: tuple[StageResult, ...]
    # The pipeline runs inside the HTTP request; there is no background job.
    execution: str = "synchronous"


def overall_status(stages: tuple[StageResult, ...]) -> str:
    requested = [s for s in stages if s.requested]
    succeeded = [s.up_to_date and s.error is None for s in requested]
    if all(succeeded):
        return OVERALL_COMPLETED
    if any(succeeded):
        return OVERALL_PARTIAL
    return OVERALL_FAILED


@dataclass(frozen=True)
class PipelineStatus:
    repository_id: uuid.UUID
    revision_id: uuid.UUID
    stages: tuple[StageAssessment, ...]
    # True while another request holds this revision's analysis lock.
    analysis_in_progress: bool
