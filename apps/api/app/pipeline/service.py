"""Repository-analysis pipeline (Product Integration v1): a thin, synchronous
orchestrator over the existing stage services

    parsing      app/parsing/service.py::run_parsing
    graph        app/graph/service.py::run_graph_construction
    rag_chunking app/rag/persistence.py::run_chunk_generation
    rag_embedding app/rag/embeddings.py::run_embedding_generation

It never parses, resolves, chunks or embeds anything itself. It decides
which stages to run (see app/pipeline/models.py::assess), runs them in
dependency order against exactly the requested revision, and reports what
happened. Every stage keeps its own AnalysisRun history and delete-and-
rebuild semantics.

Idempotency: a stage whose persisted output is current is reused (no new
AnalysisRun). A stage whose latest run failed or is outdated is re-run,
which appends a new AnalysisRun (these analyzers are append-only history;
the failed run is preserved, never overwritten). Concurrent requests for
the same revision are serialized by a PostgreSQL advisory lock; a second
request while one holds the lock is rejected rather than racing two
delete-and-rebuilds of the same rows.
"""

import logging
import uuid

from sqlalchemy.orm import Session

from app.ai.embedding_provider import DEFAULT_EMBEDDING_MODEL_VERSION, get_embedding_provider
from app.ai.models import AIConfigurationError, EmbeddingProvider
from app.core.config import Settings, get_settings
from app.db.revision_lock import RevisionBusyError, rebuild_in_progress, revision_rebuild_lock
from app.graph.service import ANALYZER_NAME as GRAPH_ANALYZER
from app.graph.service import GraphConstructionError, run_graph_construction
from app.models import AnalysisRun, Repository, RepositoryRevision
from app.models.rag import EMBEDDING_DIMENSION
from app.overview.service import latest_runs
from app.parsing.service import ANALYZER_NAME as PARSING_ANALYZER
from app.parsing.service import ParsingError, run_parsing
from app.pipeline.models import (
    ACTION_NOT_REQUESTED,
    ACTION_RAN,
    ACTION_REUSED,
    ACTION_SKIPPED,
    STAGES,
    UNAVAILABLE,
    PipelineResult,
    PipelineStatus,
    RunInfo,
    StageAssessment,
    StageResult,
    assess,
    overall_status,
)
from app.rag.embeddings import ANALYZER_NAME as EMBEDDER_ANALYZER
from app.rag.embeddings import EmbeddingGenerationError, run_embedding_generation
from app.rag.persistence import ANALYZER_NAME as CHUNKER_ANALYZER
from app.rag.persistence import ChunkGenerationError, run_chunk_generation

logger = logging.getLogger("devlens.pipeline")

_STAGE_RUN_KEYS = {
    "parsing": ("parsing", PARSING_ANALYZER),
    "graph": ("graph_build", GRAPH_ANALYZER),
    "rag_chunking": ("rag_indexing", CHUNKER_ANALYZER),
    "rag_embedding": ("rag_indexing", EMBEDDER_ANALYZER),
}
_PREREQUISITE = {"graph": "parsing", "rag_chunking": "parsing", "rag_embedding": "rag_chunking"}


class PipelineError(Exception):
    pass


class RepositoryNotFoundError(PipelineError):
    pass


class RevisionNotFoundError(PipelineError):
    pass


class RevisionConflictError(PipelineError):
    """The revision exists but belongs to another repository."""


class AnalysisInProgressError(PipelineError):
    """Another request currently holds this revision's analysis lock."""


def _resolve(db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID) -> RepositoryRevision:
    if db.get(Repository, repository_id) is None:
        raise RepositoryNotFoundError(f"no repository {repository_id}")
    revision = db.get(RepositoryRevision, revision_id)
    if revision is None:
        raise RevisionNotFoundError(f"no revision {revision_id}")
    if revision.repository_id != repository_id:
        raise RevisionConflictError(
            f"revision {revision_id} belongs to a different repository than {repository_id}"
        )
    return revision


# ---- Assessment ----


def _embedding_provider(
    settings: Settings,
) -> tuple[EmbeddingProvider | None, str | None]:
    """Provider or the reason it is unavailable. Building the provider makes
    no network call."""
    try:
        provider = get_embedding_provider(settings, expected_dimension=EMBEDDING_DIMENSION)
    except AIConfigurationError as exc:
        return None, f"embedding provider misconfigured: {exc}"
    if provider is None:
        return None, "no embedding provider is configured; lexical retrieval still works"
    return provider, None


def _latest_stage_runs(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> dict[str, RunInfo | None]:
    runs = latest_runs(db, repository_id, revision_id)

    def info(run: AnalysisRun | None) -> RunInfo | None:
        if run is None:
            return None
        return RunInfo(
            analysis_run_id=run.id,
            status=run.status,
            started_at=run.started_at,
            finished_at=run.finished_at,
            error_message=run.error_message,
            config=run.config,
        )

    return {stage: info(runs.get(key)) for stage, key in _STAGE_RUN_KEYS.items()}


def _assess(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID, settings: Settings
) -> dict[str, StageAssessment]:
    provider, reason = _embedding_provider(settings)
    return assess(
        _latest_stage_runs(db, repository_id, revision_id),
        embedding_available=provider is not None,
        embedding_unavailable_reason=reason,
        embedding_model=settings.embedding_model,
    )


def get_pipeline_status(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    settings: Settings | None = None,
) -> PipelineStatus:
    """Read-only: creates no AnalysisRun and changes nothing."""
    _resolve(db, repository_id, revision_id)
    assessed = _assess(db, repository_id, revision_id, settings or get_settings())
    return PipelineStatus(
        repository_id=repository_id,
        revision_id=revision_id,
        stages=tuple(assessed[stage] for stage in STAGES),
        analysis_in_progress=rebuild_in_progress(db, revision_id),
    )


# ---- Execution ----


def _run_stage(
    db: Session,
    stage: str,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    settings: Settings,
    provider: EmbeddingProvider | None,
) -> str | None:
    """Runs one stage through its own service. Returns an error message on
    failure (the stage service has already recorded its run as failed)."""
    try:
        if stage == "parsing":
            run_parsing(db, repository_id, revision_id)
        elif stage == "graph":
            run_graph_construction(db, repository_id, revision_id)
        elif stage == "rag_chunking":
            run_chunk_generation(db, repository_id, revision_id)
        else:
            assert provider is not None
            run_embedding_generation(
                db,
                provider,
                settings.embedding_provider,
                settings.embedding_model,
                DEFAULT_EMBEDDING_MODEL_VERSION,
                EMBEDDING_DIMENSION,
                repository_id,
                revision_id,
            )
    except (
        ParsingError,
        GraphConstructionError,
        ChunkGenerationError,
        EmbeddingGenerationError,
    ) as exc:
        # The stage service already recorded its AnalysisRun as failed.
        logger.warning("pipeline stage %s failed for %s: %s", stage, revision_id, exc)
        return str(exc)[:2000]
    except Exception as exc:
        # Escaped the stage service's own handling (e.g. before its run row
        # existed). Reset the session and report the stage as failed in this
        # request; never let it read as success.
        logger.exception("pipeline stage %s raised unexpectedly for %s", stage, revision_id)
        db.rollback()
        return f"unexpected error: {exc}"[:2000]
    return None


def _execute_stages(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    requested: dict[str, bool],
    force: bool,
    settings: Settings,
    provider: EmbeddingProvider | None,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Runs inside the revision rebuild lock. Returns (action per stage,
    error per stage that ran and failed, reason per skipped stage)."""
    actions: dict[str, str] = {}
    errors: dict[str, str] = {}
    notes: dict[str, str] = {}
    rebuilt: set[str] = set()
    for stage in STAGES:
        if not requested[stage]:
            actions[stage] = ACTION_NOT_REQUESTED
            continue
        current = _assess(db, repository_id, revision_id, settings)
        prerequisite = _PREREQUISITE.get(stage)
        if prerequisite is not None and not current[prerequisite].up_to_date:
            actions[stage] = ACTION_SKIPPED
            notes[stage] = f"skipped: {prerequisite} is not up to date for this revision"
            continue
        if current[stage].status == UNAVAILABLE:
            actions[stage] = ACTION_SKIPPED
            continue
        upstream_rebuilt = prerequisite in rebuilt if prerequisite else False
        if current[stage].up_to_date and not force and not upstream_rebuilt:
            actions[stage] = ACTION_REUSED
            continue
        error = _run_stage(db, stage, repository_id, revision_id, settings, provider)
        actions[stage] = ACTION_RAN
        if error is None:
            rebuilt.add(stage)
        else:
            errors[stage] = error
    return actions, errors, notes


def analyze_revision(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    *,
    run_parsing_stage: bool = True,
    run_graph_stage: bool = True,
    run_rag_stage: bool = True,
    force: bool = False,
    settings: Settings | None = None,
) -> PipelineResult:
    """Brings the requested stages of exactly `revision_id` up to date.
    Stages run in order parsing -> graph -> rag_chunking -> rag_embedding;
    a stage runs only if requested and (forced, or not current, or an
    upstream stage was rebuilt in this request). A stage whose prerequisite
    is not current after its upstream step is skipped, never run on stale
    input."""
    settings = settings or get_settings()
    _resolve(db, repository_id, revision_id)
    requested = {
        "parsing": run_parsing_stage,
        "graph": run_graph_stage,
        "rag_chunking": run_rag_stage,
        "rag_embedding": run_rag_stage,
    }
    provider, _ = _embedding_provider(settings)

    try:
        with revision_rebuild_lock(db, revision_id):
            actions, errors, notes = _execute_stages(
                db, repository_id, revision_id, requested, force, settings, provider
            )
    except RevisionBusyError as exc:
        # Raised only on entry: nothing ran.
        raise AnalysisInProgressError(str(exc)) from exc

    final = _assess(db, repository_id, revision_id, settings)
    stages = tuple(
        StageResult(
            stage=stage,
            requested=requested[stage],
            action=actions[stage],
            status=final[stage].status,
            up_to_date=final[stage].up_to_date,
            analysis_run_id=final[stage].analysis_run_id,
            started_at=final[stage].started_at,
            finished_at=final[stage].finished_at,
            detail=errors.get(stage) or notes.get(stage) or final[stage].detail,
            error=errors.get(stage),
        )
        for stage in STAGES
    )
    return PipelineResult(
        repository_id=repository_id,
        revision_id=revision_id,
        overall_status=overall_status(stages),
        stages=stages,
    )
