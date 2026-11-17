# n8n Local Automation Directory

This directory holds version-controlled n8n workflow exports (introduced in M6+) and local configuration notes.

> [!WARNING]
> **Production Licensing Gate:** n8n Community Edition is used here for local prototyping and development only. It is **not** approved for monetized external SaaS production without separate licensing review and Product Owner confirmation. The backend maintains the `WorkflowOrchestrator` abstraction so a Python fallback adapter can replace n8n if production licensing or hosting is unsuitable.

## Rules for Workflow Exports

1. **Never embed secrets:** Strip API keys, HMAC secrets, webhook credentials, and environment passwords before exporting any workflow JSON.
2. **Never connect to PostgreSQL:** n8n must never hold application database credentials or query/mutate core SaaS tables directly.
3. **Signed internal source retrieval:** Workflows retrieve normalized source material exclusively via `GET /api/v1/internal/source-bundles/{source_bundle_id}` using short-lived HMAC-SHA256 headers.
4. **Signed callbacks:** Workflows report completion or safe error outcomes to `POST /api/v1/internal/workflows/callback` using `HMAC-SHA256(secret, "{timestamp}.{event_id}.{METHOD}.{canonical_path}.{raw_body}")`.
5. **Log hygiene:** Keep `EXECUTIONS_DATA_SAVE_ON_SUCCESS=none` and avoid logging raw user source text in execution history.
6. **First vertical slice:** M6 implements `meeting_minutes` (Notulen Rapat) first before expanding to other document types.
