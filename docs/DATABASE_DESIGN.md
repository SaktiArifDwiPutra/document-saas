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
`document_type` - `title` - `status` - `current_version_id` -
`selected_template_version_id` - `approved_version_id` - `created_at` -
`updated_at` - `deleted_at`

A document belongs to one workspace. Every read/write must authorize
membership and document-level permissions.

### `document_versions`

Suggested fields: - `id` - `document_id` - `version_number` -
`structured_content` (JSONB) - `content_hash` - `created_by_user_id`
nullable for system-generated draft - `created_at` - `approval_status` -
`approved_by_user_id` - `approved_at`

Do not overwrite approved content in place. Create a new version when
edits occur.

### `document_sources`

Suggested fields: - `id` - `document_id` - `document_version_id`
nullable - `source_type` (`form`, `text`, `upload`,
`workspace_reference`) - `storage_object_key` nullable - `source_name` -
`extracted_text_reference` or controlled text storage - `content_hash` -
`metadata` JSONB - `created_at`

Store only what is needed. Apply retention rules to original uploads and
extracted text.

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
(`normalize`, `ai_generation`, `docx_render`) - `status` -
`workflow_key` - `workflow_version` - `workflow_provider` -
`external_execution_id` - `idempotency_key` - `attempt_count` -
`started_at` - `completed_at` - `error_code` - `safe_error_message` -
`created_at` - `updated_at`

Constraints/indexes: - Unique idempotency key within its intended
scope. - Index `(workspace_id, created_at)`. - Index
`(status, created_at)` for dispatch/recovery. - Index external execution
identifiers where useful.

### `job_attempts`

Optional but recommended if attempts need detailed history: - `id` -
`job_id` - `attempt_number` - `provider_execution_id` - `status` -
`started_at` - `completed_at` - `error_code` - `safe_metadata`

Do not store secrets or full source documents in job metadata.

### `workflow_callback_events`

Suggested fields: - `id` - `job_id` - `event_id` - `event_type` -
`received_at` - `signature_valid` - `payload_hash` - `processed_at` -
`result_status`

Unique event ID prevents replay/duplicate processing.

## 7. Credit and Payment Tables

### `credit_wallets`

Suggested fields: - `id` - `workspace_id` (unique) -
`available_balance` - `reserved_balance` - `updated_at`

Balances may be cached aggregates, but must reconcile to
ledger/reservation state.

### `credit_transactions`

Suggested fields: - `id` - `wallet_id` - `workspace_id` -
`transaction_type` (`purchase`, `grant`, `commit`, `release`, `refund`,
`adjustment`, `expiry`) - `amount` signed integer - `reference_type` -
`reference_id` - `idempotency_key` - `description` -
`created_by_user_id` - `created_at`

Append-only ledger. Corrections should use compensating entries rather
than mutating history.

### `credit_reservations`

Suggested fields: - `id` - `wallet_id` - `workspace_id` - `job_id` -
`amount` - `status` (`reserved`, `committed`, `released`, `expired`) -
`expires_at` - `idempotency_key` - `created_at` - `resolved_at`

A reconciliation process must identify stale reservations. Expiry must
not silently destroy a valid in-progress job.

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

## 11. Migration Order (Proposed)

1.  users and auth sessions
2.  workspaces and memberships
3.  invitations
4.  documents and document versions
5.  document sources
6.  templates and template versions
7.  jobs, attempts, callback events
8.  credit wallets, transactions, reservations
9.  payment orders, webhook events, subscriptions
10. audit logs and usage events

This order may change to satisfy foreign-key dependencies.
