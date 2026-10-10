import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes.automation import router as automation_router
from app.api.routes.code import router as code_router
from app.api.routes.documentation import router as documentation_router
from app.api.routes.health import router as health_router
from app.api.routes.impact import router as impact_router
from app.api.routes.ingestion import router as ingestion_router
from app.api.routes.investigation import router as investigation_router
from app.api.routes.overview import router as overview_router
from app.api.routes.pipeline import router as pipeline_router
from app.api.routes.qa import router as qa_router
from app.api.routes.security import router as security_router
from app.api.routes.testing import router as testing_router
from app.core.config import get_settings

logger = logging.getLogger("devlens.api")

settings = get_settings()

app = FastAPI(title=settings.app_name)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error while processing %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


app.include_router(health_router)
app.include_router(ingestion_router)
app.include_router(impact_router)
app.include_router(qa_router)
app.include_router(testing_router)
app.include_router(security_router)
app.include_router(automation_router)
app.include_router(investigation_router)
app.include_router(documentation_router)
app.include_router(overview_router)
app.include_router(pipeline_router)
app.include_router(code_router)
