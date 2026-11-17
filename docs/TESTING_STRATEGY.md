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
-   Password hash is not plaintext.
-   Login rate limit.
-   Email verification behavior.
-   Refresh rotation.
-   Logout revokes session.
-   Password reset invalidates appropriate sessions.

### 3.2 Workspace

-   Every user has exactly one Personal Workspace.
-   Organization creation assigns correct Owner.
-   Invitations expire and are single-use.
-   Invitation email must match.
-   Admin cannot modify or transfer Owner.
-   Removed member loses access.
-   Removing member does not delete their documents.
-   Workspace switching does not leak data.

### 3.3 Tenant isolation

For every major resource, attempt: - Read another workspace's
document. - Edit another workspace's document. - Download another
workspace's object. - Read another workspace's template. - Access
another workspace's job. - Manipulate workspace ID in request body/path.

All must be denied without leaking sensitive existence information where
practical.

### 3.4 Input and uploads

-   Structured form input.
-   Raw text input.
-   Hybrid form + text + files.
-   DOCX/PDF/TXT supported path.
-   Wrong extension/MIME mismatch.
-   Invalid magic bytes.
-   Corrupt DOCX/ZIP.
-   Oversized file.
-   Excessive archive expansion.
-   Embedded/active content.
-   External reference handling.
-   Object key from another workspace.

### 3.5 AI quality and safety

Use deterministic fixtures and mocked outputs for core tests.

Test: - Missing data remains missing. - Conflicting dates/names are
flagged. - Critical values have provenance. - Minutes distinguish
proposal from decision. - LPJ does not invent expenses or outcomes. -
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

-   Reserve before processing.
-   Insufficient balance rejected.
-   Concurrent reservations cannot overspend.
-   Commit is idempotent.
-   Release is idempotent.
-   Refund uses compensating ledger entry.
-   Failed job releases reservation according to policy.
-   Duplicate callback cannot double-commit.
-   Stale reservation reconciliation.
-   Wallet balance reconciles with ledger.

### 3.9 Jobs and callbacks

-   Duplicate dispatch.
-   Duplicate callback.
-   Out-of-order callback.
-   Invalid HMAC.
-   Stale timestamp.
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
