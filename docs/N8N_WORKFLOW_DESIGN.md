# n8n Workflow Design

**Status:** Draft v1.0\
**Role:** Replaceable AI workflow orchestration layer\
**Production licensing:** Not yet approved; see section 12.

## 1. Purpose

n8n coordinates AI document-processing workflows. It is not the SaaS
backend and must not become the system of record.

FastAPI owns user-facing application state, authorization, and all core PostgreSQL database access. Python
Worker owns technical file operations and DOCX rendering, reporting outcomes to FastAPI via `POST /api/v1/internal/workers/callback` without connecting directly to the database.

## 2. Responsibility Boundaries

### n8n may

-   Receive a minimal job payload from FastAPI (containing `job_id`, `source_bundle_id`, and workflow metadata).
-   Retrieve normalized source data from FastAPI via a private internal endpoint (`GET /api/v1/internal/source-bundles/{source_bundle_id}`) using short-lived signed authorization.
-   Route to a document-specific workflow (starting with `meeting_minutes` --- Notulen Rapat as the first vertical slice).
-   Call Gemini through a controlled provider credential.
-   Transform and validate intermediate workflow data.
-   Apply bounded retries for transient provider failures.
-   Return structured result and safe error details.
-   Send a signed callback to FastAPI (`POST /api/v1/internal/workflows/callback`).

### n8n may not

-   Authenticate users.
-   Determine workspace roles or permissions.
-   Read/write credit wallets.
-   Create payment orders or confirm payment.
-   Approve documents.
-   Directly mutate application database tables.
-   Receive or store end-user login/access tokens, database credentials, or wallet balances.
-   Decide that a document is final/approved.
-   Bypass FastAPI result validation.
-   Log unnecessary raw source content in execution history.

## 3. Workflow Keys

Initial logical workflow keys:

-   `document_generation_v1` --- main entry and router.
-   `meeting_minutes_v1` --- meeting minutes (`meeting_minutes` --- Notulen Rapat; **approved first end-to-end vertical slice**).
-   `letter_workflow_v1` --- letters and invitations (implemented only after `meeting_minutes_v1` passes acceptance criteria).
-   `proposal_workflow_v1` --- proposals (subsequent slice).
-   `report_workflow_v1` --- reports and LPJ (subsequent slice).
-   `document_error_handler_v1` --- standardized failure handling.

These keys are logical identifiers. Actual n8n workflow IDs and JSON
exports are created during implementation. Do not expand implementation to other document types before `meeting_minutes` passes its acceptance criteria.

## 4. Main Workflow

``` mermaid
flowchart TD
    A[Receive dispatch payload] --> B[Validate required fields]
    B --> C{Workflow key supported?}
    C -- No --> E[Build safe failure event]
    C -- Yes --> S[Fetch normalized sources via signed internal API]
    S --> D[Route by document type]
    D --> F[Call type-specific workflow]
    F --> G[Normalize structured result]
    G --> H[Build callback event]
    H --> I[Send signed callback to FastAPI]
    I --> J[End]
    E --> I
```

The backend remains responsible for final schema, provenance, state, and
business validation.

## 5. Minimal Dispatch Payload and Signed Internal Source Retrieval

Illustrative dispatch payload shape; finalize exact schema before implementation:

``` json
{
  "job_id": "uuid",
  "idempotency_key": "opaque-key",
  "workflow_key": "meeting_minutes_v1",
  "workflow_version": "1",
  "document_type": "meeting_minutes",
  "input_reference": {
    "source_bundle_id": "uuid",
    "retrieval_endpoint": "/api/v1/internal/source-bundles/{source_bundle_id}"
  },
  "callback": {
    "event_endpoint": "/api/v1/internal/workflows/callback"
  },
  "issued_at": "ISO-8601 timestamp"
}
```

Do not include: - User password or session token. - Access or refresh
tokens. - Wallet balance or payment credentials. - Unnecessary personal
data. - Database credentials. - Long-lived object-storage credentials. - Raw source text inline in the dispatch payload.

### Signed Internal Source Retrieval (`source_bundle_id`)

1.  **FastAPI Pre-Dispatch Validation:** Before dispatching, FastAPI validates user authentication, workspace access, organization document permissions, and credit reservation; creates and persists the durable `ai_generation` job; and generates or identifies a workspace-bound `source_bundle_id`.
2.  **`source_bundle_id` Lifecycle & Workspace Binding:** Each `source_bundle_id` is bound to a specific `(workspace_id, document_id, job_id)` context in PostgreSQL and expires after a short-lived window tied to the job execution attempt.
3.  **Authenticated Retrieval:** n8n retrieves normalized source data from `GET /api/v1/internal/source-bundles/{source_bundle_id}` using short-lived HMAC-SHA256 signed headers. No public unauthenticated source retrieval is permitted.
4.  **Verification & Replay Protection:** FastAPI verifies the HMAC-SHA256 signature (`HMAC-SHA256(secret, "{timestamp}.{event_id}.{METHOD}.{canonical_path}.{raw_body}")`, binding `METHOD="GET"`, exact `canonical_path="/api/v1/internal/source-bundles/{source_bundle_id}"`, `timestamp`, `event_id`, and `raw_body=""`) with constant-time comparison, enforces a **5-minute (300-second)** timestamp tolerance, checks `event_id` replay protection, verifies that the `source_bundle_id` has not expired and matches the active job's workspace, and enforces response/payload size bounds.
5.  **Execution Log Hygiene:** n8n workflows must avoid persisting unnecessary raw source content in execution logs.

## 6. Type-Specific Workflow Requirements

### 6.1 Letters

-   Extract sender, recipient, purpose, date, location, and requested
    action when present.
-   Missing official number/signatory/legal basis remains missing.
-   Do not fabricate institutional authority.

### 6.2 Proposals

-   Route by subtype: activity, funding, cooperation, personal/business.
-   Distinguish required sections by subtype.
-   Budget amounts must be sourced or explicitly marked as estimates
    supplied by the user.
-   Do not invent partners, approvals, or commitments.

### 6.3 Meeting Minutes (`meeting_minutes` --- First Vertical Slice)

`meeting_minutes` (Notulen Rapat) is the first end-to-end document workflow to implement and validate.

The end-to-end workflow and application slice must cover:
1.  Source input and normalization.
2.  Extraction of meeting information (metadata, attendees, absentees, agenda).
3.  Separation of opinions, proposals, and actual decisions.
4.  Identification of action items, responsible persons, and due dates when supported by sources.
5.  Missing and conflicting information detection.
6.  Source provenance for important extracted fields.
7.  Human review and editing.
8.  Explicit user approval.
9.  DOCX rendering using the selected template.
10. Download and document history.

Strict grounding rules:
-   Never invent meeting decisions, attendees, dates, responsible persons, or action items.
-   A suggestion or opinion is not a decision unless the source confirms it.

### 6.4 Reports and LPJ

-   Separate planned activity from actual execution.
-   Expenses require source evidence.
-   Outcomes and participant counts must be supported.
-   Unknown information is marked missing.

## 7. Output Contract

The workflow returns structured JSON matching a versioned Pydantic
schema maintained by the backend team.

Illustrative conceptual fields: - `job_id` - `workflow_key` -
`workflow_version` - `document_type` - `content` - `field_statuses` -
`provenance` - `warnings` - `provider_metadata` (allowlisted,
non-sensitive)

Never return arbitrary executable code, SQL, tool instructions, or
unbounded content.

The backend must validate: - Job identity. - Workflow
identity/version. - Allowed document type. - Schema and field types. -
Maximum lengths and collection sizes. - Provenance references. -
Critical field support. - Permitted job state transition.

## 8. Callback Contract and Security

Workflow callbacks are sent to `POST /api/v1/internal/workflows/callback` (and Python Worker callbacks are sent to `POST /api/v1/internal/workers/callback`).

Callback payload and headers must contain: - Unique event ID (`event_id`). - Job ID (`job_id`). -
Attempt/idempotency key. - Event type (`completed` or `failed`). -
Timestamp (`timestamp`). - Structured result or safe error. - HMAC-SHA256 signature.

Security controls:
1.  **Canonical Signature Format:** `HMAC-SHA256(secret, "{timestamp}.{event_id}.{METHOD}.{canonical_path}.{raw_body}")` (binding uppercase HTTP method, exact request path, timestamp, `event_id`, and raw body; `""` for bodyless `GET` requests).
2.  **Secret Storage:** Secret stored in a secure credential store/environment (never embedded in exported workflow JSON).
3.  **Timestamp Freshness:** Reject requests outside a **5-minute (300-second)** tolerance window.
4.  **Replay Protection:** Reject replayed or duplicate requests using unique `event_id` persistence (`workflow_callback_events`).
5.  **Constant-Time Verification:** Compare HMAC signatures using constant-time digest comparison (`hmac.compare_digest`).
6.  **Payload Size Cap:** Enforce strict maximum payload size limits.
7.  **Idempotency & Job State Validation:** FastAPI validates that `job_id` is in an active state that accepts the transition and processes retries idempotently.
8.  **Credit Commit Boundary:** FastAPI commits reserved AI credits only after the callback is verified, the output schema and provenance are validated, and the reviewable draft is persisted (`documents.status = review_required`).
9.  **Secret Rotation & Log Hygiene:** Support secret rotation (procedure **TBD** in implementation design) and never log raw source text or secrets.

## 9. Retries and Failure Handling

-   Retry only transient failures (e.g., temporary provider/network
    errors).
-   Do not retry invalid schema, unsupported document type, prompt
    policy failure, or permanently invalid input without correction.
-   Use bounded attempts and exponential backoff with jitter where
    supported.
-   Every attempt must be traceable to the application job.
-   Duplicate callbacks must not create duplicate drafts or credit
    transactions.
-   FastAPI/queue owns durable retry and reconciliation decisions.
-   n8n's execution history is diagnostic, not the sole durable job
    record.

Failure categories: - `INVALID_INPUT` - `UNSUPPORTED_DOCUMENT_TYPE` -
`AI_PROVIDER_TIMEOUT` - `AI_PROVIDER_RATE_LIMIT` - `AI_PROVIDER_ERROR` -
`OUTPUT_SCHEMA_INVALID` - `CALLBACK_DELIVERY_FAILED` -
`WORKFLOW_CONFIGURATION_ERROR`

Return safe user-facing messages separately from internal diagnostics.

## 10. Prompt Injection and Data Handling

Treat all user-provided content and uploaded file text as untrusted
data.

-   Instructions embedded in source documents must not override system
    workflow rules.
-   Do not allow documents to request secrets, credentials, or
    unauthorized actions.
-   AI prompts must clearly separate trusted task instructions from
    untrusted source content.
-   Do not give the AI database, payment, filesystem, or unrestricted
    network tools.
-   Minimize source content in execution logs.
-   Configure execution-data pruning and retention.
-   Restrict access to the n8n editor.
-   Use a strong encryption key and secure credentials.
-   Avoid unreviewed community nodes.

## 11. Local Development Plan

1.  Run n8n locally with Docker.
2.  Keep editor bound to localhost.
3.  Use dummy documents and non-sensitive data.
4.  Configure a test Gemini credential only when needed.
5.  Build a simple webhook-triggered workflow.
6.  Test a single structured output.
7.  Integrate FastAPI dispatch.
8.  Implement secured callback.
9.  Test duplicate callback and timeout behavior.
10. Export workflow JSON into `automation/n8n/` with a README describing
    credentials/configuration required.

Do not commit credentials or `.env` files.

## 12. Licensing and Production Gate

n8n Community Edition is suitable for local learning and prototyping
under its applicable license terms. However, the intended product is a
SaaS processing documents for external organizations. The exact business
model may raise licensing requirements.

Before using n8n in a monetized/public production service: - Review the
current official n8n license and FAQ. - Describe the exact product flow
and whether customers can access, configure, or interact with n8n. -
Obtain written clarification from n8n where needed. - Do not assume
self-hosting Community Edition is automatically permitted for every SaaS
use case. - If licensing is unsuitable or unclear, use the Python
fallback orchestrator or obtain the appropriate commercial arrangement.

Licensing approval is a production release gate, not a blocker for local
learning.

## 13. Hosting Decision

Production hosting is **TBD**.

Options to evaluate: - Dedicated VPS/container host. - Managed n8n cloud
service, subject to pricing and licensing. - Cloud Run or another
container platform, only after testing persistent runtime, database,
webhooks, restarts, and execution reliability.

Do not assume a sleeping free web service is suitable for production
workflow orchestration. Do not assume a scale-to-zero platform is
suitable without testing.

## 14. Workflow Definition of Done

-   Workflow is exported and version-controlled.
-   No secrets are embedded in export.
-   Input and output schemas are documented.
-   Valid and invalid inputs tested.
-   Duplicate dispatch/callback tested.
-   Provider timeout/rate limit tested.
-   Safe failure callback tested.
-   Prompt-injection sample tested.
-   Execution data retention reviewed.
-   FastAPI validates all outputs.
-   Python fallback path remains available.
