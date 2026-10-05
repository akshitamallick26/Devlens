.PHONY: dev-web dev-api infra-up infra-down infra-logs test test-web test-api lint lint-web lint-api format

dev-web:
	cd apps/web && pnpm dev

dev-api:
	cd apps/api && uv run uvicorn app.main:app --reload

infra-up:
	cd infra && docker compose up -d

infra-down:
	cd infra && docker compose down

infra-logs:
	cd infra && docker compose logs -f

test: test-web test-api

test-web:
	cd apps/web && pnpm typecheck

test-api:
	cd apps/api && uv run pytest

lint: lint-web lint-api

lint-web:
	cd apps/web && pnpm lint && pnpm format:check

lint-api:
	cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy app

format:
	cd apps/web && pnpm format
	cd apps/api && uv run ruff format .
