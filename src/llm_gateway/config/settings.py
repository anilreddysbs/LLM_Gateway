"""Application settings and environment configuration using Pydantic Settings."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central gateway configuration loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Core service settings
    ENVIRONMENT: Literal["development", "production", "test"] = "development"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False

    # Security
    ADMIN_API_KEY: str = Field(
        default="admin-super-secret-key-change-me",
        description="Master admin API key required to access /admin/* endpoints",
    )

    # Database
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/llm_gateway",
        description="Async database connection string (PostgreSQL or SQLite for tests)",
    )

    # Redis
    REDIS_URL: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for caching, rate limiting, and circuit breaker state",
    )
    FAIL_OPEN_ON_REDIS_ERROR: bool = Field(
        default=True,
        description="Allow requests if Redis is unreachable; if False, fail-closed with 503",
    )

    # Cache Settings
    CACHE_ENABLED: bool = True
    CACHE_TTL_SECONDS: int = 3600
    CACHE_MAX_TEMPERATURE: float = 0.3
    CACHE_ISOLATION_DEFAULT: bool = False

    # Resilience Settings
    REQUEST_TIMEOUT_SECONDS: float = 30.0
    PROVIDER_ATTEMPT_TIMEOUT_SECONDS: float = 15.0
    MAX_RETRIES: int = 2
    CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 5
    CIRCUIT_BREAKER_COOLDOWN_SECONDS: int = 30

    # Request validation limits
    MAX_REQUEST_BODY_SIZE_BYTES: int = 2 * 1024 * 1024  # 2 MB
    MAX_REQUEST_MESSAGES: int = 100
    MAX_REQUEST_TOKENS_CEILING: int = 32768

    # Observability & Privacy
    LOG_PAYLOADS: bool = Field(
        default=False,
        description="If False, only SHA-256 hashes of prompts/responses are logged for privacy",
    )
    USAGE_LOG_QUEUE_MAX_SIZE: int = 50000
    USAGE_LOG_BATCH_SIZE: int = 100
    USAGE_LOG_FLUSH_INTERVAL_SECONDS: float = 1.0

    # Provider Credentials and Endpoints
    GEMINI_API_KEY: str | None = None
    OPENAI_API_KEY: str | None = None
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"

    # Models YAML path
    MODELS_CONFIG_PATH: str = "config/models.yaml"


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings singleton."""
    return Settings()
