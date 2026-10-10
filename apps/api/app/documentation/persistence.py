"""DB-touching persistence for Documentation Intelligence v1: content blob
storage, idempotent GeneratedArtifact upsert, and AnalysisRun lifecycle.
See docs/architecture/documentation-intelligence.md sections 12/13.
"""

import hashlib
import platform
import uuid
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.documentation.models import DocumentationArtifactDraft, DocumentationContextPackage
from app.models import AnalysisRun, GeneratedArtifact, SourceBlob

ANALYZER_NAME = "devlens-documentation"


def analyzer_version() -> str:
    return f"documentation@{platform.python_version()}"


def _store_content_blob(db: Session, content: str) -> str:
    """Content-addressed, same mechanism as SourceFile's/GeneratedTest's own
    blobs (app/testing/persistence.py::_store_source_blob) — reuses an
    existing blob by hash rather than ever writing a duplicate."""
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    blob = db.get(SourceBlob, content_hash)
    if blob is None:
        blob = SourceBlob(
            content_hash=content_hash,
            inline_content=content,
            size_bytes=len(content.encode("utf-8")),
        )
        db.add(blob)
        db.flush()
    return content_hash


def render_markdown(draft: DocumentationArtifactDraft) -> str:
    """Deterministic rendering of the validated structured draft into the
    stored markdown content — identical shape regardless of
    generation_method, so a reader can't tell from formatting alone
    whether an artifact was LLM-authored or fallback-authored."""
    lines = [f"# {draft.title}", "", draft.summary, ""]
    for section in draft.sections:
        lines.append(f"## {section.heading}")
        lines.append(section.content)
        if section.evidence_ids:
            lines.append(f"\n_Evidence: {', '.join(section.evidence_ids)}_")
        lines.append("")
    return "\n".join(lines)


def start_run(
    db: Session,
    repository_id: uuid.UUID,
    revision_id: uuid.UUID,
    artifact_type: str,
    target_file_id: uuid.UUID | None,
    target_symbol_id: uuid.UUID | None,
) -> AnalysisRun:
    """Append-only, same convention every analyzer except Automation
    Intelligence uses (docs/architecture/documentation-intelligence.md
    section 13) — one fresh row per generation attempt."""
    run = AnalysisRun(
        repository_id=repository_id,
        revision_id=revision_id,
        analysis_type="documentation_generation",
        analyzer_name=ANALYZER_NAME,
        analyzer_version=analyzer_version(),
        status="running",
        started_at=datetime.now(UTC),
        config={
            "artifact_type": artifact_type,
            "target_file_id": str(target_file_id) if target_file_id else None,
            "target_symbol_id": str(target_symbol_id) if target_symbol_id else None,
        },
    )
    db.add(run)
    db.commit()
    return run


def finish_run(db: Session, run: AnalysisRun) -> None:
    run.status = "completed"
    run.finished_at = datetime.now(UTC)
    db.commit()


def fail_run(db: Session, run: AnalysisRun, error_message: str) -> None:
    db.rollback()
    run.status = "failed"
    run.error_message = error_message[:2000]
    run.finished_at = datetime.now(UTC)
    db.commit()


def _index_for(artifact_type: str) -> tuple[list[str], str]:
    if artifact_type in ("repository_overview", "architecture_overview"):
        return (
            ["repository_id", "revision_id", "artifact_type"],
            "artifact_type IN ('repository_overview', 'architecture_overview')",
        )
    if artifact_type == "module_documentation":
        return (
            ["repository_id", "revision_id", "artifact_type", "related_file_id"],
            "artifact_type = 'module_documentation'",
        )
    return (
        ["repository_id", "revision_id", "artifact_type", "related_symbol_id"],
        "artifact_type = 'symbol_documentation'",
    )


def upsert_artifact(
    db: Session,
    package: DocumentationContextPackage,
    draft: DocumentationArtifactDraft,
    generation_method: str,
    provider_name: str,
    model_name: str,
    model_version: str,
    analysis_run_id: uuid.UUID,
) -> GeneratedArtifact:
    """Atomically creates the logical artifact for this exact
    `(repository_id, revision_id, artifact_type, target)` identity, or — if
    one already exists — replaces its content/provenance in place (Task
    12). Never creates a second row for an identity that already has one.
    Mirrors `app/automation/persistence.py::start_or_reuse_run`'s upsert
    idiom exactly."""
    content = render_markdown(draft)
    content_hash = _store_content_blob(db, content)
    now = datetime.now(UTC)
    index_elements, index_where_sql = _index_for(package.artifact_type)

    # Ordered (D1, D2, ...), not a set — the full list actually supplied,
    # distinct from `cited_evidence_ids` (what the generator actually used).
    evidence_ids = [item.evidence_id for item in package.evidence.items]

    values = {
        "repository_id": package.repository_id,
        "revision_id": package.revision_id,
        "analysis_run_id": analysis_run_id,
        "kind": "documentation",
        "title": draft.title,
        "format": "markdown",
        "content_hash": content_hash,
        "related_file_id": package.target_file_id,
        "related_symbol_id": package.target_symbol_id,
        "status": "published",
        "artifact_type": package.artifact_type,
        "generation_method": generation_method,
        "provider": provider_name,
        "model_name": model_name,
        "model_version": model_version,
        "evidence_ids": evidence_ids,
        "cited_evidence_ids": list(draft.evidence_ids),
        "context_truncated": package.truncated,
        "updated_at": now,
    }
    update_columns = {
        k: v
        for k, v in values.items()
        if k not in ("repository_id", "revision_id", "artifact_type", "kind")
    }

    stmt = (
        pg_insert(GeneratedArtifact)
        .values(**values)
        .on_conflict_do_update(
            index_elements=index_elements,
            index_where=text(index_where_sql),
            set_=update_columns,
        )
        .returning(GeneratedArtifact.id)
    )
    artifact_id = db.execute(stmt).scalar_one()
    db.commit()
    # populate_existing: the app's SessionLocal uses expire_on_commit=False, so
    # without it a row already in this session's identity map (an earlier
    # upsert of the same identity) would be returned with its stale values.
    artifact = db.get(GeneratedArtifact, artifact_id, populate_existing=True)
    assert artifact is not None  # just upserted and committed above
    return artifact
