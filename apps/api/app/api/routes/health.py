from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app_name,
        "environment": settings.environment,
    }


@router.get("/health/db")
def health_db(db: Annotated[Session, Depends(get_db)]) -> dict[str, str]:
    """Minimal readiness check: can we reach PostgreSQL. Not used for
    liveness — /health stays dependency-free so the API can start cleanly
    before the database is up."""
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "reachable"}
