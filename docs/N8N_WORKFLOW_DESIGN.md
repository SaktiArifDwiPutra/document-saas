# n8n Workflow Design

**Status:** Draft v1.0\
**Role:** Replaceable AI workflow orchestration layer\
**Production licensing:** Not yet approved; see section 12.

## 1. Purpose

n8n coordinates AI document-processing workflows. It is not the SaaS
backend and must not become the system of record.

FastAPI owns user-facing application state and authorization. Python
Worker owns technical file operations and DOCX rendering.

## 2. Responsibility Boundaries

### n8n may

-   Receive a minimal job payload from FastAPI.
-   Route to a document-specific workflow.
-   Call Gemini through a controlled provider credential.
-   Transform and validate intermediate workflow data.
-   Apply bounded retries for transient provider failures.
-   Return structured result and safe error details.
-   Send a signed callback to FastAPI.

### n8n may not

-   Authenticate users.
-   Determine workspace roles or permissions.
-   Read/write credit wallets.
-   Create payment orders or confirm payment.
-   Approve documents.
-   Directly mutate application database tables.
-   Store end-user access tokens.
-   Decide that a document is final/approved.
-   Bypass FastAPI result validation.

## 3. Workflow Keys

Initial logical workflow keys:

-   `document_generation_v1` --- main entry and router.
-   `letter_workflow_v1` --- letters and invitations.
-   `proposal_workflow_v1` --- proposals.
-   `meeting_minutes_v1` --- meeting minutes.
-   `report_workflow_v1` --- reports and LPJ.
-   `document_error_handler_v1` --- standardized failure handling.

These keys are logical identifiers. Actual n8n workflow IDs and JSON
exports are created during implementation.

## 4. Main Workflow

``` mermaid
flowchart TD
    A[Receive dispatch payload] --> B[Validate required fields]
    B --> C{Workflow key supported?}
    C -- No --> E[Build safe failure event]
    C -- Yes --> D[Route by document type]
    D --> F[Call type-specific workflow]
    F --> G[Normalize structured result]
    G --> H[Build callback event]
    H --> I[Send callback to FastAPI]
    I --> J[End]
    E --> I
```

The backend remains responsible for final schema, provenance, state, and
business validation.

## 5. Minimal Dispatch Payload

Illustrative shape only; finalize exact schema before implementation:

``` json
{
  "job_id": "uuid",
  "idempotency_key": "opaque-key",
  "workflow_key": "meeting_minutes_v1",
  "workflow_version": "1",
  "document_type": "meeting_minutes",
  "input_reference": {
    "source_bundle_id": "uuid"
  },
  "callback": {
    "event_endpoint": "configured-internal-endpoint"
  },
  "issued_at": "ISO-8601 timestamp"
}
```

Do not include: - User password or session token. - Access or refresh
tokens. - Wallet balance or payment credentials. - Unnecessary personal
data. - Database credentials. - Long-lived object-storage credentials.

If source text must be supplied, use the minimum required content or a
short-lived backend-controlled retrieval mechanism. The choice is
**TBD** and must consider privacy, payload size, and n8n execution-data
retention.

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

### 6.3 Meeting Minutes

Represent separately: - Meeting metadata. - Attendees and absentees. -
Agenda. - Discussion points. - Proposals/opinions. - Actual decisions. -
Action items, owners, and due dates.

A suggestion is not a decision unless the source confirms it.

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

Callback should contain: - Unique event ID. - Job ID. -
Attempt/idempotency key. - Event type (`completed` or `failed`). -
Timestamp. - Structured result or safe error. - HMAC signature.

Security controls: 1. HMAC over a canonical request body and timestamp.
2. Secret stored in a secure credential store/environment. 3. Timestamp
freshness check. 4. Replay detection using unique event ID. 5.
Constant-time signature comparison. 6. Payload size cap. 7. Callback
idempotency. 8. FastAPI validates job state before processing. 9. Rotate
secrets without exposing them in workflow exports. 10. Do not log source
text or secrets.

Exact signature format and key rotation procedure are **TBD** before
implementation.

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
