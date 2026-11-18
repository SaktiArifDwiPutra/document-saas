"""Pydantic request and response schemas for documents, sources, and jobs (Milestone M5)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

DocumentType = Literal["meeting_minutes"]
DocumentStatus = Literal[
    "draft",
    "processing",
    "review_required",
    "approved",
    "rendering",
    "completed",
    "failed",
    "archived",
]
ApprovalStatus = Literal["pending", "approved", "superseded", "rejected"]
SourceType = Literal["form", "text", "upload", "workspace_reference"]
SourceValidationStatus = Literal["pending", "valid", "rejected"]
JobType = Literal["normalize", "ai_generation", "docx_render"]
JobStatus = Literal["queued", "running", "validating", "completed", "failed", "cancelled"]


def _strip_non_empty(value: str, field_label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_label} tidak boleh kosong.")
    return cleaned


class DocumentCreateRequest(BaseModel):
    """Payload to create a workspace document container."""

    title: str = Field(min_length=1, max_length=255)
    document_type: DocumentType = "meeting_minutes"
    selected_template_version_id: UUID | None = None
    initial_content: dict[str, object] | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        """Trim title and reject whitespace-only strings."""
        return _strip_non_empty(value, "Judul dokumen")


class DocumentUpdateRequest(BaseModel):
    """Payload to update editable document metadata or structured content."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    structured_content: dict[str, object] | None = None
    selected_template_version_id: UUID | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        """Trim optional title when supplied."""
        if value is None:
            return None
        return _strip_non_empty(value, "Judul dokumen")


class UploadIntentRequest(BaseModel):
    """Request a short-lived presigned upload URL bound to a document source."""

    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=1)

    @field_validator("filename", "content_type")
    @classmethod
    def validate_non_blank(cls, value: str) -> str:
        """Trim upload metadata strings."""
        return _strip_non_empty(value, "Metadata unggahan")


class MeetingMinutesFormInput(BaseModel):
    """Structured form input schema for meeting_minutes source submissions."""

    meeting_title: str = Field(min_length=1, max_length=255)
    meeting_date: str | None = Field(default=None, max_length=64)
    meeting_time: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=255)
    organizer: str | None = Field(default=None, max_length=255)
    attendees: list[str] = Field(default_factory=list, max_length=200)
    agenda_items: list[str] = Field(default_factory=list, max_length=200)
    notes: str | None = Field(default=None, max_length=100_000)
    decisions_raw: list[str] = Field(default_factory=list, max_length=200)
    action_items_raw: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("meeting_title")
    @classmethod
    def validate_meeting_title(cls, value: str) -> str:
        """Require non-blank meeting title."""
        return _strip_non_empty(value, "Judul rapat")


class DocumentSourceSubmitRequest(BaseModel):
    """Confirm an uploaded file or submit structured form / raw text source."""

    source_type: Literal["upload", "form", "text"]
    source_name: str | None = Field(default=None, max_length=255)

    # Upload confirmation fields
    object_key: str | None = Field(default=None, max_length=512)
    filename: str | None = Field(default=None, max_length=255)
    content_type: str | None = Field(default=None, max_length=128)

    # Structured form input
    form_data: MeetingMinutesFormInput | None = None

    # Raw text input
    raw_text: str | None = Field(default=None, min_length=1, max_length=200_000)

    @model_validator(mode="after")
    def validate_variant_fields(self) -> "DocumentSourceSubmitRequest":
        """Enforce required payload fields strictly matching source_type."""
        if self.source_type == "upload" and (
            not self.object_key or not self.filename or not self.content_type
        ):
            raise ValueError(
                "Unggahan membutuhkan object_key, filename, dan content_type."
            )
        if self.source_type == "form" and self.form_data is None:
            raise ValueError("Sumber formulir membutuhkan form_data.")
        if self.source_type == "text" and (
            self.raw_text is None or not self.raw_text.strip()
        ):
            raise ValueError("Sumber teks membutuhkan raw_text yang tidak kosong.")
        return self


class ProcessDocumentRequest(BaseModel):
    """Request to bundle sources and queue asynchronous normalization / AI jobs."""

    idempotency_key: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("idempotency_key")
    @classmethod
    def validate_key(cls, value: str | None) -> str | None:
        """Trim optional idempotency key."""
        if value is None:
            return None
        return _strip_non_empty(value, "Idempotency key")


class DocumentVersionResponse(BaseModel):
    """Serialized immutable document draft version."""

    id: UUID
    document_id: UUID
    version_number: int
    structured_content: dict[str, object]
    content_hash: str
    created_by_user_id: UUID | None
    created_at: datetime
    approval_status: ApprovalStatus
    approved_by_user_id: UUID | None
    approved_at: datetime | None


class DocumentSourceResponse(BaseModel):
    """Serialized document source and provenance summary."""

    id: UUID
    workspace_id: UUID
    document_id: UUID
    document_version_id: UUID | None
    source_bundle_id: UUID | None
    source_type: SourceType
    has_stored_object: bool
    source_name: str
    content_hash: str
    validation_status: SourceValidationStatus
    metadata: dict[str, object]
    extracted_text_preview: str | None
    created_by_user_id: UUID | None
    created_at: datetime


class JobResponse(BaseModel):
    """Safe public representation of an asynchronous job."""

    id: UUID
    workspace_id: UUID
    document_id: UUID
    job_type: JobType
    status: JobStatus
    source_bundle_id: UUID | None
    workflow_key: str
    workflow_version: str
    workflow_provider: str
    attempt_count: int
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    safe_error_message: str | None
    created_at: datetime
    updated_at: datetime


class DocumentSummaryResponse(BaseModel):
    """Document container summary without full draft JSON payload."""

    id: UUID
    workspace_id: UUID
    created_by_user_id: UUID | None
    document_type: str
    title: str
    status: DocumentStatus
    current_version_id: UUID | None
    selected_template_version_id: UUID | None
    approved_version_id: UUID | None
    created_at: datetime
    updated_at: datetime


class DocumentDetailResponse(DocumentSummaryResponse):
    """Document detail including current version snapshot, source count, and jobs."""

    current_version: DocumentVersionResponse | None
    sources_count: int
    jobs: list[JobResponse]


class DocumentListResponse(BaseModel):
    """Paginated workspace document listing."""

    items: list[DocumentSummaryResponse]
    total: int
    page: int
    page_size: int


class DocumentVersionListResponse(BaseModel):
    """Ordered history of immutable document versions."""

    items: list[DocumentVersionResponse]
    total: int


class DocumentSourceListResponse(BaseModel):
    """Ordered list of sources linked to a document."""

    items: list[DocumentSourceResponse]
    total: int


class UploadIntentResponse(BaseModel):
    """Presigned direct-to-R2 upload intent metadata."""

    object_key: str
    upload_url: str
    method: str
    required_headers: dict[str, str]
    expires_at: datetime
    max_file_size_bytes: int


class ProcessDocumentResponse(BaseModel):
    """Result of queueing document normalization and AI generation jobs."""

    document: DocumentSummaryResponse
    source_bundle_id: UUID
    jobs: list[JobResponse]
    reservation_id: UUID | None
    reserved_credits: int


class DeleteDocumentResponse(BaseModel):
    """Soft-delete / archive confirmation for a document."""

    id: UUID
    status: Literal["archived"]
    deleted_at: datetime

