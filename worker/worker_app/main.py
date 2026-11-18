"""FastAPI application for the isolated Python Worker service."""

import base64
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from worker_app.core.config import WorkerSettings, get_worker_settings
from worker_app.core.internal_auth import (
    WorkerAuthError,
    build_worker_callback_headers,
    verify_internal_request,
)
from worker_app.services.normalizer import NormalizationError, normalize_file_bytes


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


class WorkerNormalizeSourceInput(BaseModel):
    """Uploaded source payload delivered to Worker for normalization."""

    source_id: UUID
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=128)
    content_base64: str = Field(min_length=1)


class WorkerNormalizeRequest(BaseModel):
    """Batch source-normalization request bound to an active application job."""

    job_id: UUID
    sources: list[WorkerNormalizeSourceInput] = Field(min_length=1, max_length=10)
    deliver_callback: bool = False
    callback_url: str | None = Field(default=None, max_length=512)


class WorkerNormalizedSourceOutput(BaseModel):
    """Normalized text entry matching FastAPI WorkerCallbackRequest."""

    source_id: UUID
    normalized_text: str
    metadata: dict[str, object]


class WorkerNormalizeCallbackPayload(BaseModel):
    """Callback payload shape prepared for POST /api/v1/internal/workers/callback."""

    job_id: UUID
    job_type: Literal["normalize"] = "normalize"
    event_type: Literal["completed", "failed"]
    external_execution_id: str | None = None
    normalized_sources: list[WorkerNormalizedSourceOutput] = Field(default_factory=list)
    error_code: str | None = None
    safe_error_message: str | None = None


class WorkerNormalizeResponse(BaseModel):
    """Standardized response envelope with signed callback headers and delivery status."""

    success: Literal[True] = True
    data: WorkerNormalizeCallbackPayload
    callback_headers: dict[str, str] = Field(default_factory=dict)
    callback_delivered: bool = False


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

    @application.post("/api/v1/internal/normalize", response_model=WorkerNormalizeResponse)
    async def normalize_sources(
        request: Request,
        active_settings: Annotated[WorkerSettings, Depends(get_worker_settings)],
        x_internal_timestamp: Annotated[str | None, Header()] = None,
        x_internal_event_id: Annotated[str | None, Header()] = None,
        x_internal_signature: Annotated[str | None, Header()] = None,
    ) -> WorkerNormalizeResponse:
        raw_body = await request.body()
        if not x_internal_timestamp or not x_internal_event_id or not x_internal_signature:
            raise HTTPException(status_code=401, detail="Missing internal authentication headers.")
        try:
            ts = int(x_internal_timestamp.strip())
            verify_internal_request(
                secret=active_settings.internal_hmac_secret,
                provided_signature=x_internal_signature,
                timestamp=ts,
                event_id=x_internal_event_id,
                method="POST",
                canonical_path=request.url.path,
                raw_body=raw_body,
            )
        except (ValueError, WorkerAuthError) as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

        payload = WorkerNormalizeRequest.model_validate_json(raw_body)
        exec_id = f"wrk-exec-{uuid4()}"
        normalized_items: list[WorkerNormalizedSourceOutput] = []
        callback: WorkerNormalizeCallbackPayload | None = None

        for item in payload.sources:
            try:
                raw_file = base64.b64decode(item.content_base64, validate=True)
                result = normalize_file_bytes(
                    filename=item.filename,
                    content_type=item.content_type,
                    data=raw_file,
                    settings=active_settings,
                )
            except NormalizationError as exc:
                callback = WorkerNormalizeCallbackPayload(
                    job_id=payload.job_id,
                    event_type="failed",
                    external_execution_id=exec_id,
                    error_code=exc.code,
                    safe_error_message=exc.message,
                )
                break
            except ValueError:
                callback = WorkerNormalizeCallbackPayload(
                    job_id=payload.job_id,
                    event_type="failed",
                    external_execution_id=exec_id,
                    error_code="UPLOAD_REJECTED",
                    safe_error_message="Payload base64 sumber unggahan tidak valid.",
                )
                break
            normalized_items.append(
                WorkerNormalizedSourceOutput(
                    source_id=item.source_id,
                    normalized_text=result.normalized_text,
                    metadata=result.metadata,
                )
            )

        if callback is None:
            callback = WorkerNormalizeCallbackPayload(
                job_id=payload.job_id,
                event_type="completed",
                external_execution_id=exec_id,
                normalized_sources=normalized_items,
            )

        callback_raw_body = callback.model_dump_json().encode("utf-8")
        callback_event_id = f"wrk-cb-{uuid4()}"
        callback_headers = build_worker_callback_headers(
            secret=active_settings.internal_hmac_secret,
            event_id=callback_event_id,
            canonical_path=active_settings.worker_callback_path,
            raw_body=callback_raw_body,
        )

        delivered = False
        if payload.deliver_callback:
            if payload.callback_url is not None:
                parsed = urlsplit(payload.callback_url)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.netloc
                    or parsed.path != active_settings.worker_callback_path
                ):
                    raise HTTPException(status_code=422, detail="Invalid callback_url.")
                target_url = payload.callback_url
            else:
                target_url = (
                    f"{active_settings.backend_internal_base_url.rstrip('/')}"
                    f"{active_settings.worker_callback_path}"
                )

            injected_client = getattr(application.state, "callback_http_client", None)
            try:
                if isinstance(injected_client, httpx.AsyncClient):
                    cb_res = await injected_client.post(
                        target_url,
                        content=callback_raw_body,
                        headers=callback_headers,
                    )
                else:
                    async with httpx.AsyncClient(timeout=10.0) as cb_client:
                        cb_res = await cb_client.post(
                            target_url,
                            content=callback_raw_body,
                            headers=callback_headers,
                        )
            except httpx.HTTPError as exc:
                raise HTTPException(
                    status_code=502,
                    detail="Failed to deliver signed callback to backend.",
                ) from exc
            if cb_res.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"Backend callback rejected with status {cb_res.status_code}.",
                )
            delivered = True

        return WorkerNormalizeResponse(
            data=callback,
            callback_headers=callback_headers,
            callback_delivered=delivered,
        )

    return application


app = create_worker_app()
