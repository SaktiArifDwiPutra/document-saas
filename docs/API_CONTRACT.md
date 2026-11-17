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
protection.

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

Requirements: - Argon2id password hashing. - Rate limiting. - Generic
responses for account-recovery requests to reduce enumeration. - Refresh
token rotation and revocation. - Register transaction creates user and
exactly one Personal Workspace.

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
organization creation endpoint. - Only authorized organization roles can
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

Rules: - Live payments disabled for initial beta. - Backend looks up
price and credits from database. - Verify provider webhook signature. -
Enforce replay protection and idempotent processing. - Never trust
frontend payment status or amount. - Do not expose provider secrets. -
Subscription endpoints may remain disabled until provider and
eligibility are resolved.

## 5. Documents

  ----------------------------------------------------------------------------------------
  Method                  Endpoint                                 Purpose
  ----------------------- ---------------------------------------- -----------------------
  GET                     `/workspaces/{workspace_id}/documents`   List workspace
                                                                   documents

  POST                    `/workspaces/{workspace_id}/documents`   Create document
                                                                   draft/container

  GET                     `/documents/{document_id}`               Get document and
                                                                   current draft

  PATCH                   `/documents/{document_id}`               Update editable
                                                                   metadata/content under
                                                                   policy

  DELETE                  `/documents/{document_id}`               Delete/archive document

  POST                    `/documents/{document_id}/process`       Start normalization and
                                                                   AI processing

  POST                    `/documents/{document_id}/approve`       Approve current draft
                                                                   version

  POST                    `/documents/{document_id}/generate`      Enqueue DOCX render

  GET                     `/documents/{document_id}/versions`      List document versions

  GET                     `/documents/{document_id}/download`      Obtain short-lived
                                                                   download URL

  GET                     `/documents/{document_id}/sources`       View source/provenance
                                                                   records
  ----------------------------------------------------------------------------------------

Rules: - Every endpoint authorizes current user against workspace
membership and document policy. - Process endpoint reserves credits
transactionally. - Approval references the exact current version/hash. -
Editing after approval invalidates approval. - Generate requires a valid
approval for the exact version. - Download URL is issued only after
access check. - Delete behavior and retention are subject to policy.

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

1.  Frontend requests an upload intent from FastAPI.
2.  Backend checks authorization, file count, declared size, and allowed
    content type.
3.  Backend creates a random object key bound to
    workspace/document/template context.
4.  Backend returns short-lived presigned upload URL and required
    headers.
5.  Frontend uploads directly to private R2.
6.  Frontend confirms upload with backend.
7.  Backend/Worker verifies object existence, size, magic bytes, archive
    structure, and content safety.
8.  Only validated objects become usable sources.

Initial limits: - 10 MB per file. - 5 files per document. - DOCX, PDF,
TXT. - No arbitrary ZIP or legacy DOC. - Limits are subject to revision
after testing.

Never accept a client-supplied object key as proof of ownership.

## 8. Jobs and Usage

  Method   Endpoint                                     Purpose
  -------- -------------------------------------------- ------------------------------------------
  GET      `/jobs/{job_id}`                             Get job status and safe progress details
  GET      `/workspaces/{workspace_id}/usage`           Usage events
  GET      `/workspaces/{workspace_id}/usage/summary`   Usage summary

The backend exposes application job state, not raw provider execution
internals.

## 9. Internal Service Endpoints

Internal callback endpoints are not public user APIs. They require
service authentication and strict schemas.

Possible logical endpoints: - `POST /internal/workflows/callback` -
`POST /internal/workers/callback`

Exact paths are **TBD**. They must: - Verify HMAC or equivalent service
identity. - Include timestamp and idempotency/event ID. - Validate job
state and ownership. - Reject replayed callbacks. - Enforce payload
size. - Avoid sensitive logging.

Do not send end-user access tokens to n8n or Worker.

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

-   Login: 5 requests/minute/IP.
-   Forgot password: 3 requests/hour/account.
-   Register: 5 requests/hour/IP.
-   AI processing: workspace quotas and concurrent job caps.
-   Upload: size/count limits and per-user throttling.
-   Initial active generation target: maximum 3 concurrent jobs/user.

These are starting values and must be tuned using observed traffic and
abuse testing.

## 12. API Definition of Done

Before implementing an endpoint, document: - Request schema. - Response
schema. - Authentication requirements. - Permission requirements. -
Tenant scoping. - Idempotency behavior. - Error cases. - Side effects
and transaction boundary. - Tests.

OpenAPI must reflect the implemented contract.
