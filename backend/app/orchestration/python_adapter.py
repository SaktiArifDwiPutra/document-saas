"""Python fallback orchestrator adapter executing grounded AI workflows via internal API."""

import time
from uuid import uuid4

import httpx

from app.core.config import Settings
from app.core.internal_auth import compute_internal_hmac
from app.orchestration.base import (
    OrchestratorHealth,
    WorkflowDispatchRequest,
    WorkflowDispatchResult,
)
from app.schemas.ai import (
    SourceBundleResponse,
    WorkflowCallbackRequest,
)
from app.services.gemini import GeminiProvider, GeminiProviderError

_SUPPORTED_WORKFLOW_KEYS = frozenset({"document_generation_v1", "meeting_minutes_v1"})


class PythonWorkflowAdapter:
    """In-Python WorkflowOrchestrator adapter matching n8n's signed API contract."""

    def __init__(
        self,
        *,
        settings: Settings,
        gemini_provider: GeminiProvider,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._settings = settings
        self._gemini = gemini_provider
        self._client = http_client

    def _signed_headers(
        self,
        *,
        method: str,
        canonical_path: str,
        raw_body: bytes,
        event_id: str,
        job_id: str | None = None,
    ) -> dict[str, str]:
        ts = int(time.time())
        sig = compute_internal_hmac(
            secret=self._settings.internal_hmac_secret,
            timestamp=ts,
            event_id=event_id,
            method=method,
            canonical_path=canonical_path,
            raw_body=raw_body,
        )
        headers = {
            "X-Internal-Timestamp": str(ts),
            "X-Internal-Event-Id": event_id,
            "X-Internal-Signature": sig,
        }
        if job_id is not None:
            headers["X-Internal-Job-Id"] = job_id
        if raw_body:
            headers["Content-Type"] = "application/json"
        return headers

    async def _send_callback(
        self,
        *,
        callback_endpoint: str,
        payload: WorkflowCallbackRequest,
    ) -> None:
        raw_body = payload.model_dump_json().encode("utf-8")
        event_id = f"pywf-{uuid4()}"
        headers = self._signed_headers(
            method="POST",
            canonical_path=callback_endpoint,
            raw_body=raw_body,
            event_id=event_id,
        )
        res = await self._client.post(callback_endpoint, content=raw_body, headers=headers)
        res.raise_for_status()

    async def dispatch(self, job_payload: WorkflowDispatchRequest) -> WorkflowDispatchResult:
        """Fetch signed source bundle, call GeminiProvider, and submit signed callback."""
        exec_id = f"pyexec-{uuid4()}"
        if (
            job_payload.workflow_key not in _SUPPORTED_WORKFLOW_KEYS
            or job_payload.document_type != "meeting_minutes"
        ):
            failure = WorkflowCallbackRequest(
                job_id=job_payload.job_id,
                workflow_key=job_payload.workflow_key,
                workflow_version=job_payload.workflow_version,
                document_type=job_payload.document_type,
                event_type="failed",
                external_execution_id=exec_id,
                error_code="UNSUPPORTED_DOCUMENT_TYPE",
                safe_error_message="Jenis dokumen atau workflow belum didukung.",
            )
            await self._send_callback(
                callback_endpoint=job_payload.callback_endpoint,
                payload=failure,
            )
            return WorkflowDispatchResult(
                accepted=True,
                provider="python_fallback",
                external_execution_id=exec_id,
                message="Unsupported workflow reported via signed failure callback.",
            )

        retrieval_path = job_payload.input_reference.retrieval_endpoint
        get_headers = self._signed_headers(
            method="GET",
            canonical_path=retrieval_path,
            raw_body=b"",
            event_id=f"pybundle-{uuid4()}",
            job_id=str(job_payload.job_id),
        )
        bundle_res = await self._client.get(retrieval_path, headers=get_headers)
        if bundle_res.status_code >= 400:
            failure = WorkflowCallbackRequest(
                job_id=job_payload.job_id,
                workflow_key=job_payload.workflow_key,
                workflow_version=job_payload.workflow_version,
                document_type=job_payload.document_type,
                event_type="failed",
                external_execution_id=exec_id,
                error_code="INVALID_INPUT",
                safe_error_message="Bundel sumber dokumen tidak siap atau tidak valid.",
            )
            await self._send_callback(
                callback_endpoint=job_payload.callback_endpoint,
                payload=failure,
            )
            return WorkflowDispatchResult(
                accepted=True,
                provider="python_fallback",
                external_execution_id=exec_id,
                message="Source bundle retrieval failure reported via signed callback.",
            )

        bundle = SourceBundleResponse.model_validate(bundle_res.json()["data"])
        try:
            ai_output = await self._gemini.generate_meeting_minutes(bundle)
            callback_payload = WorkflowCallbackRequest(
                job_id=job_payload.job_id,
                workflow_key=job_payload.workflow_key,
                workflow_version=job_payload.workflow_version,
                document_type=job_payload.document_type,
                event_type="completed",
                external_execution_id=exec_id,
                result=ai_output.draft,
                provider_metadata=ai_output.usage,
            )
        except GeminiProviderError as exc:
            callback_payload = WorkflowCallbackRequest(
                job_id=job_payload.job_id,
                workflow_key=job_payload.workflow_key,
                workflow_version=job_payload.workflow_version,
                document_type=job_payload.document_type,
                event_type="failed",
                external_execution_id=exec_id,
                error_code=exc.code,
                safe_error_message=exc.safe_message,
            )

        await self._send_callback(
            callback_endpoint=job_payload.callback_endpoint,
            payload=callback_payload,
        )
        return WorkflowDispatchResult(
            accepted=True,
            provider="python_fallback",
            external_execution_id=exec_id,
        )

    async def cancel(self, external_execution_id: str) -> bool:
        """Python fallback executes inline per dispatch; nothing persistent to cancel."""
        return bool(external_execution_id)

    async def health_check(self) -> OrchestratorHealth:
        """Python fallback orchestrator is available in-process."""
        return OrchestratorHealth(
            provider="python_fallback",
            status="ok",
            detail="In-process Python orchestrator ready.",
        )

