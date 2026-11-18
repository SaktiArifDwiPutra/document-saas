"""n8n webhook orchestrator adapter sending minimal signed job dispatch payloads."""

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


class N8NWorkflowAdapter:
    """WorkflowOrchestrator adapter dispatching minimal job references to local/hosted n8n."""

    def __init__(
        self,
        *,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = http_client

    async def dispatch(self, job_payload: WorkflowDispatchRequest) -> WorkflowDispatchResult:
        """Send signed minimal WorkflowDispatchRequest to n8n webhook without raw sources."""
        webhook_path = f"/webhook/{job_payload.workflow_key}"
        url = f"{self._settings.n8n_base_url.rstrip('/')}{webhook_path}"
        raw_body = job_payload.model_dump_json().encode("utf-8")
        ts = int(time.time())
        event_id = f"n8ndisp-{uuid4()}"
        sig = compute_internal_hmac(
            secret=self._settings.internal_hmac_secret,
            timestamp=ts,
            event_id=event_id,
            method="POST",
            canonical_path=webhook_path,
            raw_body=raw_body,
        )
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Timestamp": str(ts),
            "X-Internal-Event-Id": event_id,
            "X-Internal-Signature": sig,
        }

        async def _post(client: httpx.AsyncClient) -> WorkflowDispatchResult:
            try:
                res = await client.post(url, content=raw_body, headers=headers)
            except httpx.HTTPError:
                return WorkflowDispatchResult(
                    accepted=False,
                    provider="n8n",
                    message="Layanan orkestrasi n8n tidak dapat dijangkau.",
                )
            if res.status_code >= 400:
                return WorkflowDispatchResult(
                    accepted=False,
                    provider="n8n",
                    message="Orkestrasi n8n menolak permintaan dispatch.",
                )
            ext_id: str | None = None
            try:
                body = res.json()
                if isinstance(body, dict) and isinstance(body.get("executionId"), str):
                    ext_id = body["executionId"]
            except ValueError:
                ext_id = None
            return WorkflowDispatchResult(
                accepted=True,
                provider="n8n",
                external_execution_id=ext_id,
            )

        if self._client is not None:
            return await _post(self._client)

        async with httpx.AsyncClient(timeout=self._settings.n8n_timeout_seconds) as client:
            return await _post(client)

    async def cancel(self, external_execution_id: str) -> bool:
        """Best-effort cancellation hook for external n8n executions."""
        return bool(external_execution_id)

    async def health_check(self) -> OrchestratorHealth:
        """Check configured n8n /healthz endpoint without raising on connection errors."""
        url = f"{self._settings.n8n_base_url.rstrip('/')}/healthz"

        async def _check(client: httpx.AsyncClient) -> OrchestratorHealth:
            try:
                res = await client.get(url)
            except httpx.HTTPError:
                return OrchestratorHealth(
                    provider="n8n",
                    status="unavailable",
                    detail="n8n health endpoint unreachable.",
                )
            if res.status_code == 200:
                return OrchestratorHealth(provider="n8n", status="ok")
            return OrchestratorHealth(
                provider="n8n",
                status="degraded",
                detail=f"Unexpected status {res.status_code}",
            )

        if self._client is not None:
            return await _check(self._client)

        async with httpx.AsyncClient(timeout=self._settings.n8n_timeout_seconds) as client:
            return await _check(client)

