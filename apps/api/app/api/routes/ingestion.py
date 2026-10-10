"""The only HTTP surface this phase adds. Deliberately thin: every
Git/filesystem/database decision lives in app/ingestion/service.py — this
module only translates typed exceptions into HTTP responses. See
docs/architecture/repository-ingestion.md section 2 (non-goals) and 9
(endpoint).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.ingestion.service import IngestionError, IngestionResult, ingest_repository
from app.ingestion.url_policy import RepositoryURLError

router = APIRouter()


class IngestRepositoryRequest(BaseModel):
    url: str


class IngestRepositoryResponse(BaseModel):
    repository_id: str
    revision_id: str
    commit_sha: str
    status: str
    file_count: int

    @classmethod
    def from_result(cls, result: IngestionResult) -> "IngestRepositoryResponse":
        return cls(
            repository_id=result.repository_id,
            revision_id=result.revision_id,
            commit_sha=result.commit_sha,
            status=result.status,
            file_count=result.file_count,
        )


@router.post("/repositories/ingest", response_model=IngestRepositoryResponse)
def ingest_repository_endpoint(
    payload: IngestRepositoryRequest, db: Annotated[Session, Depends(get_db)]
) -> IngestRepositoryResponse:
    try:
        result = ingest_repository(db, payload.url)
    except RepositoryURLError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IngestionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return IngestRepositoryResponse.from_result(result)
