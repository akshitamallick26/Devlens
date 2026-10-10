"""Repository-ingestion orchestration: URL -> Repository/RepositoryRevision/
SourceBlob/SourceFile rows. See docs/architecture/repository-ingestion.md for
the full design — this module implements it; it doesn't add new design.

Route -> service -> data-access boundary: this module owns the Git
subprocess calls, the transaction/commit boundaries, and the logging. The
FastAPI route (app/api/routes/ingestion.py) only translates exceptions into
HTTP responses.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.ingestion.checkout import Checkout, checkout_repository
from app.ingestion.git_adapter import (
    FileTooLargeError,
    GitCommandError,
    GitTimeoutError,
    InvalidCommitShaError,
    PathEscapeError,
    list_tracked_files,
    read_file_at_revision,
)
from app.ingestion.inventory import FileCategory, classify_file
from app.ingestion.url_policy import RepositoryIdentity, normalize_repository_url
from app.models import AnalysisRun, Repository, RepositoryRevision, SourceBlob, SourceFile

logger = logging.getLogger("devlens.ingestion")

ANALYZER_NAME = "devlens-git-ingestion"
ANALYZER_VERSION = "0.1.0"

_PRE_REVISION_ERRORS = (
    GitCommandError,
    GitTimeoutError,
    PathEscapeError,
    FileTooLargeError,
    InvalidCommitShaError,
)


class IngestionError(Exception):
    """Base class for ingestion failures that aren't URL-validation errors
    (those raise `RepositoryURLError` directly — see url_policy.py)."""


class RepositoryTooLargeError(IngestionError):
    """Raised during file discovery, before any RepositoryRevision exists."""


class ContentBudgetExceededError(IngestionError):
    """Raised while writing SourceFile/SourceBlob rows, after the revision
    and its AnalysisRun already exist."""


class _IngestionAlreadyRecorded(IngestionError):
    """Internal marker: the failure that caused this was already fully
    recorded (AnalysisRun + RepositoryRevision + Repository all updated) by
    `_write_source_files`'s own exception handler. The outer handler in
    `ingest_repository` must not record it a second time."""


@dataclass(frozen=True)
class IngestionResult:
    repository_id: str
    revision_id: str
    commit_sha: str
    status: str
    file_count: int


def _log_event(event: str, **fields: object) -> None:
    """Structured-ish logging without a logging framework: the event name is
    always the first positional arg, every other field goes through `extra`
    so log aggregators that support structured fields can use them. Never
    pass file contents, URLs with credentials, or secrets here — only
    identifiers (owner/name/sha/counts)."""
    logger.info(event, extra={"event": event, **fields})


def _get_or_create_repository(db: Session, identity: RepositoryIdentity) -> Repository:
    repository = (
        db.query(Repository).filter_by(owner=identity.owner, name=identity.name).one_or_none()
    )
    if repository is None:
        repository = Repository(
            owner=identity.owner,
            name=identity.name,
            url=identity.canonical_url,
            default_branch="",  # populated once the checkout tells us the real default branch
            status="pending",
        )
        db.add(repository)
        db.flush()
    return repository


def _store_blob(db: Session, seen: dict[str, SourceBlob], content: bytes) -> SourceBlob:
    content_hash = hashlib.sha256(content).hexdigest()
    blob = seen.get(content_hash) or db.get(SourceBlob, content_hash)
    if blob is None:
        blob = SourceBlob(
            content_hash=content_hash,
            inline_content=content.decode("utf-8", errors="replace"),
            size_bytes=len(content),
        )
        db.add(blob)
        db.flush()
    seen[content_hash] = blob
    return blob


def ingest_repository(db: Session, url: str) -> IngestionResult:
    """The public entry point: validates `url` through the same URL policy
    production traffic always goes through, then delegates to
    `ingest_identity`. Raises `RepositoryURLError` (bad input, no repository
    touched yet) or `IngestionError` (checkout/clone/content failure — the
    repository is left in `status='error'`, never a misleading `'ready'`)."""
    identity = normalize_repository_url(url)  # raises RepositoryURLError — caller maps it
    return ingest_identity(db, identity)


def ingest_identity(db: Session, identity: RepositoryIdentity) -> IngestionResult:
    """The actual ingestion core, taking an already-validated identity. Split
    out from `ingest_repository` so tests can exercise the full
    checkout/inventory/hash/write pipeline against a local Git fixture
    (`identity.clone_url` pointing at a local path) without needing real
    network access or weakening `normalize_repository_url`'s HTTPS-only
    policy for production traffic, which always goes through
    `ingest_repository` instead."""
    settings = get_settings()
    _log_event("ingestion_started", owner=identity.owner, name=identity.name)

    repository = _get_or_create_repository(db, identity)
    repository.status = "ingesting"
    db.commit()
    _log_event("repository_normalized", repository_id=str(repository.id))

    try:
        return _run_checkout_and_write(db, settings, repository, identity)
    except _IngestionAlreadyRecorded as exc:
        # _write_source_files's own handler already recorded run/revision/
        # repository failure state — don't do it again.
        raise IngestionError(str(exc)) from exc
    except _PRE_REVISION_ERRORS + (RepositoryTooLargeError,) as exc:
        # Happened before any RepositoryRevision existed (checkout itself,
        # or the file-count check in discovery) — only the repository needs
        # cleanup here.
        db.rollback()
        repository.status = "error"
        db.commit()
        _log_event("ingestion_failed", repository_id=str(repository.id), reason=str(exc))
        raise IngestionError(str(exc)) from exc


def _run_checkout_and_write(
    db: Session, settings: Settings, repository: Repository, identity: RepositoryIdentity
) -> IngestionResult:
    _log_event("clone_started", repository_id=str(repository.id))
    with checkout_repository(
        identity.clone_url, timeout=settings.ingestion_clone_timeout_seconds
    ) as checkout:
        _log_event(
            "clone_completed", repository_id=str(repository.id), commit_sha=checkout.commit_sha
        )
        _log_event("revision_resolved", commit_sha=checkout.commit_sha, branch=checkout.branch)

        if not repository.default_branch and checkout.branch:
            repository.default_branch = checkout.branch
            db.commit()

        idempotent_result = _check_idempotent(db, repository, checkout)
        if idempotent_result is not None:
            return idempotent_result

        tracked_files = list_tracked_files(
            checkout.path, timeout=settings.ingestion_git_command_timeout_seconds
        )
        if len(tracked_files) > settings.ingestion_max_files:
            raise RepositoryTooLargeError(
                f"{len(tracked_files)} tracked files exceeds the limit of "
                f"{settings.ingestion_max_files}"
            )
        _log_event(
            "inventory_completed", repository_id=str(repository.id), file_count=len(tracked_files)
        )

        revision, run = _create_revision_and_run(db, repository, checkout)

        try:
            file_count = _write_source_files(
                db, settings, checkout.path, repository, revision, tracked_files
            )
            return _record_success(db, repository, revision, run, file_count)
        except Exception as exc:
            _record_failure(db, repository, revision, run, exc)
            raise _IngestionAlreadyRecorded(str(exc)) from exc


def _check_idempotent(
    db: Session, repository: Repository, checkout: Checkout
) -> IngestionResult | None:
    existing = (
        db.query(RepositoryRevision)
        .filter_by(repository_id=repository.id, commit_sha=checkout.commit_sha)
        .one_or_none()
    )
    if existing is None:
        return None

    repository.current_revision_id = existing.id
    repository.status = "ready"
    repository.last_successful_analysis_at = datetime.now(UTC)
    db.commit()
    _log_event(
        "ingestion_completed",
        repository_id=str(repository.id),
        revision_id=str(existing.id),
        idempotent=True,
    )
    return IngestionResult(
        repository_id=str(repository.id),
        revision_id=str(existing.id),
        commit_sha=existing.commit_sha,
        status="ready",
        file_count=existing.file_count or 0,
    )


def _create_revision_and_run(
    db: Session, repository: Repository, checkout: Checkout
) -> tuple[RepositoryRevision, AnalysisRun]:
    revision = RepositoryRevision(
        repository_id=repository.id,
        commit_sha=checkout.commit_sha,
        parent_sha=checkout.commit_metadata.parent_sha,
        branch_ref=checkout.branch,
        author_name=checkout.commit_metadata.author_name,
        author_email=checkout.commit_metadata.author_email,
        committed_at=checkout.commit_metadata.committed_at,
        latest_analysis_status="running",
    )
    db.add(revision)
    db.flush()

    run = AnalysisRun(
        repository_id=repository.id,
        revision_id=revision.id,
        analysis_type="ingestion",
        analyzer_name=ANALYZER_NAME,
        analyzer_version=ANALYZER_VERSION,
        status="running",
        started_at=datetime.now(UTC),
    )
    db.add(run)
    db.commit()  # revision + running run are durable independent of what follows
    return revision, run


def _record_success(
    db: Session,
    repository: Repository,
    revision: RepositoryRevision,
    run: AnalysisRun,
    file_count: int,
) -> IngestionResult:
    revision.file_count = file_count
    revision.latest_analysis_status = "completed"
    run.status = "completed"
    run.finished_at = datetime.now(UTC)
    repository.status = "ready"
    repository.current_revision_id = revision.id
    repository.last_successful_analysis_at = datetime.now(UTC)
    db.commit()
    _log_event(
        "ingestion_completed",
        repository_id=str(repository.id),
        revision_id=str(revision.id),
        file_count=file_count,
    )
    return IngestionResult(
        repository_id=str(repository.id),
        revision_id=str(revision.id),
        commit_sha=revision.commit_sha,
        status="ready",
        file_count=file_count,
    )


def _record_failure(
    db: Session,
    repository: Repository,
    revision: RepositoryRevision,
    run: AnalysisRun,
    exc: Exception,
) -> None:
    db.rollback()  # discards every SourceFile/SourceBlob written this attempt
    run.status = "failed"
    run.error_message = str(exc)[:2000]
    run.finished_at = datetime.now(UTC)
    revision.latest_analysis_status = "failed"
    repository.status = "error"
    db.commit()
    _log_event(
        "ingestion_failed",
        repository_id=str(repository.id),
        revision_id=str(revision.id),
        reason=str(exc),
    )


def _write_source_files(
    db: Session,
    settings: Settings,
    repo_path: Path,
    repository: Repository,
    revision: RepositoryRevision,
    tracked_files: list[str],
) -> int:
    seen_blobs: dict[str, SourceBlob] = {}
    total_bytes = 0
    file_count = 0

    for relative_path in tracked_files:
        entry = classify_file(
            repo_path, relative_path, max_file_bytes=settings.ingestion_max_file_bytes
        )
        if entry.category != FileCategory.TEXT:
            continue

        content = read_file_at_revision(
            repo_path, relative_path, max_bytes=settings.ingestion_max_file_bytes
        )
        total_bytes += len(content)
        if total_bytes > settings.ingestion_max_total_bytes:
            raise ContentBudgetExceededError(
                f"total ingested bytes exceeded {settings.ingestion_max_total_bytes}"
            )

        blob = _store_blob(db, seen_blobs, content)
        source_file = SourceFile(
            repository_id=repository.id,
            revision_id=revision.id,
            path=relative_path,
            content_hash=blob.content_hash,
            language=entry.language,
            size_bytes=entry.size_bytes,
            line_count=content.count(b"\n") + 1 if content else 0,
            is_binary=False,
            parsing_status="pending",
        )
        db.add(source_file)
        file_count += 1

    _log_event("blobs_stored", repository_id=str(repository.id), blob_count=len(seen_blobs))
    _log_event("source_files_stored", repository_id=str(repository.id), file_count=file_count)
    return file_count
