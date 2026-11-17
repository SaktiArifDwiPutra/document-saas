"""Health check endpoint for backend service."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.errors import ApiResponse

router = APIRouter(tags=["health"])


class HealthStatus(BaseModel):
    """Health check data payload."""

    service: str
    status: str
    version: str
    environment: str


@router.get("/health", response_model=ApiResponse[HealthStatus])
async def get_health(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ApiResponse[HealthStatus]:
    """Return service readiness without touching external dependencies or exposing internals."""
    return ApiResponse(
        data=HealthStatus(
            service="backend",
            status="ok",
            version=settings.app_version,
            environment=settings.environment,
        )
    )
