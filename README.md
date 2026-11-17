# Document SaaS

AI-powered document generation for individuals and organizations.

The application turns forms, notes, transcripts, and supported uploaded
files into structured document drafts. Users review and approve the
content before generating a DOCX from a selected template.

## Project Status

**Current phase:** Documentation and repository foundation\
**Implementation status:** Not started unless explicitly recorded in the
repository\
**Architecture:** FastAPI core + replaceable workflow orchestration (n8n
initially) + Python Worker

## Repository Structure

``` text
document-saas/
├── frontend/              # Next.js frontend
├── backend/               # FastAPI core API
├── worker/                # Python file-processing and DOCX worker
├── automation/
│   └── n8n/               # Exported n8n workflows and setup notes
├── docs/                  # Product and technical documentation
├── docker-compose.yml     # Local development services (to be added in M1)
└── README.md
```

## Start Here

Read documents in this order:

1.  `docs/MASTER_SPECIFICATION.md`
2.  `docs/SYSTEM_ARCHITECTURE.md`
3.  `docs/DATABASE_DESIGN.md`
4.  `docs/API_CONTRACT.md`
5.  `docs/N8N_WORKFLOW_DESIGN.md`
6.  `docs/AI_AGENT_GUIDELINES.md`
7.  `docs/TESTING_STRATEGY.md`
8.  `docs/IMPLEMENTATION_ROADMAP.md`

## Architecture Summary

-   **Frontend:** Next.js static frontend.
-   **Backend:** FastAPI owns authentication, authorization, workspaces,
    documents, credits, templates, jobs, and approvals.
-   **Workflow orchestration:** n8n coordinates AI workflows through a
    replaceable adapter.
-   **Fallback:** Python workflow adapter keeps the product usable
    without n8n.
-   **Worker:** Python handles file normalization and DOCX rendering.
-   **Database:** PostgreSQL.
-   **Storage:** private Cloudflare R2 bucket candidate.
-   **AI:** Gemini through a provider abstraction.

n8n is not the system of record and does not own application
permissions, credits, payments, or approvals.

## Development Principles

-   Documentation-first.
-   Local-first.
-   One milestone at a time.
-   No secrets in Git.
-   No real confidential data in free AI tiers unless permitted by
    applicable privacy terms.
-   No paid deployment without explicit approval.
-   Human review is mandatory before final DOCX generation.

## Environment Files

Use `.env.example` files with placeholders only. Keep real `.env` files
out of Git.

Never commit: - API keys. - Database passwords. - HMAC secrets. -
Refresh tokens. - Presigned URLs. - Real user documents.

## Current Decisions and Open Questions

See the `TBD` sections in the master specification and technical
documents. Important open gates include: - n8n production licensing and
hosting. - Final credit pricing and tariff. - Payment provider and
merchant eligibility. - Email provider. - Data retention policy. - Exact
production resource sizing.

## Implementation

Follow `docs/IMPLEMENTATION_ROADMAP.md`.

The coding agent must follow `docs/AI_AGENT_GUIDELINES.md`, complete M0
repository audit first, and wait for Product Owner approval before
implementation.
