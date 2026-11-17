# Implementation Roadmap

**Status:** Draft v1.0\
**Strategy:** Documentation-first, local-first, milestone-based.\
**Rule:** Complete and review one milestone before starting the next.

## 1. Working Agreement

-   The Product Owner approves scope and milestones.
-   The System Architect maintains specifications and reviews decisions.
-   Antigravity implements approved work.
-   No milestone may silently redefine product requirements.
-   Every milestone ends with a report and a review pause.
-   Do not deploy or incur paid services without approval.

## 2. Milestones

### M0 --- Repository Audit and Documentation Alignment

Goal: - Inspect repository. - Read all docs. - Report existing files,
tooling, conflicts, missing decisions, and proposed implementation
order.

Constraints: - No application source changes. - No dependency
installation. - No cloud deployment. - No n8n workflow implementation.

Exit criteria: - Audit report reviewed and approved.

### M1 --- Foundation

Goal: - Establish backend, frontend, worker, and automation project
foundations. - Add environment configuration examples. - Add
lint/typecheck/test scaffolding. - Add local Docker Compose for
development dependencies where approved. - Add health checks.

Do not build full features yet.

Exit criteria: - Local development instructions work. - No secrets
committed. - CI checks run.

### M2 --- Authentication

Goal: - Registration, login, logout, refresh, current-user endpoint. -
Argon2id. - Refresh token rotation/revocation. - Email verification
integration can be stubbed for local development.

Exit criteria: - Auth tests and session security tests pass.

### M3 --- Workspace and RBAC

Goal: - Automatic Personal Workspace. - Organization creation. -
Membership and roles. - Invitations. - Workspace switching API. - Tenant
isolation tests.

Exit criteria: - Role matrix and cross-tenant tests pass.

### M4 --- Credit Engine Foundation

Goal: - Wallets, ledger, reservations. - Atomic
reserve/commit/release. - Idempotency and reconciliation. -
Complimentary development credits.

Do not enable live payment.

Exit criteria: - Concurrency and idempotency tests pass.

### M5 --- Document Foundation

Goal: - Document records and versions. - Source records. - Input
form/raw text models. - Upload intent and validation flow. - Job records
and state machine.

Exit criteria: - Structured/raw/hybrid input and tenant tests pass.

### M6 --- AI Document Engine

Goal: - Provider abstraction for Gemini. - WorkflowOrchestrator
interface. - Python fallback adapter. - n8n adapter in local
development. - Main router and first type-specific workflow. - Pydantic
result validation and provenance. - Secure callback.

Start with one document type (recommended: meeting minutes or a simple
letter), then expand.

Exit criteria: - Source-grounding tests pass. - Duplicate/invalid
callback tests pass. - Fallback adapter works.

### M7 --- Review and Approval

Goal: - Draft review UI. - Field status and evidence display. - Editing
and versioning. - Approval tied to exact version/hash. - Approval
invalidation after edits.

Exit criteria: - No render without approval.

### M8 --- Template Manager and DOCX Rendering

Goal: - Built-in template support. - Organization template upload and
validation. - Placeholder extraction and mapping. - Immutable template
versions. - Python DOCX renderer. - Secure generated-file storage.

Exit criteria: - Representative DOCX fixture suite passes.

### M9 --- Background Jobs and Reliability

Goal: - Durable dispatch. - Retry/backoff. - Worker callback. -
Reconciliation for stuck jobs and stale reservations. - Observability
and safe error reporting.

Some job infrastructure may be introduced earlier where needed; this
milestone hardens it.

Exit criteria: - Retry, duplicate delivery, failure, and recovery tests
pass.

### M10 --- Product UI Completion

Goal: - Dashboard. - Document list/history. - Template library. -
Workspace/member settings. - Credits/usage view. - Responsive/adaptive
UX. - Loading/empty/error states.

Exit criteria: - Critical E2E journeys pass.

### M11 --- Security and Release Readiness

Goal: - Security review. - IDOR and tenant isolation suite. - Upload
abuse tests. - Callback and credit race tests. - Backup/restore test. -
Privacy and retention review. - Cost review. - n8n licensing decision.

Exit criteria: - All critical acceptance gates pass.

### M12 --- Deployment

Goal: - Deploy approved components. - Configure secrets. - Set resource
and spend limits. - Configure backups and monitoring. - Verify health
checks and restore procedure. - Run staging smoke tests.

Deployment stack candidates are described in `MASTER_SPECIFICATION.md`.
Do not assume all services are free.

Exit criteria: - Staging is stable. - Production readiness is explicitly
approved.

## 3. Recommended First Vertical Slice

Do not implement all document types simultaneously.

Recommended first slice: 1. User creates document in Personal Workspace.
2. User selects a simple letter or meeting-minutes type. 3. User enters
structured form and text. 4. Backend creates job and reserves
complimentary test credits. 5. Local n8n workflow or Python fallback
generates structured draft. 6. FastAPI validates and stores draft. 7.
User reviews and approves. 8. Worker renders a built-in DOCX template.
9. User downloads the file.

This validates the core product path before adding subscriptions,
advanced templates, and every document type.

## 4. Local-First Sequence

1.  Verify Git repository and clean working tree.
2.  Install only the tools required for M1.
3.  Run PostgreSQL and n8n locally using Docker.
4.  Keep credentials in local `.env` files excluded from Git.
5.  Use mock AI by default.
6.  Test Gemini with dummy data only.
7.  Keep local n8n editor bound to localhost.
8.  Export workflows without credentials.
9.  Do not configure production hosting yet.

## 5. Suggested Commit Convention

Examples: - `chore: initialize repository structure` -
`docs: add master product specification` -
`feat(auth): implement registration and login` -
`feat(workspaces): add organization membership` -
`test(credits): cover concurrent reservations` -
`fix(workflow): reject duplicate callbacks`

Keep commits small and meaningful. Product Owner reviews diffs before
merge/push when practical.

## 6. Milestone Report Template

At the end of each milestone, the agent must report:

-   Goal and scope.
-   Files created/modified.
-   Important design decisions.
-   Database/API changes.
-   Commands executed.
-   Tests executed and exact results.
-   Known issues and limitations.
-   Security implications.
-   Deviations from specification.
-   Questions requiring Product Owner decision.
-   Recommended next milestone.

Then stop and wait for approval.

## 7. Deferred Work

Do not pull these into early milestones: - OCR for scanned images. -
Real-time collaboration. - Full Word editor. - Complex template layout
fidelity. - PDF preview. - Live payment. - Fine-tuning. - Vector
database/RAG. - Native mobile app. - Premature Kubernetes/microservices.
