"""Document lifecycle, source submission/validation, and job status endpoints (Milestone M5)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.v1.deps import get_current_user, get_document_service
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
    current_user: Annotated[User, Depends(get_current_user)],
    document_service: Annotated[DocumentService, Depends(get_document_service)],
    payload: ProcessDocumentRequest | None = None,
) -> ApiResponse[ProcessDocumentResponse]:
    """Bundle document sources into a source_bundle_id and queue M5 processing jobs."""
    data = await document_service.start_processing(
        current_user,
        document_id=document_id,
        payload=payload or ProcessDocumentRequest(),
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

