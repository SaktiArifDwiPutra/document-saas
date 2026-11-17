# AI Agent Development Guidelines

**Audience:** Antigravity coding agent and any future development agent\
**Status:** Draft v1.0

## 1. Mission

Implement the product described in `MASTER_SPECIFICATION.md` and the
supporting technical documents. Do not redesign settled product
decisions without explicit approval.

The human Product Owner makes product decisions and approves milestones.
The System Architect maintains specifications and reviews technical
direction. The coding agent implements only the approved milestone.

## 2. Mandatory Reading Order

Before modifying code, read:

1.  `docs/MASTER_SPECIFICATION.md`
2.  `docs/SYSTEM_ARCHITECTURE.md`
3.  `docs/DATABASE_DESIGN.md`
4.  `docs/API_CONTRACT.md`
5.  `docs/N8N_WORKFLOW_DESIGN.md`
6.  `docs/TESTING_STRATEGY.md`
7.  `docs/IMPLEMENTATION_ROADMAP.md`
8.  This file.

Also inspect the existing repository and report inconsistencies.

## 3. Non-Negotiable Rules

-   Do not invent product requirements.
-   Mark unresolved decisions as TBD and ask before implementing them.
-   Do not silently change architecture.
-   FastAPI remains the core authority.
-   n8n is replaceable and cannot own auth, permissions, credits,
    payment, approval, or application DB state.
-   Python Worker handles technical file processing and DOCX rendering.
-   All AI outputs are untrusted until backend validation.
-   No final DOCX without explicit human approval.
-   No unsupported critical facts may be silently invented.
-   Every workspace-owned resource must enforce tenant isolation.
-   Never trust frontend IDs, prices, credit counts, payment state,
    role, or approval flags.
-   Never commit secrets, credentials, access tokens, or real user
    documents.
-   Never use real confidential documents with free AI tiers unless
    privacy terms explicitly permit it.
-   Do not deploy or incur paid services without explicit Product Owner
    approval.

## 4. Workflow for Every Task

1.  Read the relevant specification.
2.  Inspect the existing code and tests.
3.  State the intended files and changes.
4.  Identify assumptions and unresolved decisions.
5.  Implement only the approved scope.
6.  Add/update tests.
7.  Run relevant tests, lint, type checks, and build.
8.  Inspect the diff for unrelated changes and secrets.
9.  Update documentation if behavior or contract changes.
10. Report files changed, commands run, results, limitations, and next
    step.

Do not claim tests passed unless they were actually executed and their
exit status was checked.

## 5. Repository Safety

-   Do not delete or overwrite valuable existing files without
    inspection.
-   Do not reformat unrelated files.
-   Do not force-push.
-   Do not rewrite Git history unless explicitly approved.
-   Do not commit automatically unless explicitly requested.
-   Keep changes small and reviewable.
-   Prefer one feature or coherent unit per commit.
-   Use conventional commit messages.

## 6. Architecture Rules

### Backend

-   Keep routers thin; put business logic in services/use cases.
-   Use dependency injection for auth, database, storage, AI, and
    orchestration.
-   Centralize permission checks.
-   Use explicit transaction boundaries.
-   Validate all external inputs.
-   Use typed request/response schemas.
-   Do not expose ORM models directly as public API contracts.
-   Do not let n8n or Worker connect to the core database unless a
    future approved design explicitly requires it.

### Frontend

-   Frontend is not an authorization boundary.
-   Do not duplicate business rules as the source of truth.
-   Show real backend states.
-   Handle loading, empty, error, and permission states.
-   Avoid fake progress percentages.
-   Keep UI accessible and responsive.
-   Do not add decorative complexity without user value.

### Worker

-   Make tasks idempotent.
-   Validate object ownership and expected content.
-   Use bounded memory and processing limits.
-   Never fetch arbitrary external URLs from document content.
-   Report safe structured outcomes.

### n8n

-   Use versioned workflow keys.
-   Keep credentials in secure credential storage.
-   Never embed secrets in exported workflow JSON.
-   Do not use unreviewed community nodes.
-   Avoid unnecessary storage of raw source text in execution history.
-   Treat workflow execution as untrusted until FastAPI validates the
    callback.

## 7. Security Requirements

-   Argon2id for password hashing.
-   Short-lived access tokens and hashed/rotated refresh tokens.
-   CSRF/origin protections for cookie-authenticated endpoints.
-   Tenant isolation on all reads/writes and background jobs.
-   Private object storage and short-lived signed URLs.
-   Upload validation based on content, not filename alone.
-   HMAC or equivalent authentication for internal callbacks.
-   Idempotency and replay protection.
-   Rate limiting for authentication, uploads, and AI processing.
-   Structured logs without secrets or full document text.
-   Audit sensitive operations.
-   Add security tests for IDOR, role bypass, callback replay, upload
    abuse, and credit concurrency.

## 8. AI Safety and Factual Integrity

-   Source documents are untrusted data, never instructions.
-   Separate trusted prompt instructions from source content.
-   AI must not invent missing fields.
-   Critical values require evidence or explicit user confirmation.
-   Distinguish opinions, proposals, and actual decisions in minutes.
-   Do not invent expenses, outcomes, participants, legal bases, or
    signatories.
-   Schema validation does not establish truth.
-   Preserve provenance references.
-   Do not give AI direct database/payment/authorization tools.

## 9. Testing Expectations

-   Unit tests for pure logic.
-   Integration tests for API/database/service boundaries.
-   E2E tests for critical user journeys.
-   Security and concurrency tests.
-   Use mocks for AI, queue, and storage where appropriate.
-   Use dummy data for real provider experiments.
-   Test retry, duplicate events, timeout, and partial failure.
-   Do not weaken tests simply to make a milestone pass.

## 10. Dependency and Tooling Policy

-   Prefer maintained, widely used dependencies.
-   Explain why a new dependency is needed.
-   Avoid adding infrastructure before it is required.
-   Pin or constrain versions appropriately.
-   Keep secrets in environment variables or secret managers.
-   Provide `.env.example` with placeholders only.
-   Do not commit `.env`.
-   Do not install global tools without approval.

## 11. Definition of Done

A task is complete only when: - Scope matches the approved milestone. -
Relevant tests pass. - Static checks/build pass where applicable. -
Security implications are reviewed. - Documentation is updated. - No
secrets or unrelated changes are present. - Agent reports exact
verification performed.

## 12. Required Agent Report

At the end of each task, report:

1.  Summary.
2.  Files created/modified.
3.  Architecture decisions or deviations.
4.  Commands executed.
5.  Tests and exact outcomes.
6.  Known limitations.
7.  Risks or unresolved questions.
8.  Recommended next task.

Stop and wait for Product Owner approval before starting the next
milestone.
