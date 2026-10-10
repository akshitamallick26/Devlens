# DevLens — api

FastAPI backend, managed with uv. See the [repository root README](../../README.md)
for the product, architecture and setup.

```bash
uv sync
uv run alembic upgrade head   # apply database migrations (requires Postgres+pgvector; see infra/)
uv run uvicorn app.main:app --reload
```

Requirements: PostgreSQL with the `pgvector` extension (`infra/`), and `git`
on the PATH for ingestion. Optional: `AI_API_KEY`, `EMBEDDING_API_KEY`, and the
`osv-scanner` / `semgrep` binaries (see `.env.example` and the root README).

```bash
uv run pytest                 # database tests skip, with a reason, if Postgres is unreachable
uv run ruff check . && uv run ruff format --check . && uv run mypy app
```

Code is organized by subsystem under `app/` (ingestion, parsing, graph,
impact, rag, testing, security, automation, documentation, investigation,
overview, pipeline); `app/api/routes/` is a thin HTTP layer over those
services. Database schema: SQLAlchemy models in `app/models/`, migrations in
`alembic/`. See [docs/architecture/](../../docs/architecture/) for the design
record of each subsystem, starting with
[database-design.md](../../docs/architecture/database-design.md).
