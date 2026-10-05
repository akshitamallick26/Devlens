from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Single configuration boundary for the API. All environment access
    should go through this class rather than reading os.environ directly."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "devlens-api"
    environment: str = "development"

    database_url: str = "postgresql://devlens:devlens@localhost:5432/devlens"
    redis_url: str = "redis://localhost:6379/0"

    cors_origins: list[str] = ["http://localhost:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
