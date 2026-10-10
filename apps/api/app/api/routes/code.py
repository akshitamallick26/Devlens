"""Read-side code intelligence HTTP surface: revisions, files, bounded
source, symbols, the bounded graph neighbourhood, and persisted security
findings / generated tests. Thin: every decision lives in
app/codeintel/service.py. Every endpoint is a GET, read-only, and bounded
(FastAPI rejects out-of-range limits/depths with 422 before any query).
"""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.codeintel import service
from app.codeintel.models import (
    DirectoryListing,
    FileDetail,
    FindingPage,
    GeneratedTestPage,
    GraphNeighborhood,
    RevisionPage,
    SourceRange,
    SymbolDetail,
    SymbolSearchResult,
)
from app.db.session import get_db

router = APIRouter()

Db = Annotated[Session, Depends(get_db)]
Limit = Annotated[int, Query(ge=1, le=service.MAX_PAGE_SIZE)]
Offset = Annotated[int, Query(ge=0)]
BASE = "/repositories/{repository_id}/revisions/{revision_id}"


def _parse_uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid {label}") from exc


def _handle(exc: service.CodeIntelError) -> HTTPException:
    if isinstance(
        exc,
        (service.RepositoryNotFoundError, service.RevisionNotFoundError, service.NotFoundError),
    ):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, service.RevisionConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def _ids(repository_id: str, revision_id: str) -> tuple[uuid.UUID, uuid.UUID]:
    return _parse_uuid(repository_id, "repository_id"), _parse_uuid(revision_id, "revision_id")


@router.get("/repositories/{repository_id}/revisions", response_model=RevisionPage)
def list_revisions_endpoint(
    repository_id: str, db: Db, limit: Limit = 50, offset: Offset = 0
) -> RevisionPage:
    try:
        return service.list_revisions(
            db, _parse_uuid(repository_id, "repository_id"), limit, offset
        )
    except service.CodeIntelError as exc:
        raise _handle(exc) from exc


@router.get(f"{BASE}/files", response_model=DirectoryListing)
def browse_files_endpoint(
    repository_id: str,
    revision_id: str,
    db: Db,
    directory: Annotated[str, Query(max_length=service.MAX_PATH_LENGTH)] = "",
    limit: Limit = 100,
    offset: Offset = 0,
) -> DirectoryListing:
    try:
        return service.browse_directory(
            db, *_ids(repository_id, revision_id), directory, limit, offset
        )
    except service.CodeIntelError as exc:
        raise _handle(exc) from exc


@router.get(f"{BASE}/files/{{file_id}}", response_model=FileDetail)
def file_detail_endpoint(repository_id: str, revision_id: str, file_id: str, db: Db) -> FileDetail:
    try:
        return service.get_file_detail(
            db, *_ids(repository_id, revision_id), _parse_uuid(file_id, "file_id")
        )
    except service.CodeIntelError as exc:
        raise _handle(exc) from exc


@router.get(f"{BASE}/files/{{file_id}}/source", response_model=SourceRange)
def file_source_endpoint(
    repository_id: str,
    revision_id: str,
    file_id: str,
    db: Db,
    start_line: Annotated[int, Query(ge=1)] = 1,
    end_line: Annotated[int | None, Query(ge=1)] = None,
) -> SourceRange:
    try:
        return service.get_source_range(
            db,
            *_ids(repository_id, revision_id),
            _parse_uuid(file_id, "file_id"),
            start_line,
            end_line,
        )
    except service.CodeIntelError as exc:
        raise _handle(exc) from exc


@router.get(f"{BASE}/symbols", response_model=SymbolSearchResult)
def search_symbols_endpoint(
    repository_id: str,
    revision_id: str,
    db: Db,
    q: Annotated[str, Query(min_length=1, max_length=300)],
    limit: Annotated[int, Query(ge=1, le=service.MAX_SYMBOL_SEARCH)] = 20,
) -> SymbolSearchResult:
    try:
        return service.search_symbols(db, *_ids(repository_id, revision_id), q, limit)
    except service.CodeIntelError as exc:
        raise _handle(exc) from exc


@router.get(f"{BASE}/symbols/{{symbol_id}}", response_model=SymbolDetail)
def symbol_detail_endpoint(
    repository_id: str, revision_id: str, symbol_id: str, db: Db
) -> SymbolDetail:
    try:
        return service.get_symbol_detail(
            db, *_ids(repository_id, revision_id), _parse_uuid(symbol_id, "symbol_id")
        )
    except service.CodeIntelError as exc:
        raise _handle(exc) from exc


@router.get(f"{BASE}/graph/neighborhood", response_model=GraphNeighborhood)
def graph_neighborhood_endpoint(
    repository_id: str,
    revision_id: str,
    db: Db,
    symbol_id: str,
    direction: Literal["dependents", "dependencies", "both"] = "both",
    depth: Annotated[int, Query(ge=1, le=service.MAX_GRAPH_DEPTH)] = 2,
    limit: Annotated[int, Query(ge=1, le=service.MAX_GRAPH_NODES)] = 100,
) -> GraphNeighborhood:
    try:
        return service.graph_neighborhood(
            db,
            *_ids(repository_id, revision_id),
            _parse_uuid(symbol_id, "symbol_id"),
            direction,
            depth,
            limit,
        )
    except service.CodeIntelError as exc:
        raise _handle(exc) from exc


@router.get(f"{BASE}/security/findings", response_model=FindingPage)
def findings_endpoint(
    repository_id: str, revision_id: str, db: Db, limit: Limit = 50, offset: Offset = 0
) -> FindingPage:
    try:
        return service.list_findings(db, *_ids(repository_id, revision_id), limit, offset)
    except service.CodeIntelError as exc:
        raise _handle(exc) from exc


@router.get(f"{BASE}/testing/generated-tests", response_model=GeneratedTestPage)
def generated_tests_endpoint(
    repository_id: str, revision_id: str, db: Db, limit: Limit = 50, offset: Offset = 0
) -> GeneratedTestPage:
    try:
        return service.list_generated_tests(db, *_ids(repository_id, revision_id), limit, offset)
    except service.CodeIntelError as exc:
        raise _handle(exc) from exc
