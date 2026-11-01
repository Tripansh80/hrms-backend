"""Application settings. Every secret comes from the environment; the app refuses to start without them."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Resolve the backend's .env from this file, not the shell's current directory.
    # This lets `uvicorn app.main:app` work from either D:\hrms or D:\hrms\backend.
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    feature_analytics: bool = False
    feature_department_profiles: bool = False
    feature_teams: bool = False
    feature_employee_invitations: bool = False
    feature_employee_profiles: bool = False
    feature_employee_transfers: bool = False
    feature_employee_import: bool = False

    # hrms_api login (member of hrms_app): RLS enforced. Used for every tenant request.
    database_url: str
    # hrms_admin login (hrms_platform, BYPASSRLS): signup, platform admin, cross-tenant workers.
    platform_database_url: str
    # Table owner: Alembic migrations only. Falls back to platform_database_url.
    migration_database_url: str | None = None
    db_pool_size: int = 10

    redis_url: str
    redis_enabled: bool = False

    jwt_secret: SecretStr = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_days: int = 14

    # acme.<base_domain> -> tenant "acme"; the bare base_domain is the public/root site.
    base_domain: str
    cors_origins: list[str] = []

    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr

    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-5-5"
    llm_api_key: SecretStr | None = None
    llm_timeout_seconds: float = 30.0

    @field_validator("base_domain")
    @classmethod
    def _normalize_domain(cls, v: str) -> str:
        return v.strip().lower().rstrip(".")

    @property
    def migrations_url(self) -> str:
        return self.migration_database_url or self.platform_database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment
