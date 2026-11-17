"""WorkflowOrchestrator interface keeping n8n replaceable."""

from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field


class WorkflowInputReference(BaseModel):
    """Minimal reference to normalized source bundle without embedding credentials."""

    source_bundle_id: UUID
    retrieval_endpoint: str


class WorkflowDispatchRequest(BaseModel):
    """Minimal job payload dispatched by FastAPI to an orchestrator adapter."""

    job_id: UUID
    idempotency_key: str
    workflow_key: str
    workflow_version: str
    document_type: str
    input_reference: WorkflowInputReference
    callback_endpoint: str
    issued_at: datetime


class WorkflowDispatchResult(BaseModel):
    """Structured result returned when dispatching a job to an orchestrator."""

    accepted: bool
    provider: str
    external_execution_id: str | None = None
    message: str | None = None


class OrchestratorHealth(BaseModel):
    """Health status reported by an orchestrator adapter."""

    provider: str
    status: Literal["ok", "degraded", "unavailable"]
    detail: str | None = Field(default=None, max_length=256)


class WorkflowOrchestrator(Protocol):
    """Stable interface for workflow orchestration adapters (n8n or Python fallback)."""

    async def dispatch(self, job_payload: WorkflowDispatchRequest) -> WorkflowDispatchResult:
        """Dispatch a minimal job payload to the workflow engine."""
        ...

    async def cancel(self, external_execution_id: str) -> bool:
        """Attempt best-effort cancellation of a running external execution."""
        ...

    async def health_check(self) -> OrchestratorHealth:
        """Return safe health status of the configured orchestrator."""
        ...
