# API Contract v1

**Status:** Draft v1.0\
**Base path:** `/api/v1`\
**Protocol:** HTTPS + JSON REST\
**Authentication:** Bearer access token; refresh token via Secure
HttpOnly cookie.

This document defines the initial API surface. Exact request/response
schemas must be added before each endpoint is implemented. Do not invent
missing fields silently.

## 1. Conventions

### 1.1 Headers

Authenticated requests: - `Authorization: Bearer <access_token>` -
`Content-Type: application/json` where applicable

Cookie-authenticated refresh/logout endpoints require CSRF/origin
protection. In static frontend deployment (Cloudflare Pages + Cloud Run),
serve the frontend and API under the same registrable domain so
`HttpOnly`, `Secure`, `SameSite` refresh cookies and `Origin`/CSRF
protections operate reliably. Dynamic document resolution and
authorization happen client-side through `/api/v1`.

### 1.2 Success response

A consistent response envelope is recommended:

``` json
{
  "success": true,
  "data": {}
}
```

### 1.3 Error response

``` json
{
  "success": false,
  "error": {
    "code": "INSUFFICIENT_CREDITS",
    "message": "Kredit workspace tidak mencukupi.",
    "details": {
      "required": 5,
      "available": 2
    }
  }
}
```

Do not expose stack traces, secrets, SQL details, provider credentials,
or sensitive source text.

### 1.4 Pagination

Initial page-based format: - `page` - `page_size`

Response should include items and pagination metadata. Enforce a maximum
page size.

### 1.5 Idempotency

Use an `Idempotency-Key` header for operations that create billable
jobs, payment orders, reservations, or other non-repeatable side
effects.

Backend must scope keys appropriately and return the original result for
safe retries.

## 2. Authentication

  -------------------------------------------------------------------------
  Method                  Endpoint                  Purpose
  ----------------------- ------------------------- -----------------------
  POST                    `/auth/register`          Register user and
                                                    create Personal
                                                    Workspace

  POST                    `/auth/login`             Authenticate and issue
                                                    tokens

  POST                    `/auth/logout`            Revoke refresh session

  POST                    `/auth/refresh`           Rotate refresh token

  GET                     `/auth/me`                Current user profile

  POST                    `/auth/verify-email`      Verify email token

  POST                    `/auth/forgot-password`   Request reset email

  POST                    `/auth/reset-password`    Set new password
  -------------------------------------------------------------------------

Requirements: - Argon2id password hashing (constant-time dummy verification
on missing accounts and transparent rehash upgrades on login). - Dual
per-IP and SHA-256 hashed per-account rate limiting on authentication
recovery and login paths. - Generic responses for account-recovery
requests to reduce enumeration. - Refresh token rotation, single-use
consumption, and family revocation under row-level locks. - Cookie-based auth endpoints (`/auth/refresh`, `/auth/logout`) enforce `Origin` allowlist
(with `Referer` scheme+netloc fallback when `Origin` is absent, configurable
via `require_origin_or_referer` in non-production) plus double-submit
`csrf_token` / `X-CSRF-Token` validation, and expire auth cookies
(`Max-Age=0`) on `401` refresh failures. `refresh_token` is `HttpOnly` and
scoped to `Path=/api/v1/auth`, while the readable `csrf_token` cookie uses
`Path=/` (with matching attributes and paths on deletion) so frontend routes
(`/`, `/login`, `/register`) can read it via `document.cookie`. Frontend
single-flight refresh coalescing and logout/refresh generation sequencing
coordinate concurrent calls within a single browser tab; cross-tab refresh
coordination (e.g., `BroadcastChannel` or `navigator.locks`) is explicitly
deferred as a known production-readiness limitation. - Register transaction creates
user and exactly one Personal Workspace (Personal Workspace creation deferred
to M3). - M2 uses a stub transactional email service; real email provider
integration remains a separate task before production launch.

## 3. Workspaces

  ----------------------------------------------------------------------------------------------------------
  Method                  Endpoint                                                   Purpose
  ----------------------- ---------------------------------------------------------- -----------------------
  GET                     `/workspaces`                                              List accessible
                                                                                     workspaces

  POST                    `/workspaces`                                              Create organization
                                                                                     workspace

  GET                     `/workspaces/{workspace_id}`                               Workspace details

  PATCH                   `/workspaces/{workspace_id}`                               Update permitted
                                                                                     settings

  DELETE                  `/workspaces/{workspace_id}`                               Delete workspace under
                                                                                     owner policy

  POST                    `/workspaces/{workspace_id}/transfer-ownership`            Transfer ownership

  GET                     `/workspaces/{workspace_id}/members`                       List members

  POST                    `/workspaces/{workspace_id}/invitations`                   Create invitation

  GET                     `/workspaces/{workspace_id}/invitations`                   List invitations

  DELETE                  `/workspaces/{workspace_id}/invitations/{invitation_id}`   Cancel invitation

  PATCH                   `/workspaces/{workspace_id}/members/{user_id}`             Change member
                                                                                     role/status

  DELETE                  `/workspaces/{workspace_id}/members/{user_id}`             Remove member

  POST                    `/invitations/accept`                                      Accept invitation
  ----------------------------------------------------------------------------------------------------------

Rules: - Personal Workspace is created automatically, not through the
organization creation endpoint. - **Workspace switching:** Workspace switching is a client-side active workspace selection. The frontend retrieves accessible workspaces via `GET /api/v1/workspaces`, the user selects an active workspace, and subsequent requests pass the selected `workspace_id` (validated by FastAPI for membership and permissions on every workspace-scoped request). No dedicated server-side workspace-switching endpoint is required. - Only authorized organization roles can
manage members. - Admin cannot transfer ownership or modify the Owner. -
All endpoints enforce tenant isolation. - Removing a member does not
delete their documents. - Invitation acceptance validates token hash,
expiry, unused status, and email match.

## 4. Credits and Payments

  ---------------------------------------------------------------------------------------------------
  Method                  Endpoint                                            Purpose
  ----------------------- --------------------------------------------------- -----------------------
  GET                     `/workspaces/{workspace_id}/credits`                Wallet summary

  GET                     `/workspaces/{workspace_id}/credits/transactions`   Ledger history

  GET                     `/credit-packages`                                  Active package catalog

  GET                     `/payment-methods`                                  Available methods, if
                                                                              enabled

  POST                    `/payments/orders`                                  Create payment order

  GET                     `/payments/orders/{order_id}`                       Payment order status

  POST                    `/subscriptions`                                    Start subscription, if
                                                                              enabled

  GET                     `/workspaces/{workspace_id}/subscription`           Subscription details

  POST                    `/subscriptions/{subscription_id}/cancel`           Cancel subscription

  POST                    `/payments/webhooks/midtrans`                       Provider webhook
  ---------------------------------------------------------------------------------------------------

Rules: - Live payments disabled for initial beta. - Complimentary beta credit allocation must be configurable in backend settings (never hardcode a permanent complimentary credit amount; initial configured amount, credit tariffs, and monetary prices remain **TBD** until decided by the Product Owner). - Credit commitment occurs **only after a valid AI result has been persisted as a reviewable draft (`documents.status = review_required`)**. - DOCX rendering failures must not cause a second AI charge, and re-rendering an already approved document version must not incur another AI-generation charge. - Stale reservations are reconciled idempotently only after verifying with row locks that the associated job is genuinely no longer active. - Backend looks up
price and credits from database. - Verify provider webhook signature. -
Enforce replay protection and idempotent processing. - Never trust
frontend payment status or amount. - Do not expose provider secrets. -
Subscription endpoints may remain disabled until provider and
eligibility are resolved.

## 5. Documents

  ------------------------------------------------------------------------------------------------------------------------
  Method                  Endpoint                                                                   Purpose
  ----------------------- -------------------------------------------------------------------------- ---------------------
  GET                     `/workspaces/{workspace_id}/documents`                                     List workspace
                                                                                                     documents

  POST                    `/workspaces/{workspace_id}/documents`                                     Create document
                                                                                                     draft/container

  GET                     `/documents/{document_id}`                                                 Get document and
                                                                                                     current draft

  PATCH                   `/documents/{document_id}`                                                 Update editable
                                                                                                     metadata/content
                                                                                                     under policy

  DELETE                  `/documents/{document_id}`                                                 Delete/archive
                                                                                                     document

  POST                    `/workspaces/{workspace_id}/documents/{document_id}/sources/upload-intents` Request presigned
                                                                                                     upload intent for
                                                                                                     document source file

  POST                    `/workspaces/{workspace_id}/documents/{document_id}/sources`               Confirm uploaded
                                                                                                     source file or submit
                                                                                                     supported text/form
                                                                                                     input

  GET                     `/documents/{document_id}/sources`                                         View
                                                                                                     source/provenance
                                                                                                     records

  POST                    `/documents/{document_id}/process`                                         Start normalization
                                                                                                     and AI processing

  POST                    `/documents/{document_id}/approve`                                         Approve current draft
                                                                                                     version

  POST                    `/documents/{document_id}/generate`                                        Enqueue DOCX render

  GET                     `/documents/{document_id}/versions`                                        List document
                                                                                                     versions

  GET                     `/documents/{document_id}/download`                                        Obtain short-lived
                                                                                                     download URL
  ------------------------------------------------------------------------------------------------------------------------

*(Note: All endpoints above are mounted under the `/api/v1` base path, e.g., `POST /api/v1/workspaces/{workspace_id}/documents/{document_id}/sources/upload-intents` and `POST /api/v1/workspaces/{workspace_id}/documents/{document_id}/sources`.)*

Rules:
-   **Organization Document Permission Policy (Authoritative Backend Enforcement):**
    -   **Owner:** Full access to all organization documents.
    -   **Admin:** Full access to all organization documents.
    -   **Editor:** Full access to documents they own; read-only access (`GET`) to other members' documents.
    -   **Member:** Full access only to documents they own; no access to other members' documents.
    -   For documents owned by other members, **Editor** and **Member** must not edit, add/confirm sources, process, approve, generate, or delete them. Frontend visibility is never a security boundary.
-   **Document Source Endpoints (`upload-intents` & `sources`):**
    -   `POST /api/v1/workspaces/{workspace_id}/documents/{document_id}/sources/upload-intents` authorizes workspace membership and document write permission, validates file count, declared size, and allowed MIME/extension (`DOCX`, `PDF`, `TXT`), and issues a short-lived presigned R2 upload URL with a random object key bound to `(workspace_id, document_id)`.
    -   `POST /api/v1/workspaces/{workspace_id}/documents/{document_id}/sources` either confirms a completed presigned upload (verifying object ownership in `(workspace_id, document_id)` before scheduling/running content validation) or records structured form/raw text source input.
-   **Document Lifecycle vs. Job Lifecycle:**
    -   `documents.status` (`draft`, `processing`, `review_required`, `approved`, `rendering`, `completed`, `failed`, `archived`) is separate from asynchronous `jobs.status` (`queued`, `running`, `validating`, `completed`, `failed`, `cancelled`).
    -   `POST /documents/{document_id}/process` reserves credits transactionally, sets `documents.status = processing`, and creates asynchronous `normalize` / `ai_generation` job record(s). Reserved AI credits are committed only when a valid AI draft is persisted (`documents.status = review_required`).
    -   The human review and approval pause (`review_required` -> `approved`) is represented on `documents.status`, never as a continuously running background job.
-   Approval (`POST /documents/{document_id}/approve`) references the exact current version/hash. Editing after approval creates a new version and invalidates approval.
-   Generate (`POST /documents/{document_id}/generate`) requires a valid approval for the exact version and creates a `docx_render` job (`documents.status = rendering`) without incurring another AI-generation credit charge.
-   Download URL is issued only after workspace and document access checks.
-   Delete behavior and retention are subject to policy.

## 6. Templates

  -----------------------------------------------------------------------------------------
  Method                  Endpoint                                  Purpose
  ----------------------- ----------------------------------------- -----------------------
  GET                     `/workspaces/{workspace_id}/templates`    List available
                                                                    templates

  POST                    `/workspaces/{workspace_id}/templates`    Create template
                                                                    metadata

  GET                     `/templates/{template_id}`                Template details

  PATCH                   `/templates/{template_id}`                Update permitted
                                                                    metadata

  DELETE                  `/templates/{template_id}`                Archive/delete template

  POST                    `/templates/{template_id}/versions`       Add immutable version

  GET                     `/templates/{template_id}/versions`       List versions

  POST                    `/templates/upload-intents`               Create presigned upload
                                                                    intent

  POST                    `/templates/{template_id}/validate`       Validate uploaded
                                                                    template

  GET                     `/templates/{template_id}/placeholders`   List extracted
                                                                    placeholders

  PATCH                   `/templates/{template_id}/mapping`        Save
                                                                    placeholder-to-field
                                                                    mapping

  POST                    `/templates/{template_id}/preview`        Validate/render a
                                                                    limited template
                                                                    preview
  -----------------------------------------------------------------------------------------

Notes: - `/preview` means template validation/preview; generated PDF
preview is deferred. - Personal Workspace may use built-in templates
only. - Organization template management requires the appropriate
role. - Published versions are immutable.

## 7. Upload Protocol

Applies consistently to both document source uploads (`POST /api/v1/workspaces/{workspace_id}/documents/{document_id}/sources/upload-intents` and `POST /api/v1/workspaces/{workspace_id}/documents/{document_id}/sources`) and organization template uploads (`POST /api/v1/templates/upload-intents` and `POST /api/v1/templates/{template_id}/validate`):

1.  Frontend requests an upload intent from FastAPI.
2.  Backend checks authorization (workspace membership and document/template write permission), file count, declared size, and allowed
    content type.
3.  Backend creates a random object key bound to
    workspace/document/template context.
4.  Backend returns short-lived presigned upload URL and required
    headers.
5.  Frontend uploads directly to private R2.
6.  Frontend confirms upload with backend (`POST /api/v1/workspaces/{workspace_id}/documents/{document_id}/sources` for document sources, or template validation endpoint for templates).
7.  Backend/Worker verifies object existence, workspace/resource ownership, size, magic bytes, archive
    structure, entry count, expansion ratio (zip-bomb protection), safe XML parsing (disabling external entities/DTDs), and content safety. For templates, also validate or normalize Word XML run-split `{{placeholders}}`.
8.  Only validated objects become usable sources or template versions; malformed or unsafe files are rejected.

Initial limits: - 10 MB per file. - 5 files per document. - DOCX, PDF,
TXT. - No arbitrary ZIP or legacy DOC. - Limits are subject to revision
after testing.

Never accept a client-supplied object key as proof of ownership.

## 8. Jobs and Usage

  Method   Endpoint                                     Purpose
  -------- -------------------------------------------- ------------------------------------------
  GET      `/jobs/{job_id}`                             Get asynchronous job status (`normalize`, `ai_generation`, `docx_render`) and safe progress details
  GET      `/workspaces/{workspace_id}/usage`           Usage events
  GET      `/workspaces/{workspace_id}/usage/summary`   Usage summary

The backend exposes individual asynchronous job state (`jobs.status`), which is distinct from overall document lifecycle state (`documents.status`), and never exposes raw provider execution internals.

## 9. Internal Service Endpoints

Internal service endpoints (mounted under `/api/v1`) are not public user APIs. They require short-lived HMAC-SHA256 service authentication and strict schemas.

Approved internal endpoints:
-   `GET /api/v1/internal/source-bundles/{source_bundle_id}` (relative: `/internal/source-bundles/{source_bundle_id}`): Used by n8n to retrieve normalized source data bound to the job's workspace and document (`source_bundle_id`). No public unauthenticated source retrieval is allowed.
-   `POST /api/v1/internal/workflows/callback` (relative: `/internal/workflows/callback`): Used by the workflow orchestrator (n8n / fallback) to report AI workflow completion or failure.
-   `POST /api/v1/internal/workers/callback` (relative: `/internal/workers/callback`): Used by the Python Worker to report `normalize` and `docx_render` completion or failure. The Worker never connects directly to PostgreSQL.

Security and validation requirements:
-   **Canonical HMAC-SHA256 Signature:** `HMAC-SHA256(secret, "{timestamp}.{event_id}.{METHOD}.{canonical_path}.{raw_body}")` verified using constant-time comparison (`hmac.compare_digest`), binding uppercase HTTP `METHOD`, exact `canonical_path` (including `source_bundle_id` for `GET` source-bundle retrieval), `timestamp`, `event_id`, and exact `raw_body` (`""` for bodyless `GET` requests, never relying on an empty `raw_body` alone).
-   **Timestamp Freshness:** Reject requests outside a **5-minute (300-second)** tolerance window.
-   **Replay Protection & Idempotency:** Require a unique `event_id` / idempotency identifier and reject replayed events.
-   **Context & State Validation:** Verify `job_id`, `source_bundle_id` expiration, workspace/document binding, and permitted job state transitions.
-   **Payload Bounds & Logging:** Enforce strict payload size limits and never log secrets or unnecessary raw source text.

Never send end-user login tokens, database credentials, or wallet balances to n8n or Worker.

## 10. Initial Error Codes

-   `VALIDATION_ERROR`
-   `UNAUTHENTICATED`
-   `FORBIDDEN`
-   `NOT_FOUND`
-   `CONFLICT`
-   `RATE_LIMITED`
-   `INSUFFICIENT_CREDITS`
-   `INVALID_JOB_STATE`
-   `UPLOAD_REJECTED`
-   `TEMPLATE_INVALID`
-   `APPROVAL_REQUIRED`
-   `CALLBACK_INVALID`
-   `IDEMPOTENCY_CONFLICT`
-   `PROVIDER_UNAVAILABLE`
-   `INTERNAL_ERROR`

## 11. Rate Limits (Initial Targets)

-   Login: 5 requests/minute/IP and 5 requests/5 minutes/hashed-account.
-   Forgot password: 10 requests/hour/IP and 3 requests/hour/hashed-account.
-   Register: 5 requests/hour/IP.
-   AI processing: workspace quotas and concurrent job caps.
-   Upload: size/count limits and per-user throttling.
-   Initial active generation target: maximum 3 concurrent jobs/user.

These are starting values and must be tuned using observed traffic and
abuse testing.

> **Operational Scope Note (M2 Dev/Beta Limiter & Proxy Trust):** The
> bounded in-memory sliding-window rate limiter uses **independent per-scope
> capacity pools** (`register:ip`, `login:ip`, `login:acct`, `forgot:ip`,
> `forgot:acct`, defaulting to 10,000 keys per scope and configurable per scope),
> SHA-256 hashed account keys, **expired/empty bucket pruning before rejecting
> new keys**, and **safe saturation logging/metrics without raw identifiers**,
> and is intended for **single-instance local development and single-instance
> beta only**. Active buckets are **never evicted**; when a scope pool is
> saturated by active keys, the limiter **fails closed with HTTP 429
> (`RATE_LIMITED`)** for unseen keys in that scope until existing sliding
> windows expire.
>
> **Availability Implications of Fail-Closed Saturation:** If a single scope
> reaches its configured active-key capacity during a high-cardinality burst or
> volumetric flood, new unseen callers in that scope receive HTTP 429 until
> active buckets age out—preserving active brute-force and recovery counters at
> the cost of temporary unavailability for unseen keys in that saturated scope.
> Independent per-scope pools ensure cross-scope exhaustion cannot occur (for
> example, a registration or IP flood cannot evict or exhaust account-level
> `login:acct` or `forgot:acct` pools). Multi-instance production deployments
> MUST replace this in-memory limiter with a shared backing store (e.g., Redis)
> so counters and capacities remain consistent across workers/replicas.
> Additionally, client IP extraction (`request.client.host`) assumes direct
> connections in dev; production deployments behind a reverse proxy or CDN
> MUST configure trusted proxy CIDR allowlists and forwarded header parsing
> (`X-Forwarded-For` / `Forwarded`) before launch.

## 12. API Definition of Done

Before implementing an endpoint, document: - Request schema. - Response
schema. - Authentication requirements. - Permission requirements. -
Tenant scoping. - Idempotency behavior. - Error cases. - Side effects
and transaction boundary. - Tests.

OpenAPI must reflect the implemented contract.
