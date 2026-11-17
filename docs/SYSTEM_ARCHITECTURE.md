# System Architecture

**Status:** Draft v1.0\
**Architecture style:** Modular monolith core + asynchronous workers +
replaceable workflow orchestration.

## 1. Goals

-   Keep FastAPI as the single application authority.
-   Use n8n for workflow orchestration without making it the system of
    record.
-   Keep file processing and DOCX rendering in Python Worker.
-   Preserve a Python orchestration fallback.
-   Support local development before cloud deployment.
-   Keep operational complexity manageable for a solo developer.

## 2. High-Level Architecture

``` mermaid
flowchart TD
    U[User Browser] --> FE[Next.js Static Frontend]
    FE -->|HTTPS /api/v1| API[FastAPI Core API]
    FE -->|Presigned upload| R2[(Private R2 Storage)]
    API --> DB[(PostgreSQL)]
    API --> Q[Durable Task Queue]
    Q --> DISP[Job Dispatcher]
    DISP --> ORCH[WorkflowOrchestrator Interface]
    ORCH --> N8N[n8n Adapter / n8n]
    ORCH --> PY[Python Fallback Adapter]
    N8N -->|Signed source retrieval| API
    N8N --> GEM[Gemini Provider]
    N8N -->|Signed callback| API
    API -->|Normalize / Approved render task| Q
    Q --> WORKER[Python Worker]
    WORKER --> R2
    WORKER -->|Signed callback| API
```

The diagram represents logical components. It does not require each
component to be a separately deployed service during local development.

## 3. Component Responsibilities

### 3.1 Frontend

Responsibilities: - Authentication UI. - Client-side active workspace
selection (`workspace_id` passed on subsequent API calls). - Document
creation and review. - Template management UI. - Credit and usage
display. - Upload initiation and direct upload to R2 using presigned
URLs. - Polling or subscribing to backend job and document status.

Restrictions: - No direct n8n calls. - No secrets or provider API
keys. - No authority over role, price, credits, approval, or payment
status. - In static deployment (`output: 'export'`), do not depend on
Next.js SSR, Server Actions, middleware, or API routes. Dynamic document
resolution and authorization happen client-side through `/api/v1`. -
Deploy under the same registrable domain as the FastAPI backend so
`HttpOnly`, `Secure`, `SameSite` refresh cookies and `Origin`/CSRF
protections operate properly.

### 3.2 FastAPI Core

FastAPI is the sole application authority and the only component with direct access to the core PostgreSQL database. It is the source of truth for: - Authentication and sessions. -
User and workspace membership. - Authorization and tenant isolation (including the organization document permission policy). -
Document metadata, versions, `source_bundle_id` binding, and document lifecycle (`documents.status`). - Credit wallet, configurable complimentary beta credits, reservations,
transactions, and refunds. - Template metadata and versions. - Asynchronous job
records (`normalize`, `ai_generation`, `docx_render`) and state transitions. - Signed internal source-bundle delivery (`GET /api/v1/internal/source-bundles/{source_bundle_id}`). - Workflow and Worker callback verification (`POST /api/v1/internal/workflows/callback` and `POST /api/v1/internal/workers/callback`) and result
validation. - Committing reserved AI credits only after a valid AI draft is persisted as `review_required`. - Approval state. - API responses and audit events.

FastAPI must not trust an n8n or Worker result until it verifies the callback
HMAC-SHA256 signature, timestamp freshness, event ID replay protection, job identity, idempotency, schema, and permitted state
transition.

### 3.3 WorkflowOrchestrator

Define a stable application interface, for example:

-   `dispatch(job_payload) -> dispatch_result`
-   `cancel(external_execution_id)` where supported
-   `health_check()`

The exact Python typing and async behavior are implementation decisions,
but the interface must hide provider-specific details.

Adapters: - `N8NWorkflowAdapter` - `PythonWorkflowAdapter`

The document service must depend on the interface, not import
n8n-specific logic directly.

### 3.4 n8n

Responsibilities: - Receive minimal job payload including `job_id`, `workflow_key`, `workflow_version`, `document_type`, and `source_bundle_id`. - Select workflow by
stable workflow key/version. - Retrieve normalized source data from FastAPI via a private internal endpoint using short-lived signed authorization. - Coordinate Gemini calls and type-specific
processing (starting with `meeting_minutes` --- Notulen Rapat as the first vertical slice). - Apply bounded retries where safe. - Produce structured
output and provenance references. - Send signed completion/failure
callback to FastAPI (`POST /api/v1/internal/workflows/callback`).

Must not: - Authenticate end users. - Make workspace permission
decisions. - Read or mutate credit wallets. - Process payments or trust
payment state. - Approve documents. - Directly write application
tables. - Hold database credentials for the SaaS core. - Receive end-user login tokens, database credentials, or wallet balances. - Return a final
document as approved. - Log unnecessary raw source content in execution history.

### 3.5 Python Worker

Responsibilities: - Normalize supported input files (`normalize` job). - Extract text from
supported DOCX/PDF/TXT files after enforcing archive expansion caps, entry-count caps, magic-byte inspection, safe XML parsing (no DTD/external entities), and malformed-file rejection. - Perform safe file and template inspection (including detecting or normalizing Word XML run-split `{{placeholders}}`). - Render
approved structured content into DOCX templates (`docx_render` job). - Store generated
output in private object storage. - Report task outcome to FastAPI using
its authenticated callback (`POST /api/v1/internal/workers/callback`).

Restrictions & Guarantees: - Worker must **never** connect directly to the core PostgreSQL database; FastAPI owns all database reads and writes. - Worker must be idempotent and must verify that the requested job payload and signed parameters match the expected task context. - Never fetch external URLs or references embedded in uploaded documents.

### 3.6 PostgreSQL

Stores authoritative application state: - Users and sessions. -
Workspaces and memberships. - Invitations. - Documents and versions. -
Source records and provenance. - Templates and immutable versions. -
Jobs and attempts. - Credit wallets, reservations, and ledger. - Payment
records and webhook events. - Usage and audit records.

n8n execution storage is not a substitute for the application database.

### 3.7 R2

-   Private buckets.
-   Random object keys.
-   Workspace association stored in application DB.
-   Short-lived presigned URLs.
-   Server-side validation after upload.
-   No public bucket access for source or generated documents.

### 3.8 Queue

Cloud Tasks is the current deployment candidate for durable dispatch and
retry. Local development may use a simple local dispatcher or a
compatible development queue adapter.

Do not make n8n's execution history the only durable queue. Job state
must remain in PostgreSQL.

## 4. Document Lifecycle vs. Asynchronous Job Lifecycle

Document lifecycle and asynchronous job execution lifecycle are strictly separate. A document may have multiple `jobs` records over its lifetime (`normalize`, `ai_generation`, `docx_render`). The human review and approval pause belongs to `documents.status` and must never be represented as one continuously running asynchronous job.

### 4.1 Document Lifecycle (`documents.status`)

``` text
draft
  -> processing        (while normalize and/or ai_generation jobs run)
  -> review_required   (valid AI draft persisted; reserved AI credits committed)
  -> approved          (explicit human approval of exact version/content_hash)
  -> rendering         (while docx_render job runs)
  -> completed         (final DOCX validated and stored in private R2)
```

- Editing a document in `approved` or `completed` status creates a new `document_version`, invalidates approval, and returns `documents.status` to `review_required`.
- Processing or rendering failures transition `documents.status` to `failed` (recoverable via retry or editing).
- Soft deletion/archiving transitions `documents.status` to `archived` (with `deleted_at` populated).

### 4.2 Asynchronous Job Lifecycle (`jobs.status`)

Each background execution (`job_type` in `normalize`, `ai_generation`, `docx_render`) has its own `jobs` record:

``` text
queued
  -> running
  -> validating
  -> completed
```

Failure or cancellation from active states:

``` text
queued / running / validating
  -> failed / cancelled
```

A retry may create a new `job_attempts` record (or a new `jobs` record for a subsequent user-initiated run) while preserving history. Define allowed transitions centrally; never allow arbitrary state changes from the frontend. Provider status such as n8n `running` or `success` is mapped to application callback events and is not directly exposed as authoritative state.

## 5. AI Generation Sequence

``` mermaid
sequenceDiagram
    participant User
    participant FE as Frontend
    participant API as FastAPI
    participant DB as PostgreSQL
    participant Q as Task Queue
    participant W as Python Worker
    participant N8N as n8n
    participant AI as Gemini

    User->>FE: Submit document process request
    FE->>API: POST /api/v1/documents/{id}/process
    API->>DB: Authorize, reserve credits, set document=processing, create job(s)
    opt Uploaded files require normalization
        API->>Q: Enqueue normalize job
        Q->>W: Deliver normalize task
        W->>API: POST /api/v1/internal/workers/callback (signed)
        API->>DB: Validate & store normalized text in source bundle
    end
    API->>Q: Enqueue ai_generation dispatch (with source_bundle_id)
    Q->>API: Dispatch task
    API->>N8N: Minimal signed job payload (job_id, source_bundle_id)
    N8N->>API: GET /api/v1/internal/source-bundles/{source_bundle_id} (signed, short-lived)
    API-->>N8N: Normalized source bundle
    N8N->>AI: Generate structured draft
    AI-->>N8N: Structured result
    N8N->>API: POST /api/v1/internal/workflows/callback (signed)
    API->>DB: Verify HMAC/replay, validate schema, save draft (review_required), commit credits
    FE->>API: Poll job/document status
    API-->>FE: document.status = review_required + draft
    User->>FE: Review and approve
```

## 6. DOCX Rendering Sequence

``` mermaid
sequenceDiagram
    participant User
    participant FE as Frontend
    participant API as FastAPI
    participant DB as PostgreSQL
    participant Q as Task Queue
    participant W as Python Worker
    participant R2 as Private Storage

    User->>FE: Approve draft
    FE->>API: POST /api/v1/documents/{id}/approve
    API->>DB: Verify permission and draft version/hash
    API->>DB: Record approval (document.status = approved)
    FE->>API: POST /api/v1/documents/{id}/generate
    API->>DB: Create docx_render job (no second AI credit charge)
    API->>Q: Enqueue render task
    Q->>W: Deliver render task
    W->>R2: Read template/source objects
    W->>W: Render DOCX
    W->>R2: Store final DOCX
    W->>API: POST /api/v1/internal/workers/callback (signed)
    API->>DB: Validate callback, mark job & document completed, record output
    FE->>API: GET /api/v1/documents/{id}/download
    API-->>FE: Short-lived signed URL
```

## 7. Internal Service Endpoints, Signed Source Retrieval, and Callback Security

### 7.1 Internal Endpoints
- `GET /api/v1/internal/source-bundles/{source_bundle_id}`: Short-lived signed internal endpoint for n8n (or orchestrator adapter) to retrieve normalized source data bound to the job's workspace and document. No public unauthenticated source retrieval is permitted.
- `POST /api/v1/internal/workflows/callback`: Authenticated callback endpoint for workflow orchestrator (`n8n` / fallback) completion or failure events.
- `POST /api/v1/internal/workers/callback`: Authenticated callback endpoint for Python Worker (`normalize` / `docx_render`) completion or failure events.

### 7.2 Canonical Signature and Replay Protection
Internal requests and callbacks must include:
- Job ID and (where applicable) `source_bundle_id` or workflow key/version.
- Unique event/idempotency identifier (`event_id`).
- Timestamp (`timestamp`, UNIX epoch seconds or ISO-8601 UTC).
- Event type (`completed` or `failed`) and structured result or safe error object.
- HMAC-SHA256 signature computed over the canonical representation binding HTTP method, request path, timestamp, `event_id`, and body:
  `HMAC-SHA256(secret, "{timestamp}.{event_id}.{METHOD}.{canonical_path}.{raw_body}")` (where `METHOD` is uppercase HTTP method such as `GET` or `POST`, `canonical_path` is the exact request path including `source_bundle_id` for `GET /api/v1/internal/source-bundles/{source_bundle_id}` or callback path for `POST`, and `raw_body` is the exact UTF-8 request body for `POST` or empty string `""` for `GET` --- never relying on an empty `raw_body` alone).

Backend verification requirements:
- Verify signature using constant-time comparison (`hmac.compare_digest`).
- Enforce a timestamp freshness tolerance of **5 minutes (300 seconds)**.
- Persist and reject replayed `event_id` values (`workflow_callback_events`).
- Verify `source_bundle_id` expiration and workspace/document/job binding before returning source data.
- Ensure the target job is in an active state that accepts the transition.
- Validate callback results with Pydantic and enforce maximum payload size limits.
- Never trust callback-supplied workspace or user permissions.
- Never log raw document content or secrets in callback or execution logs.

## 8. Local Development Topology

Start with: - Frontend dev server. - FastAPI. - PostgreSQL via Docker
Compose. - n8n via Docker Compose. - Python Worker as a local
process/container. - Local object-storage adapter or a development R2
bucket. - Gemini adapter in mock mode by default.

Do not expose local n8n editor or webhook endpoints publicly. Use
local-only credentials and dummy documents.

A tunnel may be used for a controlled callback experiment, but never
expose the editor without authentication and network restrictions.

## 9. Production Deployment Direction

Current candidate: - Frontend: Cloudflare Pages static export. - API:
Google Cloud Run. - Worker: separate Cloud Run service or task-driven
worker. - Queue: Cloud Tasks. - Database: Neon PostgreSQL. - Object
storage: Cloudflare R2. - AI: Gemini provider adapter. - n8n hosting:
**TBD** pending license review, cost analysis, and hosting evaluation.

Cloud Run requires careful design for a persistent workflow
editor/runtime. Do not assume a scale-to-zero deployment is suitable for
n8n without testing its persistent database, webhook, execution, and
restart behavior.

The Python fallback means the product can operate without n8n if
production licensing or hosting is unsuitable.

## 10. Architecture Decision Records

### ADR-001: FastAPI remains core authority

**Decision:** FastAPI owns authentication, authorization, workspace
data, credits, document lifecycle, approvals, and database writes.

**Reason:** Avoid coupling product correctness and tenant security to a
workflow tool.

### ADR-002: n8n is replaceable

**Decision:** Use a WorkflowOrchestrator interface and adapters.

**Reason:** Preserve portability and allow Python fallback if n8n is
unavailable or licensing is unsuitable.

### ADR-003: Python Worker owns technical file processing without direct DB access

**Decision:** File normalization and DOCX rendering run in Python
Worker, which reads/writes object storage as authorized and reports outcomes to FastAPI via signed callbacks (`POST /api/v1/internal/workers/callback`). Worker never connects directly to the core PostgreSQL database.

**Reason:** Use suitable Python libraries, keep binary/document
operations out of orchestration workflows, and preserve FastAPI as the single authority over database state.

### ADR-004: PostgreSQL is the durable source of truth

**Decision:** Persist application jobs and state in PostgreSQL; use a
durable queue for dispatch.

**Reason:** n8n execution history alone is insufficient as the
application's job ledger.

### ADR-005: Local first

**Decision:** Build and validate locally before committing to production
hosting.

**Reason:** Avoid premature infrastructure cost and expose integration
issues early.
