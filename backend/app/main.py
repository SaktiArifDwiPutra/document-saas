"""FastAPI application factory."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_v1_router
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    resolved_settings = settings or get_settings()

    application = FastAPI(
        title=resolved_settings.app_name,
        version=resolved_settings.app_version,
        openapi_url=f"{resolved_settings.api_v1_prefix}/openapi.json",
        docs_url=f"{resolved_settings.api_v1_prefix}/docs",
        redoc_url=f"{resolved_settings.api_v1_prefix}/redoc",
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )

    register_exception_handlers(application)
    if settings is not None:
        application.dependency_overrides[get_settings] = lambda: resolved_settings
    application.include_router(api_v1_router, prefix=resolved_settings.api_v1_prefix)
    return application


app = create_app()
