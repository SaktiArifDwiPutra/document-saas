"""FastAPI application for the isolated Python Worker service."""

from typing import Annotated, Literal

from fastapi import Depends, FastAPI
from pydantic import BaseModel

from worker_app.core.config import WorkerSettings, get_worker_settings


class WorkerHealthData(BaseModel):
    """Health payload for Python Worker."""

    service: str
    status: str
    version: str
    environment: str


class WorkerHealthResponse(BaseModel):
    """Standardized worker health response envelope."""

    success: Literal[True] = True
    data: WorkerHealthData


def create_worker_app(settings: WorkerSettings | None = None) -> FastAPI:
    """Create and configure the isolated Python Worker HTTP service."""
    resolved_settings = settings or get_worker_settings()

    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
    )

    if settings is not None:
        application.dependency_overrides[get_worker_settings] = lambda: resolved_settings

    @application.get("/health", response_model=WorkerHealthResponse)
    @application.get("/api/v1/health", response_model=WorkerHealthResponse)
    async def get_health(
        active_settings: Annotated[WorkerSettings, Depends(get_worker_settings)],
    ) -> WorkerHealthResponse:
        return WorkerHealthResponse(
            data=WorkerHealthData(
                service="worker",
                status="ok",
                version=active_settings.app_version,
                environment=active_settings.environment,
            )
        )

    return application


app = create_worker_app()
