"""Document lifecycle, source submission/validation, and job status endpoints (Milestone M5)."""

from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Query, Request

from app.api.v1.deps import (
    get_current_user,
    get_document_service,
    get_gemini_provider,
    get_internal_workflow_service,
    get_n8n_http_client,
    get_object_storage,
    get_worker_http_client,
)
from app.core.config import Settings, get_settings
from app.core.errors import ApiResponse
from app.models.auth import User
from app.schemas.document import (
    DeleteDocumentResponse,
    DocumentCreateRequest,
    DocumentDetailResponse,
    DocumentListResponse,
    DocumentSourceListResponse,
    DocumentSourceResponse,
    DocumentSourceSubmitRequest,
    DocumentUpdateRequest,
    DocumentVersionListResponse,
    JobResponse,
    ProcessDocumentRequest,
    ProcessDocumentResponse,
    UploadIntentRequest,
    UploadIntentResponse,
)
from app.services.document import DocumentService
from app.services.gemini import GeminiProvider
from app.services.internal_workflow import InternalWorkflowService
from app.services.storage import ObjectStorageService

documents_router = APIRouter(tags=["documents"])
jobs_router = APIRouter(prefix="/jobs", tags=["jobs"])


@documents_router.get(
    "/workspaces/{workspace_id}/documents",
    response_model=ApiResponse[DocumentListResponse],
)
async def list_workspace_documents(
    workspace_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ApiResponse[DocumentListResponse]:
    """List workspace documents authorized by the Organization Document Permission Policy."""
    data = await document_service.list_documents(
        current_user,
        workspace_id=workspace_id,
        page=page,
        page_size=page_size,
    )
    return ApiResponse(data=data)


@documents_router.post(
    "/workspaces/{workspace_id}/documents",
    response_model=ApiResponse[DocumentDetailResponse],
    status_code=201,
)
async def create_workspace_document(
    workspace_id: UUID,
    payload: DocumentCreateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> ApiResponse[DocumentDetailResponse]:
    """Create a document container (and optional initial structured draft version)."""
    data = await document_service.create_document(
        current_user,
        workspace_id=workspace_id,
        payload=payload,
    )
    return ApiResponse(data=data)


@documents_router.get(
    "/documents/{document_id}",
    response_model=ApiResponse[DocumentDetailResponse],
)
async def get_document(
    document_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> ApiResponse[DocumentDetailResponse]:
    """Retrieve document metadata, current draft snapshot, and associated jobs."""
    data = await document_service.get_document(current_user, document_id=document_id)
    return ApiResponse(data=data)


@documents_router.patch(
    "/documents/{document_id}",
    response_model=ApiResponse[DocumentDetailResponse],
)
async def update_document(
    document_id: UUID,
    payload: DocumentUpdateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> ApiResponse[DocumentDetailResponse]:
    """Update document metadata or create a new version on structured content edits."""
    data = await document_service.update_document(
        current_user,
        document_id=document_id,
        payload=payload,
    )
    return ApiResponse(data=data)


@documents_router.delete(
    "/documents/{document_id}",
    response_model=ApiResponse[DeleteDocumentResponse],
)
async def delete_document(
    document_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> ApiResponse[DeleteDocumentResponse]:
    """Archive/soft-delete an authorized document."""
    data = await document_service.delete_document(current_user, document_id=document_id)
    return ApiResponse(data=data)


@documents_router.post(
    "/workspaces/{workspace_id}/documents/{document_id}/sources/upload-intents",
    response_model=ApiResponse[UploadIntentResponse],
    status_code=201,
)
async def create_document_source_upload_intent(
    workspace_id: UUID,
    document_id: UUID,
    payload: UploadIntentRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> ApiResponse[UploadIntentResponse]:
    """Authorize upload quotas/MIME/size and return a short-lived presigned upload URL."""
    data = await document_service.create_upload_intent(
        current_user,
        workspace_id=workspace_id,
        document_id=document_id,
        payload=payload,
    )
    return ApiResponse(data=data)


@documents_router.post(
    "/workspaces/{workspace_id}/documents/{document_id}/sources",
    response_model=ApiResponse[DocumentSourceResponse],
    status_code=201,
)
async def submit_document_source(
    workspace_id: UUID,
    document_id: UUID,
    payload: DocumentSourceSubmitRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> ApiResponse[DocumentSourceResponse]:
    """Confirm a validated uploaded source object or submit structured form / raw text."""
    data = await document_service.submit_source(
        current_user,
        workspace_id=workspace_id,
        document_id=document_id,
        payload=payload,
    )
    return ApiResponse(data=data)


@documents_router.get(
    "/documents/{document_id}/sources",
    response_model=ApiResponse[DocumentSourceListResponse],
)
async def list_document_sources(
    document_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> ApiResponse[DocumentSourceListResponse]:
    """List validated sources and provenance summaries for a document."""
    data = await document_service.list_sources(current_user, document_id=document_id)
    return ApiResponse(data=data)


@documents_router.post(
    "/documents/{document_id}/process",
    response_model=ApiResponse[ProcessDocumentResponse],
    status_code=202,
)
async def process_document(
    document_id: UUID,
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
    internal_service: Annotated[InternalWorkflowService, Depends(get_internal_workflow_service)],
    settings: Annotated[Settings, Depends(get_settings)],
    storage: Annotated[ObjectStorageService, Depends(get_object_storage)],
    gemini_provider: Annotated[GeminiProvider, Depends(get_gemini_provider)],
    worker_http_client: Annotated[httpx.AsyncClient | None, Depends(get_worker_http_client)],
    n8n_http_client: Annotated[httpx.AsyncClient | None, Depends(get_n8n_http_client)],
    payload: ProcessDocumentRequest | None = None,
) -> ApiResponse[ProcessDocumentResponse]:
    """Bundle document sources into a source_bundle_id and queue/dispatch processing jobs."""
    data = await document_service.start_processing(
        current_user,
        document_id=document_id,
        payload=payload or ProcessDocumentRequest(),
    )
    if settings.workflow_orchestrator_provider in ("python", "n8n"):
        norm_job = next((job for job in data.jobs if job.job_type == "normalize"), None)
        ai_job = next((job for job in data.jobs if job.job_type == "ai_generation"), None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=request.app),
            base_url="http://internal-backend",
        ) as loopback_client:
            if norm_job is not None and norm_job.status == "queued":
                await internal_service.dispatch_normalize_job(
                    job_id=norm_job.id,
                    loopback_client=loopback_client,
                    storage=storage,
                    worker_http_client=worker_http_client,
                )
            elif (
                ai_job is not None
                and ai_job.status == "queued"
                and (norm_job is None or norm_job.status == "completed")
            ):
                await internal_service.dispatch_ai_generation_job(
                    job_id=ai_job.id,
                    loopback_client=loopback_client,
                    gemini_provider=gemini_provider,
                    n8n_http_client=n8n_http_client,
                )
        data = await document_service.get_processing_snapshot(
            document_id=document_id,
            source_bundle_id=data.source_bundle_id,
        )
    return ApiResponse(data=data)


@documents_router.get(
    "/documents/{document_id}/versions",
    response_model=ApiResponse[DocumentVersionListResponse],
)
async def list_document_versions(
    document_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> ApiResponse[DocumentVersionListResponse]:
    """List immutable version snapshots of a document."""
    data = await document_service.list_versions(current_user, document_id=document_id)
    return ApiResponse(data=data)


@jobs_router.get("/{job_id}", response_model=ApiResponse[JobResponse])
async def get_job_status(
    job_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
) -> ApiResponse[JobResponse]:
    """Return safe public status details for a single asynchronous job."""
    data = await document_service.get_job(current_user, job_id=job_id)
    return ApiResponse(data=data)

