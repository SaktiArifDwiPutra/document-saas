"""Replaceable workflow orchestration abstraction and concrete adapters."""

from app.orchestration.base import (
    OrchestratorHealth,
    WorkflowDispatchRequest,
    WorkflowDispatchResult,
    WorkflowInputReference,
    WorkflowOrchestrator,
)
from app.orchestration.n8n_adapter import N8NWorkflowAdapter
from app.orchestration.python_adapter import PythonWorkflowAdapter

__all__ = [
    "N8NWorkflowAdapter",
    "OrchestratorHealth",
    "PythonWorkflowAdapter",
    "WorkflowDispatchRequest",
    "WorkflowDispatchResult",
    "WorkflowInputReference",
    "WorkflowOrchestrator",
]
