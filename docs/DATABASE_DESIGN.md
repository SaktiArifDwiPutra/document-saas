# Database Design

**Status:** Draft v1.0\
**Database:** PostgreSQL\
**Principle:** relational source of truth, explicit tenant ownership,
auditable financial operations.

## 1. Conventions

-   Use UUID primary keys unless a concrete reason requires otherwise.
-   Store timestamps in UTC (`timestamptz`).
-   Use explicit foreign keys and indexes.
-   Use database transactions for multi-row state changes.
-   Use `created_at` and `updated_at` where applicable.
-   Soft deletion is appropriate for selected user-facing records, but
    financial/audit history must not be silently erased.
-   Never trust a workspace ID from a client without checking membership
    and permission.
-   Define uniqueness constraints for idempotency and one-to-one
    relationships.
-   Exact ORM and migration tooling are **TBD**.

## 2. Entity Relationship Overview

``` mermaid
erDiagram
    users ||--o{ workspace_members : joins
    workspaces ||--o{ workspace_members : has
    workspaces ||--o{ invitations : issues
    workspaces ||--o{ documents : owns
    users ||--o{ documents : creates
    documents ||--o{ document_versions : has
    documents ||--o{ document_sources : references
    documents ||--o{ jobs : processes
    workspaces ||--o{ templates : owns
    templates ||--o{ template_versions : versions
    workspaces ||--|| credit_wallets : has
    credit_wallets ||--o{ credit_transactions : records
    credit_wallets ||--o{ credit_reservations : reserves
    workspaces ||--o{ subscriptions : subscribes
    payment_orders ||--o{ payment_webhook_events : receives
```

This is a logical overview, not a complete migration specification.

## 3. Identity and Access Tables

### `users`

Suggested fields: - `id` - `email` (normalized; unique) -
`password_hash` - `display_name` - `email_verified_at` - `status` -
`created_at` - `updated_at`

Do not store plaintext passwords or raw refresh tokens.

### `auth_sessions` or `refresh_tokens`

Suggested fields: - `id` - `user_id` - `token_hash` - `family_id` -
`expires_at` - `revoked_at` - `replaced_by_id` - `created_at` -
`last_used_at` - optional device/session metadata

Refresh token rotation and reuse detection must be implemented.

### `workspaces`

Suggested fields: - `id` - `name` - `type` (`personal`,
`organization`) - `owner_user_id` - `status` - `created_at` -
`updated_at` - `deleted_at` if soft deletion is used

Every user must have exactly one Personal Workspace. Enforce this
through a reliable service transaction and, where feasible, a database
constraint/index strategy.

### `workspace_members`

Suggested fields: - `id` - `workspace_id` - `user_id` - `role` -
`status` - `joined_at` - `created_at` - `updated_at`

Constraints: - Unique `(workspace_id, user_id)`. - Role values
restricted to approved enum/check constraint. - Owner membership must
remain consistent with `workspaces.owner_user_id`.

### `invitations`

Suggested fields: - `id` - `workspace_id` - `email_normalized` -
`role` - `token_hash` - `expires_at` - `accepted_at` - `cancelled_at` -
`created_by_user_id` - `created_at`

Store only a hash of the random invitation token. Enforce single use and
email match.

## 4. Documents and Provenance

### `documents`

Suggested fields: - `id` - `workspace_id` - `created_by_user_id` -
`document_type` - `title` - `status` (`draft`, `processing`, `review_required`, `approved`, `rendering`, `completed`, `failed`, `archived`) - `current_version_id` (nullable) -
`selected_template_version_id` (nullable; references `template_versions.id`) - `approved_version_id` (nullable) - `created_at` -
`updated_at` - `deleted_at`

A document belongs to one workspace. Every read/write must authorize
membership and document-level permissions according to the approved Organization Document Permission Policy:
-   **Owner:** Full access to all organization documents.
-   **Admin:** Full access to all organization documents.
-   **Editor:** Full access to documents they own (`created_by_user_id`); read-only access to other members' documents.
-   **Member:** Full access only to documents they own (`created_by_user_id`); no access to other members' documents.
-   For documents owned by other members, **Editor** and **Member** must not edit, approve, generate, or delete them.

**Document Lifecycle vs. Job Lifecycle:**
`documents.status` tracks the overall document lifecycle across human review (`review_required` -> `approved`), whereas `jobs.status` tracks individual asynchronous tasks (`normalize`, `ai_generation`, `docx_render`). The human review pause is never represented as a continuously running job.

**Circular Relationship (`documents` <-> `document_versions`):**
Because `documents.current_version_id` and `documents.approved_version_id` reference `document_versions.id` while `document_versions.document_id` references `documents.id`:
-   `current_version_id` and `approved_version_id` must be nullable.
-   Migrations must create `documents` first, then `document_versions` (with `document_id REFERENCES documents(id)`), and then add the `current_version_id` and `approved_version_id` foreign key constraints via `ALTER TABLE` (`use_alter=True` / `DEFERRABLE INITIALLY DEFERRED` where appropriate).

### `document_versions`

Suggested fields: - `id` - `document_id` - `version_number` -
`structured_content` (JSONB) - `content_hash` - `created_by_user_id`
nullable for system-generated draft - `created_at` - `approval_status` -
`approved_by_user_id` - `approved_at`

Do not overwrite approved content in place. Create a new version when
edits occur, clear `documents.approved_version_id`, and transition `documents.status` back to `review_required`.

### `document_sources`

Suggested fields: - `id` - `workspace_id` - `document_id` - `document_version_id`
nullable - `source_bundle_id` (nullable UUID grouping normalized sources for a processing run, bound to `workspace_id` and `document_id`) - `source_type` (`form`, `text`, `upload`,
`workspace_reference`) - `storage_object_key` nullable - `source_name` -
`extracted_text_reference` or controlled text storage - `content_hash` -
`metadata` JSONB - `created_at`

Store only what is needed. Apply retention rules to original uploads and
extracted text. `source_bundle_id` is used by FastAPI's signed internal endpoint (`GET /api/v1/internal/source-bundles/{source_bundle_id}`) so n8n can retrieve normalized source content with short-lived authorization after workspace and job binding verification.

### Provenance representation

Important generated fields should be traceable to source identifiers
and, where feasible, text excerpts or page/section references. Avoid
copying large source excerpts into ordinary logs.

## 5. Templates

### `templates`

Suggested fields: - `id` - `workspace_id` nullable for built-in
templates - `name` - `document_type` - `description` - `is_builtin` -
`status` - `created_by_user_id` - `created_at` - `updated_at`

### `template_versions`

Suggested fields: - `id` - `template_id` - `version_number` -
`storage_object_key` - `content_hash` - `placeholder_schema` JSONB -
`mapping` JSONB - `validation_status` - `created_at` - `published_at` -
`archived_at`

Versions are immutable after publication. Documents reference exact
template version IDs.

## 6. Jobs and Workflow Execution

### `jobs`

Suggested fields: - `id` - `workspace_id` - `document_id` - `job_type`
(`normalize`, `ai_generation`, `docx_render`) - `status` (`queued`, `running`, `validating`, `completed`, `failed`, `cancelled`) -
`source_bundle_id` (nullable UUID) - `workflow_key` - `workflow_version` - `workflow_provider` -
`external_execution_id` - `idempotency_key` - `attempt_count` -
`started_at` - `completed_at` - `error_code` - `safe_error_message` -
`created_at` - `updated_at`

A document may have multiple `jobs` rows over its lifetime (e.g., a `normalize` job, an `ai_generation` job, and one or more `docx_render` jobs). Only FastAPI reads or writes the `jobs` table; neither n8n nor Python Worker connects directly to PostgreSQL.

Constraints/indexes: - Unique idempotency key within its intended
scope. - Index `(workspace_id, created_at)`. - Index
`(document_id, created_at)`. - Index
`(status, created_at)` for dispatch/recovery. - Index external execution
identifiers where useful.

### `job_attempts`

Optional but recommended if attempts need detailed history: - `id` -
`job_id` - `attempt_number` - `provider_execution_id` - `status` -
`started_at` - `completed_at` - `error_code` - `safe_metadata`

Do not store secrets or full source documents in job metadata.

### `workflow_callback_events`

Suggested fields: - `id` - `job_id` - `event_id` - `source_service` (`workflow`, `worker`) - `event_type` (`completed`, `failed`) -
`received_at` - `signature_valid` - `payload_hash` - `processed_at` -
`result_status`

Records callbacks received at `POST /api/v1/internal/workflows/callback` and `POST /api/v1/internal/workers/callback`. A unique constraint on `event_id` prevents replay and duplicate processing alongside the 5-minute HMAC-SHA256 timestamp check.

## 7. Credit and Payment Tables

### `credit_wallets`

Suggested fields: - `id` - `workspace_id` (unique) -
`available_balance` - `reserved_balance` - `updated_at`

Balances may be cached aggregates, but must reconcile to
ledger/reservation state. Complimentary beta credits are allocated via a configurable backend setting (initial configured amount is **TBD**, never hardcoded permanently) and recorded as a `grant` entry in `credit_transactions`.

### `credit_transactions`

Suggested fields: - `id` - `wallet_id` - `workspace_id` -
`transaction_type` (`purchase`, `grant`, `commit`, `release`, `refund`,
`adjustment`, `expiry`) - `amount` signed integer - `reference_type` -
`reference_id` - `idempotency_key` - `description` -
`created_by_user_id` - `created_at`

Append-only ledger. Corrections should use compensating entries rather
than mutating history.

### `credit_reservations`

Suggested fields: - `id` - `wallet_id` - `workspace_id` - `job_id` (nullable in M4 until `jobs` table is added in M5) -
`amount` - `status` (`reserved`, `committed`, `released`, `expired`) -
`expires_at` - `idempotency_key` - `created_at` - `resolved_at`

**Commit & Reconciliation Rules:**
-   Credit commitment (`status = committed` + `commit` ledger entry) occurs **only after a valid AI result has been persisted as a reviewable draft (`documents.status = review_required`)**.
-   DOCX rendering (`docx_render` job) failures must not cause a second AI charge, and re-rendering an already approved document version must not incur another AI-generation charge.
-   An idempotent reconciliation process must identify stale reservations (`expires_at < now()`). Reconciliation must acquire row locks (`SELECT ... FOR UPDATE`) and verify that the associated `jobs` record is genuinely no longer active before releasing or expiring a reservation; never release credits merely because a callback is late.

### `credit_packages`

Suggested fields: - `id` - `name` - `credits` - `price_minor_units` -
`currency` - `workspace_type` - `is_active` - `created_at` -
`updated_at`

Backend reads price and credit quantity from this table. Frontend values
are display-only.

### `payment_orders`

Suggested fields: - `id` - `workspace_id` - `user_id` - `package_id` -
`provider` - `provider_order_id` - `amount_minor_units` - `currency` -
`status` - `idempotency_key` - `created_at` - `paid_at` - `expired_at`

### `payment_webhook_events`

Suggested fields: - `id` - `provider` - `provider_event_id` or stable
event fingerprint - `payment_order_id` - `payload_hash` -
`signature_valid` - `received_at` - `processed_at` - `processing_status`

Never credit a wallet based solely on a frontend redirect.

### `subscriptions`

Suggested fields: - `id` - `workspace_id` - `provider` -
`provider_subscription_id` - `plan_id` - `status` -
`current_period_start` - `current_period_end` - `cancel_at_period_end` -
`created_at` - `updated_at`

Exact plan and provider schema are **TBD**.

## 8. Audit and Usage

### `audit_logs`

Suggested fields: - `id` - `workspace_id` - `actor_user_id` - `action` -
`resource_type` - `resource_id` - `metadata` JSONB (allowlisted,
non-sensitive) - `created_at`

Examples: role changed, invitation cancelled, template published,
document approved, credit adjustment.

### `usage_events`

Suggested fields: - `id` - `workspace_id` - `user_id` - `document_id` -
`job_id` - `provider` - `model` - `workflow_key` - `input_tokens` -
`output_tokens` - `estimated_cost` - `duration_ms` - `status` -
`created_at`

Do not store full document content in usage telemetry.

## 9. Tenant Isolation Rules

-   Every organization-owned record must carry a workspace relationship
    directly or through a validated parent.
-   Query by both resource ID and authorized workspace context where
    possible.
-   Never load by ID and assume the frontend-provided workspace is
    correct.
-   Background jobs re-check ownership and current state before
    processing.
-   Signed object keys must be checked against database ownership.
-   Consider PostgreSQL RLS as defense-in-depth after application
    authorization is tested.
-   Add explicit cross-tenant IDOR tests.

## 10. Transactions and Concurrency

Use transactions/row locks for: - Credit reservation. - Credit
commit/release/refund. - Payment webhook processing. - Invitation
acceptance. - Ownership transfer. - Approval and version changes. - Job
state transitions and callback idempotency.

The exact isolation level and lock strategy must be chosen during
implementation and tested under concurrent requests.

## 11. Migration Order (Aligned with Milestones & Foreign Keys)

Every foreign key must reference a table that already exists at that migration step:

1.  **Identity & Sessions (M2):** `users` and `auth_sessions` (`refresh_tokens`).
2.  **Workspaces & Access (M3):** `workspaces`, `workspace_members`, and `invitations` ( plus `audit_logs` if introduced in M3).
3.  **Credit Engine Foundation (M4):** `credit_wallets`, `credit_transactions`, `credit_reservations` (with `job_id` defined as a nullable `UUID` column without a foreign key constraint in M4 because `jobs` is introduced in M5), and `credit_packages`.
4.  **Templates Foundation (M5/M8):** `templates` and `template_versions` (created before `documents` so `documents.selected_template_version_id` can reference `template_versions.id`).
5.  **Documents & Versions (M5):**
    -   Create `documents` with `selected_template_version_id REFERENCES template_versions(id)` and nullable `current_version_id` and `approved_version_id` (initially without FK constraints to `document_versions`).
    -   Create `document_versions` with `document_id REFERENCES documents(id)`.
    -   Alter `documents` (`ALTER TABLE documents ADD CONSTRAINT ...`) to add foreign keys from `current_version_id` and `approved_version_id` to `document_versions(id)`.
6.  **Document Sources (M5):** `document_sources` (referencing `workspaces`, `documents`, and `document_versions`, with indexed `source_bundle_id`).
7.  **Jobs & Callbacks (M5/M6):**
    -   Create `jobs`, `job_attempts`, and `workflow_callback_events`.
    -   Alter `credit_reservations` (`ALTER TABLE credit_reservations ADD CONSTRAINT fk_credit_reservations_job_id FOREIGN KEY (job_id) REFERENCES jobs(id)`) now that `jobs` exists.
8.  **Usage & Audit Telemetry (M6+):** `usage_events` (and `audit_logs` if not already created in M3).
9.  **Payments & Subscriptions (Deferred / Post-Beta):** `payment_orders`, `payment_webhook_events`, and `subscriptions`.
