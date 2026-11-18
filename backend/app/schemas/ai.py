"""Pydantic schemas for internal source bundles, meeting_minutes AI drafts, and callbacks (M6)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.document import DocumentStatus, DocumentType, JobStatus, SourceType

FieldStatus = Literal["grounded", "missing", "conflicting"]

WorkflowFailureCode = Literal[
    "INVALID_INPUT",
    "UNSUPPORTED_DOCUMENT_TYPE",
    "AI_PROVIDER_TIMEOUT",
    "AI_PROVIDER_RATE_LIMIT",
    "AI_PROVIDER_ERROR",
    "OUTPUT_SCHEMA_INVALID",
    "CALLBACK_DELIVERY_FAILED",
    "WORKFLOW_CONFIGURATION_ERROR",
]


class SourceBundleItem(BaseModel):
    """Single validated normalized source entry inside a job-bound source bundle."""

    source_id: UUID
    source_type: SourceType
    source_name: str
    content_hash: str
    normalized_text: str
    metadata: dict[str, object]


class SourceBundleResponse(BaseModel):
    """Workspace- and job-bound source bundle served to orchestrators via signed GET."""

    source_bundle_id: UUID
    workspace_id: UUID
    document_id: UUID
    job_id: UUID
    document_type: DocumentType
    title: str
    expires_at: datetime
    sources: list[SourceBundleItem]


class ProvenanceEntry(BaseModel):
    """Source attribution linking an extracted field path back to its supporting source."""

    model_config = ConfigDict(extra="forbid")

    field_path: str = Field(min_length=1, max_length=120)
    source_id: UUID
    source_name: str = Field(min_length=1, max_length=255)
    source_type: SourceType
    excerpt: str = Field(min_length=1, max_length=500)


class OpinionItem(BaseModel):
    """Subjective viewpoint expressed in the meeting (strictly distinct from decisions)."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=1000)
    speaker: str | None = Field(default=None, max_length=120)
    source_id: UUID
    excerpt: str = Field(min_length=1, max_length=500)


class ProposalItem(BaseModel):
    """Suggestion or option raised during the meeting without final approval."""

    model_config = ConfigDict(extra="forbid")

    proposal: str = Field(min_length=1, max_length=1000)
    proposer: str | None = Field(default=None, max_length=120)
    status: Literal["proposed", "deferred"] = "proposed"
    source_id: UUID
    excerpt: str = Field(min_length=1, max_length=500)


class DecisionItem(BaseModel):
    """Actual approved meeting decision explicitly supported by source text."""

    model_config = ConfigDict(extra="forbid")

    decision: str = Field(min_length=1, max_length=1000)
    source_id: UUID
    excerpt: str = Field(min_length=1, max_length=500)


class ActionItem(BaseModel):
    """Follow-up task grounded in source text; assignee/due_date stay null if absent."""

    model_config = ConfigDict(extra="forbid")

    task: str = Field(min_length=1, max_length=1000)
    assignee: str | None = Field(default=None, max_length=120)
    due_date: str | None = Field(default=None, max_length=64)
    source_id: UUID
    excerpt: str = Field(min_length=1, max_length=500)


class ConflictItem(BaseModel):
    """Detected contradiction across source inputs requiring human review."""

    model_config = ConfigDict(extra="forbid")

    field_path: str = Field(min_length=1, max_length=120)
    values: list[str] = Field(min_length=2, max_length=20)
    source_ids: list[UUID] = Field(min_length=1, max_length=20)
    description: str = Field(min_length=1, max_length=500)


class MeetingMinutesDraftContent(BaseModel):
    """Structured Notulen Rapat draft payload validated strictly before persistence."""

    model_config = ConfigDict(extra="forbid")

    meeting_title: str | None = Field(default=None, max_length=255)
    meeting_date: str | None = Field(default=None, max_length=64)
    meeting_time: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=255)
    organizer: str | None = Field(default=None, max_length=255)
    chairperson: str | None = Field(default=None, max_length=120)
    attendees: list[str] = Field(default_factory=list, max_length=200)
    absentees: list[str] = Field(default_factory=list, max_length=200)
    agenda_items: list[str] = Field(default_factory=list, max_length=200)
    discussion_summary: str | None = Field(default=None, max_length=10_000)
    opinions: list[OpinionItem] = Field(default_factory=list, max_length=100)
    proposals: list[ProposalItem] = Field(default_factory=list, max_length=100)
    decisions: list[DecisionItem] = Field(default_factory=list, max_length=100)
    action_items: list[ActionItem] = Field(default_factory=list, max_length=100)
    field_statuses: dict[str, FieldStatus] = Field(default_factory=dict)
    conflicts: list[ConflictItem] = Field(default_factory=list, max_length=50)
    provenance: list[ProvenanceEntry] = Field(default_factory=list, max_length=300)
    warnings: list[str] = Field(default_factory=list, max_length=50)


class ProviderUsageMetadata(BaseModel):
    """Non-sensitive token/timing telemetry emitted by an AI provider adapter."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(default="gemini", min_length=1, max_length=64)
    model: str = Field(default="gemini-2.5-flash", min_length=1, max_length=128)
    input_tokens: int = Field(default=0, ge=0, le=10_000_000)
    output_tokens: int = Field(default=0, ge=0, le=10_000_000)
    estimated_cost_minor_units: int = Field(default=0, ge=0, le=100_000_000)
    duration_ms: int = Field(default=0, ge=0, le=3_600_000)


class WorkflowCallbackRequest(BaseModel):
    """Signed internal callback payload sent by n8n or Python fallback orchestrator."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    workflow_key: str = Field(min_length=1, max_length=128)
    workflow_version: str = Field(min_length=1, max_length=32)
    document_type: str = Field(min_length=1, max_length=64)
    event_type: Literal["completed", "failed"]
    external_execution_id: str | None = Field(default=None, max_length=128)
    result: MeetingMinutesDraftContent | None = None
    error_code: WorkflowFailureCode | None = None
    safe_error_message: str | None = Field(default=None, max_length=255)
    provider_metadata: ProviderUsageMetadata | None = None

    @model_validator(mode="after")
    def validate_event_fields(self) -> "WorkflowCallbackRequest":
        """Require structured result on completed events and reject result on failures."""
        if self.event_type == "completed" and self.result is None:
            raise ValueError("Callback completed wajib menyertakan payload result.")
        if self.event_type == "failed" and self.result is not None:
            raise ValueError("Callback failed tidak boleh menyertakan payload result.")
        return self


class NormalizedSourceItem(BaseModel):
    """Normalized source text produced by Python Worker for an uploaded source record."""

    model_config = ConfigDict(extra="forbid")

    source_id: UUID
    normalized_text: str = Field(max_length=200_000)
    metadata: dict[str, object] = Field(default_factory=dict)


class WorkerCallbackRequest(BaseModel):
    """Signed internal callback payload sent by isolated Python Worker."""

    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    job_type: Literal["normalize", "docx_render"]
    event_type: Literal["completed", "failed"]
    external_execution_id: str | None = Field(default=None, max_length=128)
    normalized_sources: list[NormalizedSourceItem] = Field(default_factory=list, max_length=10)
    output_object_key: str | None = Field(default=None, max_length=512)
    error_code: str | None = Field(default=None, max_length=64)
    safe_error_message: str | None = Field(default=None, max_length=255)


class InternalCallbackAckResponse(BaseModel):
    """Acknowledgment returned by internal workflow and worker callback endpoints."""

    event_id: str
    job_id: UUID
    job_status: JobStatus
    document_status: DocumentStatus
    result_status: Literal["processed", "ignored_duplicate"]


class UsageEventResponse(BaseModel):
    """Public non-sensitive usage telemetry entry for a workspace."""

    id: UUID
    workspace_id: UUID
    user_id: UUID | None
    document_id: UUID | None
    job_id: UUID | None
    provider: str
    model: str
    workflow_key: str
    input_tokens: int
    output_tokens: int
    estimated_cost_minor_units: int
    duration_ms: int
    status: Literal["completed", "failed"]
    created_at: datetime


class UsageListResponse(BaseModel):
    """Paginated workspace usage events list."""

    items: list[UsageEventResponse]
    total: int
    page: int
    page_size: int


class UsageSummaryResponse(BaseModel):
    """Aggregated non-sensitive AI usage summary for a workspace."""

    workspace_id: UUID
    total_events: int
    completed_events: int
    failed_events: int
    total_input_tokens: int
    total_output_tokens: int
    total_estimated_cost_minor_units: int
    total_duration_ms: int
