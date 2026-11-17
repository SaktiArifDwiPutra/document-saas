"""Application configuration and environment validation."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EnvironmentName = Literal["local", "test", "staging", "production"]

_PLACEHOLDER_PREFIXES = ("replace-with-", "changeme", "placeholder", "example")


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return any(lowered.startswith(prefix) or prefix in lowered for prefix in _PLACEHOLDER_PREFIXES)


class Settings(BaseSettings):
    """Runtime settings validated at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Document SaaS API"
    app_version: str = "0.1.0"
    environment: EnvironmentName = "local"
    api_v1_prefix: str = "/api/v1"

    database_url: str = (
        "postgresql+asyncpg://document_saas:replace-with-local-dev-password"
        "@localhost:5432/document_saas_dev"
    )

    internal_hmac_secret: str = "replace-with-local-dev-hmac-secret-minimum-32-chars"
    internal_hmac_timestamp_tolerance_seconds: int = Field(default=300, ge=30, le=300)

    # Configurable beta complimentary credits; exact initial amount remains TBD by Product Owner.
    beta_complimentary_credits: int | None = Field(default=None, ge=0)

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    @field_validator("api_v1_prefix")
    @classmethod
    def validate_api_prefix(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("/"):
            raise ValueError("api_v1_prefix must start with '/'")
        return normalized.rstrip("/") or "/api/v1"

    @field_validator("internal_hmac_secret")
    @classmethod
    def validate_hmac_secret_length(cls, value: str) -> str:
        if len(value.strip()) < 32:
            raise ValueError("internal_hmac_secret must be at least 32 characters")
        return value

    @model_validator(mode="after")
    def validate_non_local_security(self) -> "Settings":
        if self.environment in ("staging", "production"):
            if _looks_like_placeholder(self.internal_hmac_secret):
                raise ValueError(
                    "internal_hmac_secret cannot use a placeholder value in staging/production"
                )
            if _looks_like_placeholder(self.database_url):
                raise ValueError(
                    "database_url cannot use a placeholder value in staging/production"
                )
            if self.database_url.startswith("sqlite"):
                raise ValueError("SQLite is not permitted in staging/production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached validated application settings."""
    return Settings()
