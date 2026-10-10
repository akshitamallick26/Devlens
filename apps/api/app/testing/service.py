"""Test-generation orchestration: `generate_tests_for_target`. Pipeline —

    target discovery -> deterministic context -> LLM generation
    -> strict output validation -> persist GeneratedTest
    -> optional execution -> persist TestExecution

Deterministic stages (discovery, context, validation) work identically with
no LLM configured — they simply never run past the point where generation
would start; nothing fakes a GeneratedTest in that case. No autonomous
retry loop (Task 15): each target is generated at most once, validated at
most once, executed at most once per call. Regeneration is a future,
explicit, separately-invoked operation — not built here.
"""

import logging
import platform
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.models import AIProvider
from app.models import AnalysisRun, RepositoryRevision, SourceBlob, SourceFile
from app.rag.persistence import load_chunk_texts
from app.testing import generation, persistence, sandbox, validation
from app.testing.context import ConfigurationFile, assemble_test_context
from app.testing.discovery import discover_test_targets
from app.testing.models import ExecutionResult, TestTarget, ValidationIssue

logger = logging.getLogger("devlens.testing")

ANALYZER_NAME = "devlens-test-generation"
MAX_TESTS_PER_REQUEST = 5

# A root-level pytest configuration file, if one exists in the revision —
# deterministically looked up by exact, fixed candidate paths, never a
# repository-wide filesystem walk.
_CONFIGURATION_CANDIDATES = ("conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg")


class TestGenerationError(Exception):
    pass


class RevisionNotFoundError(TestGenerationError):
    pass


class ProviderIdentityError(TestGenerationError):
    """The provider does not identify itself (empty provider or model
    name). Raised before anything is persisted: a GeneratedTest whose
    origin cannot be recorded is never written."""


@dataclass(frozen=True)
class TargetGenerationOutcome:
    target: TestTarget
    # no_provider | generation_failed | validation_failed | ready | passed
    # | failed | execution_failed
    status: str
    generated_test_id: uuid.UUID | None
    execution_id: uuid.UUID | None
    validation_issues: tuple[ValidationIssue, ...]
    execution: ExecutionResult | None
    error: str | None


@dataclass(frozen=True)
class TestGenerationRunResult:
    __test__ = False  # not a test class — see app/testing/models.py::TestTarget

    repository_id: uuid.UUID
    revision_id: uuid.UUID
    analysis_run_id: uuid.UUID | None
    analysis_status: str  # no_targets | no_provider | completed
    outcomes: tuple[TargetGenerationOutcome, ...]


def _analyzer_version() -> str:
    return f"test-generation@{platform.python_version()}"


def _load_configuration(
    db: Session, repository_id: uuid.UUID, revision_id: uuid.UUID
) -> tuple[ConfigurationFile, ...]:
    rows = db.execute(
        select(SourceFile.path, SourceBlob.inline_content)
        .join(SourceBlob, SourceFile.content_hash == SourceBlob.content_hash)
        .where(SourceFile.repository_id == repository_id)
        .where(SourceFile.revision_id == revision_id)
        .where(SourceFile.path.in_(_CONFIGURATION_CANDIDATES))
    ).all()
    by_path = dict(rows)
    return tuple(
        ConfigurationFile(file_path=path, content=by_path[path])
        for path in _CONFIGURATION_CANDIDATES
        if path in by_path
    )


def _load_full_file_contents(
    db: Session, revision_id: uuid.UUID, file_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str, str]]:
    """Full (not bounded-snippet) file content for a small, explicit set of
    file ids — used only to materialize the files a generated test actually
    needs to import at execution time. Bounded by `file_ids` itself, never
    the whole revision."""
    if not file_ids:
        return {}
    rows = db.execute(
        select(SourceFile.id, SourceFile.path, SourceBlob.inline_content)
        .join(SourceBlob, SourceFile.content_hash == SourceBlob.content_hash)
        .where(SourceFile.revision_id == revision_id)
        .where(SourceFile.id.in_(file_ids))
    ).all()
    return {file_id: (path, content) for file_id, path, content in rows if content is not None}


def _generate_one(
    db: Session,
    llm_provider: AIProvider,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    target: TestTarget,
    existing_file_paths: frozenset[str],
    configuration: tuple[ConfigurationFile, ...],
    *,
    execute: bool,
    provider_name: str,
    model_name: str,
    execution_timeout_seconds: float,
    max_output_bytes: int,
    analysis_run_id: uuid.UUID,
) -> TargetGenerationOutcome:
    # Existing tests are deliberately included as metadata only (file path,
    # test identifier, relevance reason) — ExistingTestRef carries no line
    # span, only a symbol id, and resolving one would mean a second lookup
    # against Symbol per existing test. Not worth the extra query for v1:
    # knowing a related test exists and why is useful context even without
    # its body, and the LLM never claims to have read it (context.py marks
    # its content None, which is always rendered as "not available").
    spans: list[tuple[uuid.UUID, int, int]] = [(target.file_id, target.start_line, target.end_line)]
    spans.extend((c.file_id, c.start_line, c.end_line) for c in target.callers)
    spans.extend((c.file_id, c.start_line, c.end_line) for c in target.callees)
    texts = load_chunk_texts(db, revision_id, spans)
    index = 0
    target_source = texts[index]
    index += 1
    caller_sources: dict[uuid.UUID, str | None] = {}
    for caller in target.callers:
        caller_sources[caller.symbol_id] = texts[index]
        index += 1
    callee_sources: dict[uuid.UUID, str | None] = {}
    for callee in target.callees:
        callee_sources[callee.symbol_id] = texts[index]
        index += 1
    existing_test_sources: dict[uuid.UUID, str | None] = {
        et.symbol_id: None for et in target.existing_tests
    }

    context = assemble_test_context(
        repository_id,
        revision_id,
        target,
        target_source,
        caller_sources,
        callee_sources,
        existing_test_sources,
        configuration,
    )

    try:
        output = generation.generate_test(llm_provider, target, context)
    except Exception as exc:  # noqa: BLE001 - provider call, see app/rag/qa.py's identical seam
        logger.warning("test generation failed for %s: %s", target.qualified_name, exc)
        return TargetGenerationOutcome(
            target=target,
            status="generation_failed",
            generated_test_id=None,
            execution_id=None,
            validation_issues=(),
            execution=None,
            error=str(exc),
        )

    generated_test = persistence.persist_generated_test(
        db,
        repository_id,
        revision_id,
        target.target_symbol_id,
        output,
        provider_name,
        model_name,
        None,  # AIProvider reports no model version; unknown, not ""
        analysis_run_id,
    )

    result = validation.validate_generated_test(
        output, target, context.evidence, existing_file_paths
    )
    persistence.record_validation(generated_test, result)
    db.flush()

    if not result.passed:
        return TargetGenerationOutcome(
            target=target,
            status="validation_failed",
            generated_test_id=generated_test.id,
            execution_id=None,
            validation_issues=result.issues,
            execution=None,
            error=None,
        )

    if not execute:
        return TargetGenerationOutcome(
            target=target,
            status="ready",
            generated_test_id=generated_test.id,
            execution_id=None,
            validation_issues=(),
            execution=None,
            error=None,
        )

    needed_file_ids = {target.file_id} | {c.file_id for c in target.callees}
    full_files = _load_full_file_contents(db, revision_id, needed_file_ids)
    context_files = [
        sandbox.MaterializedFile(relative_path=path, content=content)
        for path, content in full_files.values()
    ]
    test_file = sandbox.MaterializedFile(relative_path=output.file_path, content=output.source)

    started_at = datetime.now(UTC)
    execution = sandbox.run_generated_test(
        context_files,
        test_file,
        timeout_seconds=execution_timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    test_execution = persistence.record_execution(
        db, generated_test, repository_id, revision_id, execution, analysis_run_id, started_at
    )
    db.flush()

    return TargetGenerationOutcome(
        target=target,
        status=generated_test.status,
        generated_test_id=generated_test.id,
        execution_id=test_execution.id,
        validation_issues=(),
        execution=execution,
        error=None,
    )


def generate_tests_for_target(
    db: Session,
    llm_provider: AIProvider | None,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    *,
    target_symbol_id: uuid.UUID | None = None,
    impacted_symbol_ids: set[uuid.UUID] | None = None,
    impacted_file_ids: set[uuid.UUID] | None = None,
    impact_depths: dict[uuid.UUID, int] | None = None,
    execute: bool = False,
    max_tests: int = MAX_TESTS_PER_REQUEST,
    execution_timeout_seconds: float = sandbox.DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = sandbox.DEFAULT_MAX_OUTPUT_BYTES,
) -> TestGenerationRunResult:
    revision = db.get(RepositoryRevision, revision_id)
    if revision is None or revision.repository_id != repository_id:
        raise RevisionNotFoundError(f"no revision {revision_id} for repository {repository_id}")

    discovery_result = discover_test_targets(
        db,
        repository_id,
        revision_id,
        target_symbol_id=target_symbol_id,
        impacted_symbol_ids=impacted_symbol_ids,
        impacted_file_ids=impacted_file_ids,
        impact_depths=impact_depths,
        max_targets=max_tests,
    )
    targets = discovery_result.targets
    if not targets:
        return TestGenerationRunResult(
            repository_id=repository_id,
            revision_id=revision_id,
            analysis_run_id=None,
            analysis_status="no_targets",
            outcomes=(),
        )

    if llm_provider is None:
        outcomes = tuple(
            TargetGenerationOutcome(
                target=t,
                status="no_provider",
                generated_test_id=None,
                execution_id=None,
                validation_issues=(),
                execution=None,
                error=None,
            )
            for t in targets
        )
        return TestGenerationRunResult(
            repository_id=repository_id,
            revision_id=revision_id,
            analysis_run_id=None,
            analysis_status="no_provider",
            outcomes=outcomes,
        )

    # Provenance comes from the provider that will generate, never from
    # caller-supplied strings that could disagree with it or be omitted.
    provider_name = getattr(llm_provider, "provider_name", None)
    model_name = getattr(llm_provider, "model_name", None)
    if not isinstance(provider_name, str) or not isinstance(model_name, str):
        raise ProviderIdentityError("AI provider does not identify its provider and model")
    if not provider_name.strip() or not model_name.strip():
        raise ProviderIdentityError("AI provider does not identify its provider and model")

    run = AnalysisRun(
        repository_id=repository_id,
        revision_id=revision_id,
        analysis_type="test_generation",
        analyzer_name=ANALYZER_NAME,
        analyzer_version=_analyzer_version(),
        status="running",
        started_at=datetime.now(UTC),
        config={
            "provider": provider_name,
            "model_name": model_name,
            "model_version": None,
            "execute": execute,
        },
    )
    db.add(run)
    db.commit()

    existing_file_paths = frozenset(
        path
        for (path,) in db.execute(
            select(SourceFile.path)
            .where(SourceFile.repository_id == repository_id)
            .where(SourceFile.revision_id == revision_id)
        ).all()
    )
    configuration = _load_configuration(db, repository_id, revision_id)

    try:
        outcome_list = [
            _generate_one(
                db,
                llm_provider,
                repository_id,
                revision_id,
                target,
                existing_file_paths,
                configuration,
                execute=execute,
                provider_name=provider_name,
                model_name=model_name,
                execution_timeout_seconds=execution_timeout_seconds,
                max_output_bytes=max_output_bytes,
                analysis_run_id=run.id,
            )
            for target in targets
        ]
        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        db.commit()
    except Exception as exc:
        db.rollback()
        run.status = "failed"
        run.error_message = str(exc)[:2000]
        run.finished_at = datetime.now(UTC)
        db.commit()
        raise TestGenerationError(str(exc)) from exc

    return TestGenerationRunResult(
        repository_id=repository_id,
        revision_id=revision_id,
        analysis_run_id=run.id,
        analysis_status="completed",
        outcomes=tuple(outcome_list),
    )
