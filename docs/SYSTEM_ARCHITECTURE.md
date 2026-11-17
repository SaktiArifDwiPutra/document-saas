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
    N8N --> GEM[Gemini Provider]
    N8N -->|Signed callback| API
    API --> DB
    API -->|Approved render task| Q
    Q --> WORKER[Python Worker]
    WORKER --> R2
    WORKER --> DB
```

The diagram represents logical components. It does not require each
component to be a separately deployed service during local development.

## 3. Component Responsibilities

### 3.1 Frontend

Responsibilities: - Authentication UI. - Workspace selection. - Document
creation and review. - Template management UI. - Credit and usage
display. - Upload initiation and direct upload to R2 using presigned
URLs. - Polling or subscribing to backend job status.

Restrictions: - No direct n8n calls. - No secrets or provider API
keys. - No authority over role, price, credits, approval, or payment
status. - In static deployment, do not depend on Next.js SSR, Server
Actions, middleware, or API routes.

### 3.2 FastAPI Core

FastAPI is the source of truth for: - Authentication and sessions. -
User and workspace membership. - Authorization and tenant isolation. -
Document metadata and lifecycle. - Credit wallet, reservations,
transactions, and refunds. - Template metadata and versions. - Job
records and state transitions. - Callback verification and result
validation. - Approval state. - API responses and audit events.

FastAPI must not trust an n8n result until it verifies the callback
signature, job identity, idempotency, schema, and permitted state
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

Responsibilities: - Receive minimal job payload. - Select workflow by
stable workflow key/version. - Coordinate Gemini calls and type-specific
processing. - Apply bounded retries where safe. - Produce structured
output and provenance references. - Send signed completion/failure
callback to FastAPI.

Must not: - Authenticate end users. - Make workspace permission
decisions. - Read or mutate credit wallets. - Process payments or trust
payment state. - Approve documents. - Directly write application
tables. - Hold database credentials for the SaaS core. - Return a final
document as approved.

### 3.5 Python Worker

Responsibilities: - Normalize supported input files. - Extract text from
supported DOCX/PDF/TXT files. - Perform safe file inspection. - Render
approved structured content into DOCX templates. - Store generated
output in private object storage. - Report task outcome to FastAPI using
authenticated service-to-service communication.

Worker must be idempotent and must verify that the requested document
and template version are authorized for the job.

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

## 4. Job Lifecycle

Application job lifecycle and provider execution status are distinct.

Suggested application states:

``` text
queued
  -> normalizing
  -> routing
  -> generating
  -> validating
  -> review_required
  -> approved
  -> rendering
  -> completed
```

Failure can occur from processing states:

``` text
normalizing/routing/generating/validating/rendering
  -> failed
```

A retry may create another attempt while preserving the overall
job/document history. Define allowed transitions centrally; never allow
arbitrary state changes from the frontend.

Provider status such as n8n `running` or `success` is mapped to
application events and is not directly exposed as authoritative state.

## 5. AI Generation Sequence

``` mermaid
sequenceDiagram
    participant User
    participant FE as Frontend
    participant API as FastAPI
    participant DB as PostgreSQL
    participant Q as Task Queue
    participant N8N as n8n
    participant AI as Gemini

    User->>FE: Submit document request
    FE->>API: Create/process request
    API->>DB: Authorize, create job, reserve credits
    API->>Q: Enqueue dispatch
    Q->>API: Dispatch task
    API->>N8N: Minimal signed job payload
    N8N->>AI: Generate structured draft
    AI-->>N8N: Structured result
    N8N->>API: Signed callback
    API->>DB: Verify, validate, save draft
    FE->>API: Poll job/document status
    API-->>FE: review_required + draft
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
    FE->>API: POST approve
    API->>DB: Verify permission and draft version
    API->>DB: Record approval
    FE->>API: POST generate
    API->>DB: Create render job
    API->>Q: Enqueue render
    Q->>W: Deliver render task
    W->>R2: Read template/source objects
    W->>W: Render DOCX
    W->>R2: Store final DOCX
    W->>API: Authenticated completion callback
    API->>DB: Validate completion and record output
    FE->>API: Request download
    API-->>FE: Short-lived signed URL
```

## 7. Callback Security

Callbacks must include: - Job ID. - Workflow key and version. -
Attempt/idempotency identifier. - Timestamp. - Event type. - Structured
result or safe error object. - HMAC signature over a canonical request
representation.

Backend requirements: - Verify signature using constant-time
comparison. - Reject stale timestamps within a defined tolerance. -
Store/reject replayed event identifiers. - Ensure job is in a state that
accepts the event. - Validate result with Pydantic. - Enforce maximum
payload size. - Never trust callback-supplied workspace or user
permissions. - Avoid raw document content in callback logs.

Exact canonicalization, secret rotation, and timestamp tolerance are
**TBD** in implementation design.

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

### ADR-003: Python Worker owns technical file processing

**Decision:** File normalization and DOCX rendering run in Python
Worker.

**Reason:** Use suitable Python libraries and keep binary/document
operations out of orchestration workflows.

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
