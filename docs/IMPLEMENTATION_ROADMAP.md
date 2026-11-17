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
Membership and roles (including the Organization Document Permission Policy: Owner/Admin full access to all org documents; Editor full access to own documents and read-only to others'; Member full access only to own documents). - Invitations. - Client-side active workspace selection backed by `GET /api/v1/workspaces` and `GET /api/v1/workspaces/{workspace_id}` with backend membership/permission enforcement on every workspace-scoped request. - Tenant
isolation tests.

Exit criteria: - Role matrix and cross-tenant tests pass.

### M4 --- Credit Engine Foundation

Goal: - Wallets, ledger, reservations. - Atomic
reserve/commit/release/refund. - Credit commitment only after a valid AI draft is persisted as `review_required` (no second AI charge for DOCX render failures or re-rendering an already approved version). - Idempotency and row-locked stale reservation reconciliation (verifying job liveness before releasing). -
Configurable complimentary beta credits (backend configuration mechanism; initial configured amount and tariffs remain **TBD**; never hardcode a permanent amount or invent a tariff/price).

Do not enable live payment.

Exit criteria: - Concurrency and idempotency tests pass.

### M5 --- Document Foundation

Goal: - Document records, versions, and `documents.status` lifecycle (`draft`, `processing`, `review_required`, `approved`, `rendering`, `completed`, `failed`, `archived`). - Source records and workspace-bound `source_bundle_id`. - Input
form/raw text models. - Document source upload intent (`POST /api/v1/workspaces/{workspace_id}/documents/{document_id}/sources/upload-intents`), upload confirmation/source submission (`POST /api/v1/workspaces/{workspace_id}/documents/{document_id}/sources`), and content validation flow. - Separate asynchronous `jobs` records (`normalize`, `ai_generation`, `docx_render`) and `jobs.status` state machine (human review is never represented as a continuously running job).

Exit criteria: - Structured/raw/hybrid input, organization document permission policy, and tenant tests pass.

### M6 --- AI Document Engine

Goal: - Provider abstraction for Gemini. - WorkflowOrchestrator
interface. - Python fallback adapter. - n8n adapter in local
development. - Signed internal source retrieval (`GET /api/v1/internal/source-bundles/{source_bundle_id}`). - Main router and the approved first vertical slice workflow: `meeting_minutes` (Notulen Rapat). - Pydantic
result validation, opinion/proposal/decision separation, missing/conflict detection, and provenance. - Secured callbacks (`POST /api/v1/internal/workflows/callback` and `POST /api/v1/internal/workers/callback`) with HMAC-SHA256, 5-minute timestamp tolerance, and `event_id` replay protection.

Implement and validate `meeting_minutes` (Notulen Rapat) first. Do not expand implementation to other document types before this slice passes its acceptance criteria.

Exit criteria: - Source-grounding tests pass. - Duplicate/invalid
callback and signed source retrieval tests pass. - Fallback adapter works.

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

## 3. Approved First Vertical Slice: `meeting_minutes` (Notulen Rapat)

Do not implement all document types simultaneously. Do not expand implementation to other document types before `meeting_minutes` passes its acceptance criteria.

The `meeting_minutes` (Notulen Rapat) end-to-end vertical slice must cover:
1.  **Source input and normalization:** User creates a `meeting_minutes` document and provides form, raw text, and/or supported uploaded files (`DOCX`, `PDF`, `TXT`); Python Worker normalizes uploaded files and reports via `POST /api/v1/internal/workers/callback`.
2.  **Extraction of meeting information:** FastAPI reserves configurable complimentary test credits, creates the `ai_generation` job and workspace-bound `source_bundle_id`, and dispatches minimal metadata; n8n retrieves normalized sources via `GET /api/v1/internal/source-bundles/{source_bundle_id}` and extracts meeting metadata, attendees, and agenda.
3.  **Separation of opinions, proposals, and actual decisions:** Never treat a suggestion or opinion as an approved meeting decision unless supported by the source.
4.  **Identification of action items and responsible persons:** Extract action items, owners, and due dates only when supported by sources.
5.  **Missing and conflicting information detection:** Flag missing critical fields and conflicting source statements.
6.  **Source provenance:** Attach provenance references for important extracted fields.
7.  **Human review and editing:** FastAPI validates the callback (`POST /api/v1/internal/workflows/callback`), persists the draft (`documents.status = review_required`), and commits reserved AI credits; user reviews and edits the draft.
8.  **Explicit user approval:** User approves the exact draft version/hash (`documents.status = approved`).
9.  **DOCX rendering using the selected template:** FastAPI creates a `docx_render` job (no second AI charge); Python Worker renders the approved content into the selected template and reports via `POST /api/v1/internal/workers/callback`.
10. **Download and document history:** User obtains a short-lived signed download URL and views document versions and provenance history.

Never invent meeting decisions, attendees, dates, responsible persons, or action items.

This validates the core product path before adding subscriptions,
advanced templates, and additional document types.

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
