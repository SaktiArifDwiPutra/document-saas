# Testing Strategy

**Status:** Draft v1.0\
**Approach:** Testing pyramid + security, concurrency, and recovery
testing.

## 1. Goals

-   Protect tenant isolation.
-   Preserve credit consistency.
-   Prevent unsupported AI facts from reaching final documents.
-   Ensure approved content maps to the correct template version.
-   Make asynchronous jobs recoverable.
-   Ensure generated DOCX files open and retain key layout.

## 2. Testing Pyramid

### Unit tests

Suggested tools: - Backend: Pytest. - Frontend: Vitest and React Testing
Library. - Worker: Pytest.

Test pure business logic: - Permission policy. - State transitions. -
Credit calculations. - Reservation lifecycle. - Schema validation. -
Placeholder parsing/mapping. - Workflow adapter behavior. - Error
mapping.

### Integration tests

-   FastAPI TestClient with test PostgreSQL.
-   Mock AI provider.
-   Mock n8n adapter and Python fallback adapter.
-   Mock object storage initially.
-   Selective staging R2 tests when configured.
-   Worker tests against representative DOCX/PDF/TXT fixtures.

### End-to-end tests

Use Playwright for critical journeys: - Register/login. - Workspace
switching. - Create document. - Submit inputs. - Review AI draft. - Edit
and approve. - Generate DOCX. - Download output. - Manage template and
organization members according to role.

### Security/reliability tests

-   Cross-tenant access and IDOR.
-   Token rotation/revocation.
-   Upload validation.
-   Callback signature/replay.
-   Credit race conditions.
-   Payment webhook replay if payment is enabled.
-   Job retry/duplicate delivery.
-   Signed URL expiry.
-   Backup restore.

## 3. Required Functional Test Areas

### 3.1 Authentication

-   Registration creates user and Personal Workspace.
-   Duplicate normalized email rejected.
-   Password hash is not plaintext; Argon2id constant-time dummy verification runs on nonexistent accounts and outdated hashes rehash transparently on login.
-   Dual per-IP and SHA-256 hashed per-account rate limiting (plus independent per-scope capacity pools, expired-bucket pruning before rejection, non-eviction of active buckets, fail-closed HTTP 429 saturation behavior and availability trade-offs, and PII-free saturation logging/metrics), noting in-memory scope is single-instance dev/beta only.
-   Email verification and password reset single-use enforcement, expiry, cross-type isolation, and prior-token supersession.
-   Refresh rotation, family reuse detection, expired/disabled-account rejection, and `Max-Age=0` cookie clearing on `401` refresh failures.
-   Frontend single-flight `/auth/refresh` coalescing, logout/refresh generation coordination, and latest `csrf_token` cookie resolution across concurrent and sequential refreshes (single-tab scope; multi-tab coordination deferred as a known production-readiness limitation).
-   Cookie-endpoint `Origin` / `Referer` allowlist, double-submit CSRF enforcement, and cookie attribute/path consistency (`refresh_token` `HttpOnly` on `/api/v1/auth`; readable `csrf_token` on `/`) on creation and deletion.
-   PostgreSQL concurrency tests (`SELECT ... FOR UPDATE` and atomic conditional updates) for concurrent `/auth/refresh`, `/auth/reset-password`, and `/auth/verify-email`.
-   Logout revokes session and enforces Origin/Referer + CSRF checks.
-   Password reset invalidates appropriate sessions.

### 3.2 Workspace and Organization Document Permissions

-   Every user has exactly one Personal Workspace.
-   Organization creation assigns correct Owner.
-   Invitations expire and are single-use.
-   Invitation email must match.
-   Admin cannot modify or transfer Owner.
-   Removed member loses access.
-   Removing member does not delete their documents.
-   Client-side active workspace switching (`GET /api/v1/workspaces` + passing `workspace_id` on subsequent requests) enforces backend authorization on every request and does not leak data.
-   **Organization Document Permission Matrix:**
    -   **Owner** and **Admin**: allowed full access (view, edit, approve, generate, delete) to all organization documents.
    -   **Editor**: allowed full access to own documents; allowed read-only (`GET`) access to other members' documents; denied edit, source submission, process, approve, generate, and delete on other members' documents.
    -   **Member**: allowed full access to own documents; denied read, edit, source submission, process, approve, generate, and delete on other members' documents.

### 3.3 Tenant isolation

For every major resource, attempt: - Read another workspace's
document. - Edit another workspace's document. - Download another
workspace's object. - Read another workspace's template. - Access
another workspace's job or `source_bundle_id`. - Manipulate workspace ID in request body/path.

All must be denied without leaking sensitive existence information where
practical.

### 3.4 Input and uploads

-   Document source upload intent (`POST /api/v1/workspaces/{workspace_id}/documents/{document_id}/sources/upload-intents`) and source confirmation/submission (`POST /api/v1/workspaces/{workspace_id}/documents/{document_id}/sources`).
-   Structured form input.
-   Raw text input.
-   Hybrid form + text + files.
-   DOCX/PDF/TXT supported path.
-   Wrong extension/MIME mismatch.
-   Invalid magic bytes.
-   Corrupt or malformed DOCX/PDF/ZIP rejected.
-   Oversized file.
-   Excessive archive expansion ratio / uncompressed byte cap and ZIP entry-count limit enforced.
-   Safe XML parsing enforced (DTDs and external entity resolution disabled).
-   Embedded/active content rejected.
-   External reference handling (never fetched).
-   Object key from another workspace or document rejected.

### 3.5 AI quality and safety (`meeting_minutes` first vertical slice)

Use deterministic fixtures and mocked outputs for core tests, starting with `meeting_minutes` (Notulen Rapat) as the first end-to-end vertical slice before expanding to other document types.

Test: - Missing data remains missing. - Conflicting dates/names are
flagged. - Critical values have provenance. - Minutes (`meeting_minutes`) extract meeting information, separate opinions/proposals from actual decisions, and identify action items and responsible persons only when supported by sources. - Minutes never invent meeting decisions, attendees, dates, responsible persons, or action items. - LPJ does not invent expenses or outcomes. -
Decision letter does not invent legal basis or signatory. -
Prompt-injection text in uploaded source is ignored as instruction. -
Invalid schema is rejected. - Overlong output is bounded. - Provider
timeout/rate limit handled. - AI output cannot approve itself.

Real Gemini testing must use dummy/non-sensitive documents unless the
applicable privacy terms permit the data.

### 3.6 Review and approval

-   Draft is editable.
-   Autosave conflict behavior is tested.
-   New edit creates/increments version.
-   Edit after approval invalidates approval.
-   Approval references exact content hash/version.
-   Generate without approval is rejected.
-   Unauthorized user cannot approve.

### 3.7 Templates and DOCX

Maintain a representative template fixture suite: - Simple letter. -
Multi-section report. - Tables. - Header/footer. - Image placeholder. -
Numbering. - Page break. - Multiple sections/orientation where
supported.

Test: - Placeholder extraction. - Placeholder split across Word runs. -
Missing mapping. - Duplicate/unknown placeholder. - Template version
immutability. - Document references exact version. - Generated DOCX
opens with supported office tooling. - Key layout elements remain
present.

Do not assert pixel-perfect fidelity for unsupported advanced Word
features.

### 3.8 Credits

-   Configurable complimentary beta credit allocation (loaded from backend configuration, never hardcoded permanently).
-   Reserve before processing.
-   Insufficient balance rejected.
-   Concurrent reservations cannot overspend.
-   Credit commit occurs **only after a valid AI result has been persisted as a reviewable draft (`documents.status = review_required`)**.
-   DOCX rendering (`docx_render`) failures do not cause a second AI charge.
-   Re-rendering an already approved document version does not incur another AI-generation charge.
-   Commit is idempotent.
-   Release is idempotent.
-   Refund uses compensating ledger entry.
-   Failed AI job releases reservation according to policy.
-   Duplicate callback cannot double-commit.
-   Stale reservation reconciliation checks job liveness with row locks and never releases credits merely because a callback is late while a job is still active.
-   Wallet balance reconciles with ledger.

### 3.9 Jobs, signed source retrieval, and callbacks

-   `documents.status` (`draft`, `processing`, `review_required`, `approved`, `rendering`, `completed`, `failed`) is tracked separately from individual asynchronous `jobs.status` (`normalize`, `ai_generation`, `docx_render`), and the human review pause does not keep an async job running.
-   Signed internal source retrieval (`GET /api/v1/internal/source-bundles/{source_bundle_id}`) enforces short-lived expiration, workspace/document/job binding, `HMAC-SHA256(secret, "{timestamp}.{event_id}.{METHOD}.{canonical_path}.{raw_body}")` verification binding `METHOD=GET` and exact `canonical_path` (including `source_bundle_id`), 5-minute timestamp tolerance, `event_id` replay protection, and rejects unauthenticated or path-mismatched requests.
-   Workflow (`POST /api/v1/internal/workflows/callback`) and Worker (`POST /api/v1/internal/workers/callback`) callbacks enforce `HMAC-SHA256(secret, "{timestamp}.{event_id}.{METHOD}.{canonical_path}.{raw_body}")`, constant-time signature comparison, 5-minute timestamp tolerance, `event_id` replay rejection, and payload size limits.
-   Duplicate dispatch.
-   Duplicate callback.
-   Out-of-order callback.
-   Invalid HMAC.
-   Stale timestamp (> 5 minutes).
-   Replay event ID.
-   Provider timeout.
-   Worker timeout.
-   Retry exhaustion.
-   Worker restart/recovery.
-   Job stuck in intermediate state.
-   Safe failure message returned to user.

### 3.10 Payments

Payments are disabled in initial beta. If sandbox integration is later
enabled: - Verify webhook signature. - Reject replay. - Duplicate
webhook is idempotent. - Frontend redirect does not grant credits. -
Backend package price is authoritative. - Failed/expired payment does
not credit wallet.

Live payment tests are not an MVP acceptance requirement.

## 4. CI Pipeline

Suggested pipeline: 1. Dependency install with lockfile. 2.
Formatting/lint. 3. Type checking. 4. Unit tests. 5. Integration tests.
6. Frontend build. 7. Backend/worker build checks. 8. Selected E2E
tests. 9. Security/dependency scanning as practical.

Do not deploy if required checks fail.

Exact CI provider and workflow configuration are **TBD**.

## 5. Test Data Policy

-   Use synthetic users and organization names.
-   Use dummy source documents.
-   Never commit real school/organization records.
-   Never include production credentials.
-   Keep test storage isolated.
-   Clean up test data deterministically.

## 6. MVP Acceptance Gates

The MVP cannot be considered ready unless: - No cross-workspace access
succeeds. - No negative/inconsistent credit balance under concurrency. -
No duplicate credit transaction on retries. - No final DOCX without
explicit approval. - No unsupported critical information is silently
invented. - Generated DOCX opens. - Failed jobs are recoverable and do
not leave permanent reservations. - Template version traceability
works. - Backup restore has been tested. - Production privacy and n8n
licensing decisions are resolved before public commercial use.
