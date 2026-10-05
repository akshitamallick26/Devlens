# DevLens

DevLens is an AI-powered codebase intelligence platform. You point it at a GitHub
repository; it builds a structural/dependency representation of the codebase and lets
you understand, query, visualize, and safely modify the code — "understand your code
before you change it."

**Current status: Phase 0 — Foundation only.** No product features exist yet. This repo
currently contains a minimal, working frontend/backend/infrastructure skeleton and
nothing else — no repository ingestion, no parsing, no graph, no AI, no auth.

## Architecture (current)

- **`apps/web`** — Next.js (App Router, TypeScript) frontend. A single page confirming
  the frontend is running and showing the configured API base URL.
- **`apps/api`** — FastAPI backend, managed with [uv](https://docs.astral.sh/uv/). A
  single `GET /health` endpoint. All configuration goes through one
  `app/core/config.py` settings boundary.
- **`infra`** — Docker Compose for local PostgreSQL (with the `pgvector` extension)
  and Redis. No application tables exist yet — schema design starts in the repository
  ingestion phase.

The frontend and backend are independent processes that talk over HTTP; there is no
shared build tool (no Turborepo/Nx) and no monorepo workspace linking them, since a
single frontend package doesn't need one yet.

## Repository structure

```
devlens/
  apps/
    web/     # Next.js frontend
    api/     # FastAPI backend
  infra/     # Docker Compose (Postgres+pgvector, Redis)
  README.md
```

## Prerequisites

- Node.js 22 (LTS) — see `.nvmrc`. If you use nvm: `nvm use`.
- pnpm (`corepack enable` or `npm install -g pnpm`)
- Python 3.12 and [uv](https://docs.astral.sh/uv/) — uv will download Python 3.12
  automatically if it isn't already installed.
- Docker (for local Postgres/Redis)

## Local setup

```bash
git clone <repo> devlens && cd devlens

cp apps/web/.env.example apps/web/.env.local
cp apps/api/.env.example apps/api/.env
cp infra/.env.example infra/.env
```

### Run the infrastructure

```bash
make infra-up     # starts Postgres (pgvector) + Redis via Docker Compose
make infra-down   # stops them
```

### Run the backend

```bash
cd apps/api
uv sync
make dev-api      # or: uv run uvicorn app.main:app --reload
```

Verify: `curl http://localhost:8000/health`

### Run the frontend

```bash
cd apps/web
pnpm install
make dev-web      # or: pnpm dev
```

Open http://localhost:3000.

### Run tests / lint

```bash
make test   # backend pytest + frontend typecheck
make lint   # ruff + mypy (backend), eslint + prettier (frontend)
```

## Implementation status

Done (Phase 0):
- Next.js + TypeScript (strict) frontend skeleton, ESLint + Prettier
- FastAPI backend skeleton with a single `/health` endpoint, pydantic-settings
  configuration boundary, pytest, ruff, mypy
- Docker Compose for Postgres (pgvector) + Redis — no application schema yet
- Single `.env.example` per app/infra boundary, `.nvmrc`, root `Makefile`

Not started: repository ingestion, parsing/AST analysis, dependency graph,
visualization, impact analysis, RAG/Q&A, test generation, security analysis,
automation, sessions, the landing experience, and auth. These are later phases.
