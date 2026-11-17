# AI Document Generator SaaS --- Master Specification

**Document status:** Draft v1.0\
**Product status:** Pre-implementation\
**Primary language:** Indonesian-first user experience; English
technical documentation\
**Owner:** Product Owner (user)\
**Architecture:** Hybrid --- FastAPI core + n8n AI orchestration +
Python Worker

------------------------------------------------------------------------

## 1. Product Overview

The product is a multi-workspace SaaS that helps individuals and
organizations create formal documents from structured forms, pasted
text, meeting notes, transcripts, and supported uploaded files.

The system uses AI to extract and organize source information into a
structured document draft. A human must review and approve the draft
before the system generates the final DOCX using a selected Word
template.

The product is intended to reduce repetitive document preparation while
preserving user control, source traceability, and template formatting.

### 1.1 Product principles

1.  **Source-grounded:** never invent facts that are absent from source
    material.
2.  **Human-approved:** AI output is a draft, not an authoritative final
    document.
3.  **Document-first:** the primary output is a usable DOCX, not an AI
    chat.
4.  **Workspace isolation:** personal and organization data must remain
    separated.
5.  **Transparent usage:** show credit cost and processing status
    clearly.
6.  **Modular AI:** AI providers and workflow orchestrators must be
    replaceable.
7.  **Free-first development:** use free or low-cost services where
    practical, without promising that production will always cost zero.
8.  **Solo-developer maintainability:** avoid premature microservices
    and enterprise complexity.

## 2. Target Users and Rollout

### Initial target

School organizations and their members. Initial distribution is expected
to be direct onboarding rather than mass-market viral growth.

### Later target

Other student organizations, clubs, communities, small businesses, and
individuals who regularly prepare formal documents.

### Rollout strategy

-   Local development and internal testing.
-   Small closed beta with dummy or non-sensitive data.
-   Controlled pilot with selected organizations.
-   Production launch only after privacy, reliability, licensing, and
    cost questions are resolved.

## 3. Core User Journey

1.  User signs in.
2.  User selects Personal Workspace or an Organization Workspace (client-side active workspace selection; backend authorizes every workspace-scoped request).
3.  User selects a document type (first vertical slice: `meeting_minutes` --- Notulen Rapat).
4.  User supplies information using a structured form, pasted text, supported files, or a hybrid of these.
5.  Backend creates a document processing job (`normalize` / `ai_generation`), transitions `documents.status` to `processing`, and atomically reserves the required credits.
6.  Python Worker (Input Normalizer) prepares and validates uploaded source material and reports completion via an authenticated callback (`POST /api/v1/internal/workers/callback`).
7.  FastAPI generates or identifies a workspace-bound `source_bundle_id` and dispatches minimal job metadata to the configured workflow orchestrator.
8.  n8n retrieves normalized source data from FastAPI via a short-lived signed internal endpoint and coordinates the AI workflow and Gemini provider.
9.  The AI result is returned to FastAPI through a secured callback (`POST /api/v1/internal/workflows/callback`).
10. FastAPI validates signature, timestamp freshness, replay protection, schema, provenance, critical fields, and job identity, persists the reviewable draft (`documents.status = review_required`), and commits the reserved AI credits.
11. User reviews and edits the draft (paused in `review_required` document state; no background job stays running during human review).
12. User explicitly approves the content (`documents.status = approved`).
13. User requests DOCX generation; FastAPI creates a `docx_render` job (`documents.status = rendering`) without charging a second AI credit, and Python Worker renders the approved structured content into the selected DOCX template.
14. Python Worker stores the final DOCX privately in R2 and notifies FastAPI via `POST /api/v1/internal/workers/callback`; FastAPI marks the render job and document `completed` and issues short-lived download URLs on authorized request.
15. Job usage, credit transactions, document versions, and audit events are recorded.

## 4. Document Types

The initial taxonomy is an MVP product taxonomy, not a claim that it
covers every legal or administrative document type.

### 4.1 Personal and organization document types

-   Surat Permohonan
-   Proposal (personal, business, funding, cooperation)
-   Surat Undangan
-   Surat Pengajuan
-   Generic document

### 4.2 Organization-focused document types

-   Notulen Rapat
-   Laporan Kegiatan
-   LPJ
-   Surat Keputusan
-   AD/ART
-   SOP organisasi
-   Proposal Kegiatan
-   Surat Undangan Resmi

### 4.3 Workflow-specific behavior

-   Meeting minutes (`meeting_minutes` --- Notulen Rapat) is the approved **first end-to-end vertical slice** to implement and validate before expanding to any other document type. It must cover:
    1. Source input and normalization.
    2. Extraction of meeting information.
    3. Separation of opinions, proposals, and actual decisions.
    4. Identification of action items and responsible persons when supported by sources.
    5. Missing and conflicting information detection.
    6. Source provenance for important extracted fields.
    7. Human review and editing.
    8. Explicit user approval.
    9. DOCX rendering using the selected template.
    10. Download and document history.
    Never invent meeting decisions, attendees, dates, responsible persons, or action items.
-   Reports and LPJ must not invent expenses, participants, outcomes, or
    evidence.
-   Decision letters must not invent legal bases, official numbers,
    signatories, or approval authority.
-   Proposals vary by subtype and must request missing essential
    information.
-   Generic documents must not silently claim a formal legal status.

## 5. Workspace Model

Every user receives a Personal Workspace automatically.

### 5.1 Personal Workspace

-   One owner.
-   No member management.
-   Built-in application templates.
-   Individual credit wallet.
-   One-time credit packs may be offered later.
-   No organization subscription requirement.

### 5.2 Organization Workspace

-   Multiple members.
-   Role-based access control.
-   Shared credit wallet.
-   Built-in and organization-uploaded templates.
-   Organization settings, usage, and subscription.
-   Members may belong to multiple organizations with different roles.

### 5.3 Roles

  -------------------------------------------------------------------------------------
  Capability                       Owner          Admin         Editor           Member
  ----------------------- -------------- -------------- -------------- ----------------
  Full workspace access              Yes             No             No               No

  Transfer ownership                 Yes             No             No               No

  Delete workspace                   Yes             No             No               No

  Manage members                     Yes    Yes, except             No               No
                                                  Owner

  Change roles except                Yes    Yes, within             No               No
  Owner                                          policy

  Manage workspace                   Yes Yes, permitted             No               No
  settings                                     settings

  Manage organization                Yes            Yes            Yes               No
  templates

  Create documents                   Yes            Yes            Yes              Yes

  Own documents (view,               Yes            Yes            Yes              Yes
  edit, approve,
  generate, delete)

  Other members'                     Yes            Yes      Read-only        No access
  documents (view, edit,     (full access)  (full access)      (view only)
  approve, generate,
  delete)

  View workspace                     Yes            Yes            Yes       Limited or
  credits/usage                                                          policy-defined
  -------------------------------------------------------------------------------------

**Approved Organization Document Permission Policy:**
-   **Owner:** Full access to all organization documents (view, edit, approve, generate, delete).
-   **Admin:** Full access to all organization documents (view, edit, approve, generate, delete).
-   **Editor:** Full access to documents they own; read-only access to other members' documents. Must not edit, approve, generate, or delete documents owned by other members.
-   **Member:** Full access only to documents they own; no access to other members' documents. Must not view, edit, approve, generate, or delete documents owned by other members.

Backend authorization is authoritative. Never infer permission from frontend visibility.

### 5.4 Membership, invitations, and workspace selection

-   Invitations use single-use random tokens stored as hashes.
-   Invitations expire.
-   Invitations can be cancelled.
-   Acceptance requires the intended email identity to match.
-   Removing a member does not delete documents created by that member.
-   Workspace IDs supplied by the client never establish authorization.
-   Workspace switching is a client-side active workspace selection: the frontend lists accessible workspaces (`GET /api/v1/workspaces`), the user selects an active workspace, and subsequent requests pass the selected `workspace_id`, with FastAPI validating membership and permissions on every request.

## 6. Credits and Monetization

### 6.1 Model

-   Personal Workspace: individual credit balance; no subscription
    required.
-   Organization Workspace: shared credit pool; subscription may be
    offered.
-   Organization credit packs may supplement subscription allowance.
-   Purchased credits do not expire arbitrarily.
-   Promotional credits may have explicit terms and expiry.

### 6.2 Credit engine requirements

-   Provide a backend configuration mechanism for complimentary beta credits (do not hardcode a permanent complimentary credit amount; the initial configured amount remains **TBD** until explicitly decided by the Product Owner).
-   Reserve credits before billable AI processing begins.
-   Use atomic database transactions and row locking to prevent
    overspending.
-   Maintain an immutable or append-only transaction ledger.
-   Make reservation, commit, release, and refund operations idempotent.
-   Never allow negative balance.
-   Release reservations on qualifying failures.
-   Do not commit credits merely because n8n sent a callback.
-   **Credit Commit Boundary:** Credit commitment occurs **only after a valid AI result has been validated by FastAPI and persisted as a reviewable draft (`documents.status = review_required`)**.
-   DOCX rendering failures must not cause a second AI charge.
-   Re-rendering an already approved document version must not incur another AI-generation charge.
-   **Stranded Reservation Protection:** Reservations must carry an expiration timestamp (`expires_at`) and be covered by idempotent background reconciliation. Reconciliation must never release credits merely because a callback is late; it must verify with row locks that the associated job is genuinely no longer active before releasing or expiring a reservation.
-   Expose cost estimate before generation (exact credit tariff and monetary pricing remain **TBD**; never invent a tariff or price).

### 6.3 Monetization status

-   Pricing, credit tariffs, and initial configured complimentary beta credit amount: **TBD** (controlled via backend configuration, never hardcoded as a permanent policy).
-   Payment provider: Midtrans is a candidate, not a final decision.
-   Live payments are disabled for initial beta.
-   Use configurable complimentary credits or sandbox testing during development.
-   Never trust price or credit quantity sent by the frontend.
-   Payment eligibility and merchant account requirements must be
    handled lawfully; involve an eligible adult/guardian if a provider
    requires an adult account.

## 7. AI Document Engine

### 7.1 Processing stages

1.  Input Normalizer
2.  Document Router
3.  Type-specific workflow
4.  Extraction and structuring
5.  Validation Engine
6.  Provenance/evidence association
7.  Human Review
8.  Approval
9.  DOCX Rendering

### 7.2 Source and truth rules

-   Use only user-provided sources and authorized workspace context.
-   Missing information must remain missing/null.
-   Do not fabricate names, dates, amounts, decisions, legal bases,
    participants, or outcomes.
-   Important fields must have evidence/provenance where possible.
-   Distinguish `provided`, `extracted`, `inferred`, `missing`,
    `conflict`, and `needs_review`.
-   Critical fields such as dates, names, amounts, official numbers,
    signatories, and decisions cannot be accepted from unsupported
    inference.
-   Pydantic/JSON Schema validates structure, not factual truth.
-   Uploaded files are untrusted data, not instructions. Ignore embedded
    prompt-injection attempts.
-   AI has no direct database, wallet, payment, or authorization tools.
-   Do not fine-tune for MVP.
-   RAG/vector database is deferred; later it may support approved
    bylaws, SOPs, and style guides.

### 7.3 Human review

-   AI output remains a draft until explicitly approved.
-   User can edit fields and content.
-   Editing after approval invalidates the approval.
-   Show missing/conflicting information and source evidence.
-   Preserve document versions and audit events.
-   No final DOCX may be generated without approval.

## 8. Template Manager and DOCX Renderer

### 8.1 Template sources

-   Built-in templates maintained by the application.
-   Organization templates uploaded by authorized organization roles.
-   Personal Workspace uses built-in templates only.
-   A document stores the exact immutable `template_version_id` used.

### 8.2 Placeholder model

Examples:

-   `{{letter_number}}`
-   `{{recipient_name}}`
-   `{{event_name}}`
-   `{{event_date}}`
-   `{{event_location}}`

Word frequently splits a single visible `{{placeholder}}` across multiple `<w:r>` XML runs during editing. The template validator/renderer must either implement a robust run-normalization pass (merging adjacent XML runs within `{{...}}` delimiters before parsing/rendering) or strictly detect and reject templates with split/unsupported placeholders at upload validation time with a clear actionable error.

### 8.3 Upload validation

Validate extension, MIME type, magic bytes, size, ZIP structure, entry count, uncompressed byte size / expansion ratio (zip-bomb protection), corruption, embedded/active content (macros/OLE), external references, safe XML parsing (disabling DTDs and external entity resolution), and placeholder syntax. Reject malformed files and never fetch external references from uploaded documents. Treat all uploaded files and extracted text as untrusted data.

Initial upload limits: - Maximum 10 MB per file. - Maximum 5 source
files per document. - Allowed source types: DOCX, PDF, TXT. - Legacy
DOC, arbitrary ZIP, executable, and unsupported formats are rejected. -
These limits are initial defaults and may be adjusted after testing.

### 8.4 Rendering

Use Python `docxtpl` and `python-docx` where appropriate.

Preserve, as far as practical: - Fonts and text styles - Colors -
Margins - Headers and footers - Tables - Images - Numbering - Page
breaks - Sections and orientation - Signature areas

Do not promise perfect compatibility with every Word feature. Complex
SmartArt, OLE objects, advanced layout features, and unusual field codes
are outside MVP guarantees.

PDF preview is deferred. MVP output is DOCX.

Template versions are immutable. Old versions remain archived while
referenced by documents.

## 9. User Experience

### 9.1 Product design direction

-   Professional, minimalist SaaS.
-   Indonesian-first.
-   Document-first.
-   Responsive and adaptive.
-   Avoid excessive gradients, decorative dashboards, and generic AI
    visual styling.
-   Accessible contrast and keyboard-friendly controls.

### 9.2 Navigation

Desktop sidebar; mobile drawer or adaptive navigation.

Main areas: - Dashboard - Buat Dokumen - Dokumen Saya - Template -
Workspace: Anggota - Kredit & Penggunaan - Billing & Subscription -
Pengaturan

Hide organization-only navigation in Personal Workspace. Account
settings remain separate from workspace settings.

### 9.3 Document creation wizard

1.  Choose document type.
2.  Provide structured form/raw text/files.
3.  Select template.
4.  Process with AI.
5.  Review and edit.
6.  Approve.
7.  Generate DOCX.

### 9.4 Review UI

-   Two-panel review on desktop where useful.
-   Tabs or stacked panels on mobile.
-   Show missing/conflicting fields and evidence.
-   Autosave.
-   Undo/redo for supported editing operations.
-   Version history.
-   Approval invalidation after edits.
-   Do not build a full Microsoft Word clone.

### 9.5 Progress

Show real job states, not fabricated percentage progress.

## 10. API and Platform

-   REST API under `/api/v1`.
-   JSON request/response.
-   Bearer access token.
-   Refresh token in Secure, HttpOnly cookie.
-   Page-based pagination initially.
-   Standardized error response.
-   OpenAPI documentation.
-   Async processing for long-running AI and rendering jobs.
-   Uploads use short-lived presigned R2 URLs.
-   Frontend uploads directly to R2 and confirms upload with backend.
-   Backend validates object ownership and file content.

See `API_CONTRACT.md` for endpoint details.

## 11. Initial Technology Decisions

  -----------------------------------------------------------------------
  Layer                   Selected direction      Notes
  ----------------------- ----------------------- -----------------------
  Frontend                Next.js static export   Cloudflare Pages
                                                  target; no SSR, Server
                                                  Actions, middleware, or
                                                  API routes. Dynamic
                                                  document resolution and
                                                  auth happen client-side
                                                  via `/api/v1`

  API                     FastAPI                 Core authority and sole
                                                  owner of PostgreSQL DB

  AI orchestration        n8n adapter             Replaceable; retrieves
                                                  sources via signed
                                                  internal API; license
                                                  approval required
                                                  before commercial
                                                  production

  Fallback orchestration  Python adapter          Keeps product usable
                                                  without n8n

  Worker                  Python                  File normalization and
                                                  DOCX rendering; reports
                                                  via signed callback (no
                                                  direct DB access)

  Database                PostgreSQL              Neon Free for early
                                                  development candidate

  Object storage          Cloudflare R2           Private objects

  Queue                   Cloud Tasks             Durable dispatch/retry
                                                  candidate

  AI                      Gemini provider adapter Free tier only with
                                                  dummy/non-sensitive
                                                  data

  Payments                Midtrans candidate      Disabled in beta

  Deployment              Cloudflare Pages +      Requires same
                          Cloud Run + Neon + R2   registrable domain for
                                                  cookie/CSRF alignment;
                                                  validate quotas/costs
  -----------------------------------------------------------------------

Cloud Run requires a billing account and may incur costs outside
applicable free usage. Free tiers and provider limits can change.
Re-check official pricing and quotas before deployment.

## 12. Security and Reliability

-   Password hashing: Argon2id.
-   Short-lived access JWT (approximately 15 minutes as an initial
    target).
-   Opaque random refresh token, stored hashed, rotated and revocable.
-   Secure, HttpOnly, SameSite cookie policy; CSRF and origin validation
    for cookie-authenticated endpoints. In static frontend deployment
    (Cloudflare Pages + Cloud Run API), serve frontend and API under the
    same registrable domain so `HttpOnly` refresh cookies and `Origin`/CSRF
    protections work without third-party cookie issues.
-   Workspace isolation enforced in every API and background job.
-   PostgreSQL RLS may be defense-in-depth, never the sole authorization
    layer.
-   Private R2 buckets and short-lived signed download URLs.
-   Validate uploads using content signatures (magic bytes), ZIP entry
    count and expansion ratio caps, and safe XML parsing, not filename
    alone.
-   Avoid SSRF and external-reference fetching.
-   AI content is untrusted.
-   Internal service-to-service endpoints (n8n source bundle retrieval,
    workflow callbacks, and worker callbacks) require HMAC-SHA256
    signatures (`HMAC-SHA256(secret, "{timestamp}.{event_id}.{METHOD}.{canonical_path}.{raw_body}")`),
    binding uppercase HTTP method and exact request path (so bodyless `GET` requests never rely on an empty `raw_body` alone),
    constant-time verification, a 5-minute timestamp tolerance, event ID
    replay protection, and strict payload size limits.
-   API keys remain server-side.
-   Payment webhooks require signature verification, replay protection,
    and idempotency.
-   Structured logs; never log secrets or full document text by default.
-   Audit logs for sensitive actions.
-   Daily backup target with offsite copy; test restore.
-   Initial backup retention target: 7--14 days, subject to provider
    capability and cost.
-   Initial recovery targets: RPO 24 hours / RTO 8 hours as planning
    targets, not guarantees.

## 13. MVP Scope

### Included

-   Authentication.
-   Personal Workspace.
-   Organization Workspace and role-based access.
-   Document creation using form, text, and supported file input.
-   AI extraction and type-specific draft generation.
-   Human review and approval.
-   Built-in templates and basic organization templates.
-   DOCX generation and download.
-   Credit ledger and reservation lifecycle.
-   Async jobs and failure handling.
-   Usage/audit records.
-   Core security and tests.

### Deferred

-   OCR for scanned images.
-   Real-time collaboration.
-   Full Word editor.
-   Complex repeating table logic.
-   PDF preview.
-   Live payments until eligibility, provider, and business requirements
    are resolved.
-   Fine-tuning.
-   Vector database/RAG.
-   Native mobile application.
-   Advanced analytics.
-   Guaranteed fidelity for complex Word layouts.

## 14. Open Decisions (TBD)

-   Final product name and domain.
-   Initial configured value for complimentary beta credits (mechanism is configurable in backend; exact number is **TBD**).
-   Credit tariff per operation and monetary prices (do not invent values).
-   Subscription plans and prices.
-   Payment provider and merchant onboarding.
-   n8n production licensing approval and hosting.
-   Whether n8n is used in production or Python orchestration is used
    instead.
-   Exact Cloud Run service sizing and quotas.
-   Email provider for verification and invitations.
-   Retention/deletion policy and legal review.
-   Supported language/model configuration.
-   Final upload limits after testing.
-   Exact ORM/migration and package manager tooling for M1 scaffolding.

## 15. Definition of MVP Done

-   No cross-workspace data access.
-   No negative or inconsistent credit balance under concurrent
    requests.
-   No duplicate credit transaction on retries.
-   No final DOCX without approval.
-   No unsupported critical facts silently invented.
-   Generated DOCX opens in supported office software.
-   Failed jobs reach a recoverable terminal state.
-   Reservations are not stranded indefinitely.
-   Template version used by a document is immutable and traceable.
-   Backup restore has been tested.
-   Security and integration tests pass.
-   Production licensing and privacy questions are resolved before
    public launch.
