"""The Repository Overview HTTP surface. Deliberately thin, same convention
as every other app/api/routes/*.py: every decision lives in
app/overview/service.py — this module only parses ids, translates typed
exceptions into HTTP, and serializes the pure result shape. The frozen
`RepositoryOverview` dataclass is the response model (FastAPI validates and
serializes it through Pydantic); no ORM object is ever returned. See
docs/architecture/repository-overview.md section 9.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.overview.models import RepositoryOverview
from app.overview.service import (
    OverviewError,
    RepositoryNotFoundError,
    RevisionConflictError,
    RevisionNotFoundError,
    get_repository_overview,
)

router = APIRouter()


def _parse_uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid {label}") from exc


def _handle(exc: OverviewError) -> HTTPException:
    if isinstance(exc, (RepositoryNotFoundError, RevisionNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RevisionConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/repositories/{repository_id}/overview", response_model=RepositoryOverview)
def get_overview_endpoint(
    repository_id: str,
    db: Annotated[Session, Depends(get_db)],
    revision_id: Annotated[str | None, Query()] = None,
) -> RepositoryOverview:
    repo_uuid = _parse_uuid(repository_id, "repository_id")
    revision_uuid = _parse_uuid(revision_id, "revision_id") if revision_id is not None else None
    try:
        return get_repository_overview(db, repo_uuid, revision_uuid)
    except OverviewError as exc:
        raise _handle(exc) from exc
