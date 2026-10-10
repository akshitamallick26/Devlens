from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Single configuration boundary for the API. All environment access
    should go through this class rather than reading os.environ directly."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "devlens-api"
    environment: str = "development"

    database_url: str = "postgresql+psycopg://devlens:devlens@localhost:5432/devlens"
    redis_url: str = "redis://localhost:6379/0"

    cors_origins: list[str] = ["http://localhost:3000"]

    # Repository ingestion limits — see docs/architecture/repository-ingestion.md
    # section 14 for why each default is set where it is. Conservative v1
    # numbers for a foundation phase, not a claim about final capacity.
    ingestion_clone_timeout_seconds: float = 120.0
    ingestion_git_command_timeout_seconds: float = 30.0
    ingestion_max_files: int = 20_000
    ingestion_max_file_bytes: int = 1_000_000
    ingestion_max_total_bytes: int = 200_000_000

    # Impact Intelligence (optional AI explanation layer) — see
    # docs/architecture/impact-intelligence.md sections 5/11. Absence of
    # ai_api_key is a normal, fully-supported state: the deterministic
    # Impact Analysis endpoint works identically either way, and nothing
    # reads these at import/startup time, only on an explanation request.
    ai_provider: str = "anthropic"
    ai_model: str = "claude-sonnet-4-5"
    ai_api_key: str | None = None

    # RAG embeddings (separate vendor from ai_* above — Anthropic has no
    # embeddings API). OpenAI chosen specifically because text-embedding-3-
    # small's default output width (1536) already matches this schema's
    # pre-committed EMBEDDING_DIMENSION (app/models/rag.py) — see
    # docs/architecture/rag-codebase-qa.md section 4. Same optional-
    # provider convention as ai_api_key: absence is normal, never a
    # startup error, and deterministic chunking/retrieval work without it.
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_api_key: str | None = None

    # Testing / Generated Tests v1 — see app/testing/sandbox.py's module
    # docstring for exactly what this execution boundary does and does not
    # provide. Conservative v1 defaults, same "foundation phase, not a
    # capacity claim" framing as the ingestion limits above.
    testing_execution_timeout_seconds: float = 30.0
    testing_max_output_bytes: int = 64_000
    testing_max_tests_per_request: int = 5

    # Security Intelligence v1 — see app/security/{osv,semgrep}.py module
    # docstrings for exactly what each scanner boundary does and does not
    # provide. Neither osv-scanner nor semgrep is installed in this
    # development sandbox (confirmed via `shutil.which` before this code
    # was written) — both return a typed "unavailable" status, never a
    # fabricated clean result, until a deployment installs the binaries.
    security_osv_timeout_seconds: float = 60.0
    security_semgrep_timeout_seconds: float = 120.0
    security_max_blast_radius_depth: int = 5
    security_max_blast_radius_symbols: int = 50
    security_max_blast_radius_files: int = 50


@lru_cache
def get_settings() -> Settings:
    return Settings()
