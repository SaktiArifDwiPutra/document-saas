"""Internal service endpoints (source-bundles, callbacks) and workspace usage endpoints (M6)."""

from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, Query, Request

from app.api.v1.deps import (
    get_current_user,
    get_gemini_provider,
    get_internal_workflow_service,
    get_n8n_http_client,
)
from app.core.config import Settings, get_settings
from app.core.errors import ApiResponse
from app.models.auth import User
from app.schemas.ai import (
    InternalCallbackAckResponse,
    SourceBundleResponse,
    UsageListResponse,
    UsageSummaryResponse,
)
from app.services.gemini import GeminiProvider
from app.services.internal_workflow import InternalWorkflowService

internal_router = APIRouter(prefix="/internal", tags=["internal"])
usage_router = APIRouter(tags=["usage"])


@internal_router.get(
    "/source-bundles/{source_bundle_id}",
    response_model=ApiResponse[SourceBundleResponse],
)
async def get_internal_source_bundle(
    source_bundle_id: UUID,
    request: Request,
    internal_service: Annotated[InternalWorkflowService, Depends(get_internal_workflow_service)],
    x_internal_timestamp: Annotated[str | None, Header()] = None,
    x_internal_event_id: Annotated[str | None, Header()] = None,
    x_internal_signature: Annotated[str | None, Header()] = None,
    x_internal_job_id: Annotated[str | None, Header()] = None,
) -> ApiResponse[SourceBundleResponse]:
    """Serve normalized sources for an active job using short-lived HMAC-SHA256 verification."""
    data = await internal_service.get_source_bundle(
        source_bundle_id=source_bundle_id,
        canonical_path=request.url.path,
        timestamp_header=x_internal_timestamp,
        event_id_header=x_internal_event_id,
        signature_header=x_internal_signature,
        job_id_header=x_internal_job_id,
    )
    return ApiResponse(data=data)


@internal_router.post(
    "/workers/callback",
    response_model=ApiResponse[InternalCallbackAckResponse],
)
async def receive_worker_callback(
    request: Request,
    internal_service: Annotated[InternalWorkflowService, Depends(get_internal_workflow_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    gemini_provider: Annotated[GeminiProvider, Depends(get_gemini_provider)],
    n8n_http_client: Annotated[httpx.AsyncClient | None, Depends(get_n8n_http_client)],
    x_internal_timestamp: Annotated[str | None, Header()] = None,
    x_internal_event_id: Annotated[str | None, Header()] = None,
    x_internal_signature: Annotated[str | None, Header()] = None,
) -> ApiResponse[InternalCallbackAckResponse]:
    """Receive signed completion or failure callbacks from the isolated Python Worker."""
    raw_body = await request.body()
    data = await internal_service.handle_worker_callback(
        raw_body=raw_body,
        canonical_path=request.url.path,
        timestamp_header=x_internal_timestamp,
        event_id_header=x_internal_event_id,
        signature_header=x_internal_signature,
    )
    if (
        settings.workflow_orchestrator_provider in ("python", "n8n")
        and data.result_status == "processed"
        and data.job_status == "completed"
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=request.app),
            base_url="http://internal-backend",
        ) as loopback_client:
            await internal_service.dispatch_paired_ai_after_normalize(
                normalize_job_id=data.job_id,
                loopback_client=loopback_client,
                gemini_provider=gemini_provider,
                n8n_http_client=n8n_http_client,
            )
        data = await internal_service.refresh_callback_ack(data=data)
    return ApiResponse(data=data)


@internal_router.post(
    "/workflows/callback",
    response_model=ApiResponse[InternalCallbackAckResponse],
)
async def receive_workflow_callback(
    request: Request,
    internal_service: Annotated[InternalWorkflowService, Depends(get_internal_workflow_service)],
    x_internal_timestamp: Annotated[str | None, Header()] = None,
    x_internal_event_id: Annotated[str | None, Header()] = None,
    x_internal_signature: Annotated[str | None, Header()] = None,
) -> ApiResponse[InternalCallbackAckResponse]:
    """Receive signed AI completion or failure callbacks from n8n or Python fallback."""
    raw_body = await request.body()
    data = await internal_service.handle_workflow_callback(
        raw_body=raw_body,
        canonical_path=request.url.path,
        timestamp_header=x_internal_timestamp,
        event_id_header=x_internal_event_id,
        signature_header=x_internal_signature,
    )
    return ApiResponse(data=data)


@usage_router.get(
    "/workspaces/{workspace_id}/usage",
    response_model=ApiResponse[UsageListResponse],
)
async def list_workspace_usage(
    workspace_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    internal_service: Annotated[InternalWorkflowService, Depends(get_internal_workflow_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ApiResponse[UsageListResponse]:
    """List paginated AI usage telemetry for an authorized workspace member."""
    data = await internal_service.list_workspace_usage(
        current_user,
        workspace_id=workspace_id,
        page=page,
        page_size=page_size,
    )
    return ApiResponse(data=data)


@usage_router.get(
    "/workspaces/{workspace_id}/usage/summary",
    response_model=ApiResponse[UsageSummaryResponse],
)
async def get_workspace_usage_summary(
    workspace_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    internal_service: Annotated[InternalWorkflowService, Depends(get_internal_workflow_service)],
) -> ApiResponse[UsageSummaryResponse]:
    """Return aggregated AI token, cost, and latency telemetry for a workspace."""
    data = await internal_service.get_workspace_usage_summary(
        current_user,
        workspace_id=workspace_id,
    )
    return ApiResponse(data=data)

