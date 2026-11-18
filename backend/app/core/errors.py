"""Standardized API envelopes and exception handlers."""

import logging
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class ApiResponse[DataT](BaseModel):
    """Standardized success response envelope."""

    success: Literal[True] = True
    data: DataT


class ErrorDetail(BaseModel):
    """Safe structured error payload."""

    code: str
    message: str
    details: dict[str, Any] | None = None


class ApiErrorResponse(BaseModel):
    """Standardized error response envelope."""

    success: Literal[False] = False
    error: ErrorDetail


class AppError(Exception):
    """Application-level domain/HTTP exception mapped to standardized error JSON."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


def _cors_headers_for_request(request: Request, settings: Settings) -> dict[str, str]:
    """Build CORS headers for allowed origins even on outermost 500 error responses."""
    origin = request.headers.get("origin")
    if not origin:
        return {}
    cleaned_origin = origin.strip().rstrip("/")
    normalized_origin = cleaned_origin.lower()
    if not normalized_origin:
        return {}
    allowed_origins = {
        allowed.strip().rstrip("/").lower()
        for allowed in settings.cors_origins
        if allowed.strip()
    }
    if normalized_origin not in allowed_origins:
        return {}
    return {
        "Access-Control-Allow-Origin": cleaned_origin,
        "Access-Control-Allow-Credentials": "true",
        "Vary": "Origin",
    }


def register_exception_handlers(app: FastAPI, settings: Settings | None = None) -> None:
    """Attach standardized JSON exception handlers without leaking internals."""
    resolved_settings = settings or get_settings()

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        payload = ApiErrorResponse(
            error=ErrorDetail(code=exc.code, message=exc.message, details=exc.details)
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=payload.model_dump(),
            headers=_cors_headers_for_request(request, resolved_settings),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        payload = ApiErrorResponse(
            error=ErrorDetail(
                code="VALIDATION_ERROR",
                message="Permintaan tidak valid.",
                details={"error_count": len(exc.errors())},
            )
        )
        return JSONResponse(
            status_code=422,
            content=payload.model_dump(),
            headers=_cors_headers_for_request(request, resolved_settings),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "INTERNAL_ERROR"
        if exc.status_code == 401:
            code = "UNAUTHENTICATED"
        elif exc.status_code == 403:
            code = "FORBIDDEN"
        elif exc.status_code == 409:
            code = "CONFLICT"
        elif exc.status_code == 429:
            code = "RATE_LIMITED"
        payload = ApiErrorResponse(
            error=ErrorDetail(
                code=code,
                message=str(exc.detail) if exc.detail else "Terjadi kesalahan permintaan.",
            )
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=payload.model_dump(),
            headers=_cors_headers_for_request(request, resolved_settings),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, _exc: Exception) -> JSONResponse:
        logger.exception("Unhandled exception during %s %s", request.method, request.url.path)
        payload = ApiErrorResponse(
            error=ErrorDetail(
                code="INTERNAL_ERROR",
                message="Terjadi kesalahan internal pada server.",
            )
        )
        return JSONResponse(
            status_code=500,
            content=payload.model_dump(),
            headers=_cors_headers_for_request(request, resolved_settings),
        )
