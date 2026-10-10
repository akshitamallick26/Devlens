from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DeploymentMode = Literal["development", "public_demo"]


class Settings(BaseSettings):
    """Single configuration boundary for the API. All environment access
    should go through this class rather than reading os.environ directly."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "devlens-api"
    environment: str = "development"

    # Deployment safety — see docs/architecture/deployment-safety.md. The
    # default is the restrictive mode, so a deployment that forgets to set
    # DEPLOYMENT_MODE gets the safe behavior, never the permissive one; an
    # unrecognized value is a startup error (Literal), not a fallback.
    # Local development opts out explicitly (apps/api/.env.example).
    #   public_demo: AI features off unless PUBLIC_DEMO_AI_ENABLED=true,
    #                generated-test execution impossible, rate limiting on.
    #   development: AI features on whenever a key is configured, generated-
    #                test execution only with TEST_EXECUTION_ENABLED=true,
    #                rate limiting only with RATE_LIMIT_ENABLED=true.
    deployment_mode: DeploymentMode = "public_demo"
    public_demo_ai_enabled: bool = False
    test_execution_enabled: bool = False
    # None = the mode's default (on in public_demo, off in development).
    # Explicitly false in public_demo is rejected at startup.
    rate_limit_enabled: bool | None = None
    # One sliding window shared by every bucket; each bucket has a per-client
    # and a global (all clients) ceiling. Defaults are sized for one small
    # instance serving a portfolio demo — see deployment-safety.md section 3.
    rate_limit_window_seconds: float = Field(default=3600.0, gt=0)
    rate_limit_ingest_per_client: int = Field(default=5, gt=0)
    rate_limit_ingest_global: int = Field(default=30, gt=0)
    rate_limit_pipeline_per_client: int = Field(default=10, gt=0)
    rate_limit_pipeline_global: int = Field(default=60, gt=0)
    rate_limit_analysis_per_client: int = Field(default=60, gt=0)
    rate_limit_analysis_global: int = Field(default=600, gt=0)
    # Investigation-session writes: cheap individually, but each one persists
    # visitor-supplied content, so volume is what needs bounding. Their own
    # bucket so saving notes never consumes analysis capacity, or vice versa.
    rate_limit_session_per_client: int = Field(default=120, gt=0)
    rate_limit_session_global: int = Field(default=1200, gt=0)

    database_url: str = "postgresql+psycopg://devlens:devlens@localhost:5432/devlens"
    redis_url: str = "redis://localhost:6379/0"

    cors_origins: list[str] = ["http://localhost:3000"]

    # Repository ingestion limits — see docs/architecture/repository-ingestion.md
    # section 14 for why each default is set where it is. Conservative v1
    # numbers for a foundation phase, not a claim about final capacity.
    ingestion_clone_timeout_seconds: float = Field(default=120.0, gt=0)
    ingestion_git_command_timeout_seconds: float = Field(default=30.0, gt=0)
    ingestion_max_files: int = Field(default=20_000, gt=0)
    ingestion_max_file_bytes: int = Field(default=1_000_000, gt=0)
    ingestion_max_total_bytes: int = Field(default=200_000_000, gt=0)

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
    testing_execution_timeout_seconds: float = Field(default=30.0, gt=0)
    testing_max_output_bytes: int = Field(default=64_000, gt=0)
    testing_max_tests_per_request: int = Field(default=5, gt=0)

    # Security Intelligence v1 — see app/security/{osv,semgrep}.py module
    # docstrings for exactly what each scanner boundary does and does not
    # provide. Neither osv-scanner nor semgrep is installed in this
    # development sandbox (confirmed via `shutil.which` before this code
    # was written) — both return a typed "unavailable" status, never a
    # fabricated clean result, until a deployment installs the binaries.
    security_osv_timeout_seconds: float = Field(default=60.0, gt=0)
    security_semgrep_timeout_seconds: float = Field(default=120.0, gt=0)
    security_max_blast_radius_depth: int = Field(default=5, gt=0)
    security_max_blast_radius_symbols: int = Field(default=50, gt=0)
    security_max_blast_radius_files: int = Field(default=50, gt=0)

    @model_validator(mode="after")
    def _public_demo_is_never_weakened(self) -> Self:
        """A setting that would weaken public_demo is a startup error, not
        silently ignored: the operator learns their configuration is not
        what they think it is before the API serves a request."""
        if self.deployment_mode == "public_demo":
            if self.test_execution_enabled:
                raise ValueError(
                    "TEST_EXECUTION_ENABLED=true is not allowed when "
                    "DEPLOYMENT_MODE=public_demo: generated tests run without "
                    "OS-level isolation"
                )
            if self.rate_limit_enabled is False:
                raise ValueError(
                    "RATE_LIMIT_ENABLED=false is not allowed when DEPLOYMENT_MODE=public_demo"
                )
        return self

    @property
    def ai_features_allowed(self) -> bool:
        """Whether deployment policy permits AI provider calls at all (a key
        must still be configured). Checked by both provider factories."""
        if self.deployment_mode == "development":
            return True
        return self.public_demo_ai_enabled

    @property
    def test_execution_allowed(self) -> bool:
        """Never true in public_demo; opt-in in development."""
        return self.deployment_mode == "development" and self.test_execution_enabled

    @property
    def rate_limiting_active(self) -> bool:
        if self.rate_limit_enabled is None:
            return self.deployment_mode == "public_demo"
        return self.rate_limit_enabled


@lru_cache
def get_settings() -> Settings:
    return Settings()
