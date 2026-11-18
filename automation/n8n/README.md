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

## Exported Workflows

- `meeting_minutes_v1.workflow.json`: Local n8n workflow for the approved first vertical slice (`meeting_minutes`), covering signed `GET /api/v1/internal/source-bundles/{source_bundle_id}`, prompt-injection-separated Gemini generation, and signed `POST /api/v1/internal/workflows/callback`.

## Required Local Credentials & Environment Variables

Configure these in local n8n (never committed to workflow JSON):
- Environment variables in n8n runtime:
  - `INTERNAL_HMAC_SECRET`: Matches backend `INTERNAL_HMAC_SECRET` (`>= 32` chars).
  - `BACKEND_INTERNAL_BASE_URL`: Backend URL reachable from the n8n container (e.g., `http://host.docker.internal:8000`).
- n8n Credential:
  - `Google Gemini API Credential` (`httpHeaderAuth` with header name `x-goog-api-key` and your non-sensitive test Gemini API key).

## Known M6 Limitations & Operational Warnings

M6 is **not production-ready** solely on the strength of passing automated test suites. Keep the following operational boundaries in mind before live or production rollout:

- **PDF Extraction Scope:** The pure-stdlib PDF text extractor (`worker/worker_app/services/normalizer.py` and `backend/app/core/file_validation.py`) supports a defined subset (`FlateDecode` and uncompressed streams, standard single-byte/UTF-16BE BOM strings, and `Tj`/`TJ`/`'`/`"` text-showing operators). Encrypted PDFs, non-Flate stream filters (`LZWDecode`, `ASCII85Decode`), custom `ToUnicode` CMap font remapping, and scanned image-only OCR PDFs are not supported.
- **Provenance Grounding vs. Semantic Truth:** `_validate_draft_provenance` enforces traceability signals (valid bundle `source_id`, normalized excerpt substring presence, and claim-token overlap) rather than a mathematical proof of semantic correctness. Human review and explicit approval (`documents.status == "review_required"` -> manual approval) remain mandatory before final DOCX rendering.
- **Live Gemini API Unverified:** `HttpGeminiProvider` has only been verified against mocked HTTP transports (`httpx.MockTransport`) and the offline deterministic provider, not against live Google Gemini endpoints.
- **Live n8n Execution Unverified:** `meeting_minutes_v1.workflow.json` has been verified only through static structure checks, schema-parity assertions, Node VM JavaScript expression checks, and mocked `N8NWorkflowAdapter` tests; live n8n import and webhook execution remain unverified against a running n8n instance.
- **M9 Scope Deferred:** Durable background queues, exponential retry scheduling, dead-letter handling, R2 presigned object download by the Worker, and worker crash recovery remain out of M6 scope and are deferred to Milestone M9.
