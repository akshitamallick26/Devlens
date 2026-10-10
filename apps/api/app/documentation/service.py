"""Documentation Intelligence orchestration: `generate_documentation`.
Pipeline —

    validate artifact_type -> resolve revision -> resolve/validate target
    -> start AnalysisRun -> assemble deterministic context (bounded)
    -> LLM generation, falling back to deterministic generation on any
       failure -> persist/upsert GeneratedArtifact -> finish AnalysisRun

Deterministic stages (target discovery, context assembly) work identically
with no LLM configured — they simply route straight to the deterministic
fallback. No autonomous retry loop: each request generates (or reuses) at
most one artifact. See docs/architecture/documentation-intelligence.md.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.models import AIProvider
from app.documentation.context import (
    assemble_architecture_overview_context,
    assemble_module_context,
    assemble_repository_overview_context,
    assemble_symbol_context,
)
from app.documentation.discovery import resolve_target
from app.documentation.fallback import generate_fallback_draft
from app.documentation.generation import generate_documentation_draft
from app.documentation.models import (
    ARTIFACT_TYPES,
    DocumentationArtifactResult,
    DocumentationArtifactSummary,
)
from app.documentation.persistence import fail_run, finish_run, start_run, upsert_artifact
from app.models import (
    GeneratedArtifact,
    Repository,
    RepositoryRevision,
    SourceBlob,
    SourceFile,
    Symbol,
)

logger = logging.getLogger("devlens.documentation")

# Newest first; one revision can hold one artifact per symbol, so the
# listing is capped rather than returning an unbounded row set.
MAX_LISTED_ARTIFACTS = 200


class DocumentationError(Exception):
    pass


class RevisionNotFoundError(DocumentationError):
    pass


class InvalidArtifactTypeError(DocumentationError):
    pass


class MissingTargetError(DocumentationError):
    pass


class UnexpectedTargetError(DocumentationError):
    pass


class TargetNotFoundError(DocumentationError):
    pass


class TargetConflictError(DocumentationError):
    """The requested target exists but belongs to a different repository
    or a different revision than requested."""


class ArtifactNotFoundError(DocumentationError):
    pass


def _result_from_artifact(artifact: GeneratedArtifact, content: str) -> DocumentationArtifactResult:
    assert artifact.artifact_type is not None
    assert artifact.generation_method is not None
    return DocumentationArtifactResult(
        artifact_id=artifact.id,
        repository_id=artifact.repository_id,
        revision_id=artifact.revision_id,
        artifact_type=artifact.artifact_type,
        target_file_id=artifact.related_file_id,
        target_symbol_id=artifact.related_symbol_id,
        title=artifact.title,
        content=content,
        generation_method=artifact.generation_method,
        provider=artifact.provider,
        model_name=artifact.model_name,
        model_version=artifact.model_version,
        evidence_ids=tuple(artifact.evidence_ids or ()),
        cited_evidence_ids=tuple(artifact.cited_evidence_ids or ()),
        context_truncated=artifact.context_truncated,
        analysis_run_id=artifact.analysis_run_id,  # type: ignore[arg-type]
        created_at=artifact.created_at.isoformat(),
        updated_at=artifact.updated_at.isoformat(),
    )


def generate_documentation(
    db: Session,
    llm_provider: AIProvider | None,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    artifact_type: str,
    *,
    source_file_id: uuid.UUID | None = None,
    symbol_id: uuid.UUID | None = None,
    provider_name: str = "",
    model_name: str = "",
    model_version: str = "",
) -> DocumentationArtifactResult:
    if artifact_type not in ARTIFACT_TYPES:
        raise InvalidArtifactTypeError(f"artifact_type must be one of {ARTIFACT_TYPES}")

    revision = db.get(RepositoryRevision, revision_id)
    if revision is None or revision.repository_id != repository_id:
        raise RevisionNotFoundError(f"no revision {revision_id} for repository {repository_id}")
    repository = db.get(Repository, repository_id)
    assert repository is not None  # guaranteed by the revision match above

    resolution = resolve_target(
        db,
        repository_id,
        revision_id,
        artifact_type,
        source_file_id=source_file_id,
        symbol_id=symbol_id,
    )
    if resolution.status == "missing_target":
        raise MissingTargetError(resolution.detail or "missing target")
    if resolution.status == "unexpected_target":
        raise UnexpectedTargetError(resolution.detail or "unexpected target")
    if resolution.status == "not_found":
        raise TargetNotFoundError(resolution.detail or "target not found")
    if resolution.status in ("wrong_repository", "wrong_revision"):
        raise TargetConflictError(resolution.detail or "target conflict")

    run = start_run(
        db,
        repository_id,
        revision_id,
        artifact_type,
        resolution.target_file_id,
        resolution.target_symbol_id,
    )

    try:
        if artifact_type == "repository_overview":
            package = assemble_repository_overview_context(db, repository, revision)
        elif artifact_type == "architecture_overview":
            package = assemble_architecture_overview_context(db, repository, revision)
        elif artifact_type == "module_documentation":
            target_file = db.get(SourceFile, resolution.target_file_id)
            assert target_file is not None
            package = assemble_module_context(db, repository, revision, target_file)
        else:
            target_symbol = db.get(Symbol, resolution.target_symbol_id)
            assert target_symbol is not None
            target_symbol_file = db.get(SourceFile, target_symbol.file_id)
            assert target_symbol_file is not None
            package = assemble_symbol_context(
                db, repository, revision, target_symbol, target_symbol_file.path
            )

        effective_provider_name, effective_model_name, effective_model_version = "", "", ""
        if llm_provider is not None:
            try:
                draft = generate_documentation_draft(llm_provider, package)
                generation_method = "llm_generated"
                effective_provider_name = provider_name
                effective_model_name = model_name
                effective_model_version = model_version
            except Exception as exc:
                # Deliberately broader than `except AIProviderError`: the
                # identical seam app/impact/explanation.py already
                # established — any provider misbehavior degrades to the
                # deterministic fallback, never loses the result. Context
                # assembly above this block is NOT covered by this catch.
                logger.warning(
                    "documentation generation unavailable for %s: %s", artifact_type, exc
                )
                draft = generate_fallback_draft(package)
                generation_method = "deterministic_fallback"
        else:
            draft = generate_fallback_draft(package)
            generation_method = "deterministic_fallback"

        artifact = upsert_artifact(
            db,
            package,
            draft,
            generation_method,
            effective_provider_name,
            effective_model_name,
            effective_model_version,
            run.id,
        )
        finish_run(db, run)
    except Exception as exc:
        fail_run(db, run, str(exc))
        raise DocumentationError(str(exc)) from exc

    content = _load_artifact_content(db, artifact)
    return _result_from_artifact(artifact, content)


def _load_artifact_content(db: Session, artifact: GeneratedArtifact) -> str:
    blob = db.get(SourceBlob, artifact.content_hash)
    assert blob is not None and blob.inline_content is not None
    return blob.inline_content


def get_artifact(
    db: Session, repository_id: uuid.UUID, artifact_id: uuid.UUID
) -> DocumentationArtifactResult:
    artifact = db.get(GeneratedArtifact, artifact_id)
    if (
        artifact is None
        or artifact.repository_id != repository_id
        or artifact.kind != "documentation"
    ):
        raise ArtifactNotFoundError(f"no documentation artifact {artifact_id}")
    content = _load_artifact_content(db, artifact)
    return _result_from_artifact(artifact, content)


def _summary_from_artifact(artifact: GeneratedArtifact) -> DocumentationArtifactSummary:
    assert artifact.artifact_type is not None
    assert artifact.generation_method is not None
    return DocumentationArtifactSummary(
        artifact_id=artifact.id,
        repository_id=artifact.repository_id,
        revision_id=artifact.revision_id,
        artifact_type=artifact.artifact_type,
        target_file_id=artifact.related_file_id,
        target_symbol_id=artifact.related_symbol_id,
        title=artifact.title,
        generation_method=artifact.generation_method,
        created_at=artifact.created_at.isoformat(),
        updated_at=artifact.updated_at.isoformat(),
    )


def list_artifacts(
    db: Session,
    repository_id: uuid.UUID,
    *,
    revision_id: uuid.UUID | None = None,
    artifact_type: str | None = None,
) -> tuple[DocumentationArtifactSummary, ...]:
    """Summaries only (no content) — same "don't load the heavy payload to
    list rows" discipline as app/investigation/service.py::
    list_recent_sessions. One query, no N+1."""
    stmt = (
        select(GeneratedArtifact)
        .where(GeneratedArtifact.repository_id == repository_id)
        .where(GeneratedArtifact.kind == "documentation")
        .order_by(GeneratedArtifact.updated_at.desc(), GeneratedArtifact.id.desc())
    )
    if revision_id is not None:
        stmt = stmt.where(GeneratedArtifact.revision_id == revision_id)
    if artifact_type is not None:
        stmt = stmt.where(GeneratedArtifact.artifact_type == artifact_type)
    rows = db.execute(stmt.limit(MAX_LISTED_ARTIFACTS)).scalars().all()
    return tuple(_summary_from_artifact(a) for a in rows)
