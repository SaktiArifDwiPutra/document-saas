# Document SaaS

AI-powered document generation for individuals and organizations.

The application turns forms, notes, transcripts, and supported uploaded
files into structured document drafts. Users review and approve the
content before generating a DOCX from a selected template.

## Project Status

**Current phase:** Milestone M1 — Project Foundation\
**Implementation status:** M1 foundation scaffolding (backend, worker, frontend, local Docker Compose)\
**Architecture:** FastAPI core + replaceable workflow orchestration (n8n
initially) + Python Worker

## Repository Structure

``` text
document-saas/
├── frontend/              # Next.js App Router static export (TypeScript + Tailwind + pnpm)
├── backend/               # FastAPI core API (Python 3.12 + SQLAlchemy 2 + Alembic)
├── worker/                # Python file-processing and DOCX worker (no DB access)
├── automation/
│   └── n8n/               # Exported n8n workflows and setup notes
├── docs/                  # Product and technical documentation
├── docker-compose.yml     # Local PostgreSQL and n8n development services
├── .env.example           # Root environment placeholders
└── README.md
```

## Start Here

Read documents in this order:

1.  `docs/MASTER_SPECIFICATION.md`
2.  `docs/SYSTEM_ARCHITECTURE.md`
3.  `docs/DATABASE_DESIGN.md`
4.  `docs/API_CONTRACT.md`
5.  `docs/N8N_WORKFLOW_DESIGN.md`
6.  `docs/TESTING_STRATEGY.md`
7.  `docs/IMPLEMENTATION_ROADMAP.md`
8.  `docs/AI_AGENT_GUIDELINES.md`

## Architecture Summary

-   **Frontend:** Next.js static frontend (`output: 'export'`; client-side active workspace selection and dynamic document resolution via `/api/v1`).
-   **Backend:** FastAPI is the sole owner of the PostgreSQL database and owns authentication, authorization, workspaces, documents (`documents.status`), configurable beta credits, templates, asynchronous jobs (`jobs.status`), and approvals.
-   **Workflow orchestration:** n8n coordinates AI workflows through a replaceable adapter, retrieving normalized source bundles via a short-lived signed internal endpoint (`GET /api/v1/internal/source-bundles/{source_bundle_id}`) and reporting via `POST /api/v1/internal/workflows/callback`.
-   **Fallback:** Python workflow adapter keeps the product usable without n8n.
-   **Worker:** Python handles file normalization and DOCX rendering, reporting outcomes to FastAPI via `POST /api/v1/internal/workers/callback` without direct database access.
-   **Database:** PostgreSQL.
-   **Storage:** private Cloudflare R2 bucket candidate.
-   **AI:** Gemini through a provider abstraction (first vertical slice: `meeting_minutes` --- Notulen Rapat).

Neither n8n nor Python Worker connects directly to PostgreSQL. n8n is not the system of record and does not own application
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

**Approved M0 Decisions:**
-   **Organization Document Permissions:** Owner and Admin have full access to all organization documents; Editor has full access to own documents and read-only access to other members' documents; Member has full access only to own documents.
-   **Beta Credits:** Complimentary beta credits are configurable in backend settings (initial configured amount and tariffs remain `TBD`); reserved AI credits are committed only when a valid AI draft is persisted as `review_required`, and DOCX rendering failures or re-renders of an approved version do not incur a second AI charge.
-   **First Vertical Slice:** `meeting_minutes` (Notulen Rapat) is the first end-to-end workflow to implement and validate before expanding to other document types.
-   **Internal Communication:** Signed internal source retrieval (`GET /api/v1/internal/source-bundles/{source_bundle_id}`) and HMAC-SHA256 callbacks (`POST /api/v1/internal/workflows/callback`, `POST /api/v1/internal/workers/callback`) with 5-minute timestamp tolerance and `event_id` replay protection.

**Remaining `TBD` Decisions:**
See the `TBD` sections in the master specification and technical
documents. Important open gates include: - n8n production licensing and
hosting. - Initial configured complimentary credit value, final credit pricing, and tariff. - Payment provider and
merchant eligibility. - Email provider. - Data retention policy. - Exact
production resource sizing.

## Implementation

Follow `docs/IMPLEMENTATION_ROADMAP.md`.

The coding agent must follow `docs/AI_AGENT_GUIDELINES.md` and complete each approved milestone before starting the next.

## Local Development Commands

### 1. Environment Setup

Copy the root and service `.env.example` files into ignored `.env` files before running services:

```bash
cp .env.example .env
cp backend/.env.example backend/.env
cp worker/.env.example worker/.env
cp frontend/.env.example frontend/.env.local
```

### 2. Local Infrastructure (Docker Compose)

> Note: Local n8n Community Edition is for development only; commercial SaaS production licensing remains an unresolved gate.

```bash
# Start PostgreSQL and n8n in the background
docker compose up -d

# Check service status and logs
docker compose ps
docker compose logs -f postgres n8n

# Stop containers (preserving data volumes)
docker compose down

# Stop and remove persistent local volumes (WARNING: deletes local DB and n8n state)
docker compose down -v
```

### 3. Backend (`backend/`)

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Lint, format check, typecheck, and unit tests
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy app tests
.venv/bin/pytest

# Run migrations and start local API server
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload --port 8000
```

### 4. Python Worker (`worker/`)

```bash
cd worker
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Lint, format check, typecheck, and unit tests
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy worker_app tests
.venv/bin/pytest

# Start local worker HTTP health/service process
.venv/bin/uvicorn worker_app.main:app --reload --port 8001
```

### 5. Frontend (`frontend/`)

```bash
cd frontend
pnpm install

# Typecheck, lint, unit tests, and static export build (outputs to frontend/out/)
pnpm typecheck
pnpm lint
pnpm test
pnpm build
```
