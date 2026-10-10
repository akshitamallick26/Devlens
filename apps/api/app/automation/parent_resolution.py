"""Deterministic parent-revision resolution/verification (Task 16). Uses
`RepositoryRevision.parent_sha` — a real git fact captured at ingestion
time (`app/ingestion/git_adapter.py::read_commit_metadata`) — never a
guess, never "the most recent other revision," and never an arbitrary pair
of UUIDs the caller happens to supply.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.models import ParentResolution
from app.models import RepositoryRevision


def resolve_parent(
    db: Session,
    repository_id: uuid.UUID,
    current_revision: RepositoryRevision,
    requested_parent_revision_id: uuid.UUID | None,
) -> ParentResolution:
    """If the caller supplies `requested_parent_revision_id`, it is
    verified — never trusted — against `current_revision.parent_sha`. If
    the caller supplies nothing, the parent is looked up automatically by
    matching `current_revision.parent_sha` against a revision's own
    `commit_sha` within the same repository. A root commit
    (`parent_sha is None`) or a parent that was never ingested both
    legitimately resolve to "no_parent" — not an error, just nothing to
    diff against."""
    if current_revision.parent_sha is None:
        if requested_parent_revision_id is not None:
            return ParentResolution(
                status="mismatch",
                parent_revision_id=None,
                detail="current revision is a root commit (no parent_sha) but a parent was "
                "requested",
            )
        return ParentResolution(
            status="no_parent",
            parent_revision_id=None,
            detail="current revision has no parent commit",
        )

    if requested_parent_revision_id is not None:
        candidate = db.get(RepositoryRevision, requested_parent_revision_id)
        if candidate is None or candidate.repository_id != repository_id:
            return ParentResolution(
                status="not_found",
                parent_revision_id=None,
                detail=f"no revision {requested_parent_revision_id} in this repository",
            )
        if candidate.commit_sha != current_revision.parent_sha:
            return ParentResolution(
                status="mismatch",
                parent_revision_id=None,
                detail=(
                    f"revision {requested_parent_revision_id} (commit {candidate.commit_sha}) "
                    f"is not the parent of the current revision (expected commit "
                    f"{current_revision.parent_sha})"
                ),
            )
        return ParentResolution(status="resolved", parent_revision_id=candidate.id, detail=None)

    # No parent requested — find it automatically from parent_sha.
    found = db.execute(
        select(RepositoryRevision)
        .where(RepositoryRevision.repository_id == repository_id)
        .where(RepositoryRevision.commit_sha == current_revision.parent_sha)
    ).scalar_one_or_none()
    if found is None:
        return ParentResolution(
            status="no_parent",
            parent_revision_id=None,
            detail=f"parent commit {current_revision.parent_sha} was never ingested",
        )
    return ParentResolution(status="resolved", parent_revision_id=found.id, detail=None)
