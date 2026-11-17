"""Application configuration and environment validation."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EnvironmentName = Literal["local", "test", "staging", "production"]

_PLACEHOLDER_PREFIXES = ("replace-with-", "changeme", "placeholder", "example")
_DEFAULT_PLACEHOLDER_DB_PASSWORD = "replace-with-local-dev-password"
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_BACKEND_ENV_FILE = _BACKEND_DIR / ".env"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ROOT_ENV_FILE = _REPO_ROOT / ".env"


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return any(lowered.startswith(prefix) or prefix in lowered for prefix in _PLACEHOLDER_PREFIXES)


class Settings(BaseSettings):
    """Runtime settings validated at startup."""

    model_config = SettingsConfigDict(
        env_file=(str(_ROOT_ENV_FILE), str(_BACKEND_ENV_FILE)),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "Document SaaS API"
    app_version: str = "0.1.0"
    environment: EnvironmentName = "local"
    api_v1_prefix: str = "/api/v1"

    postgres_user: str = "document_saas"
    postgres_password: str = _DEFAULT_PLACEHOLDER_DB_PASSWORD
    postgres_db: str = "document_saas_dev"
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=5432, ge=1, le=65535)

    database_url: str = (
        f"postgresql+asyncpg://document_saas:{_DEFAULT_PLACEHOLDER_DB_PASSWORD}"
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
    def sync_and_validate_security(self) -> "Settings":
        # If DATABASE_URL still contains the default template placeholder password while
        # POSTGRES_PASSWORD was configured in .env, derive the matching PostgreSQL URL.
        if _DEFAULT_PLACEHOLDER_DB_PASSWORD in self.database_url and not _looks_like_placeholder(
            self.postgres_password
        ):
            encoded_user = quote(self.postgres_user, safe="")
            encoded_password = quote(self.postgres_password, safe="")
            encoded_db = quote(self.postgres_db, safe="")
            self.database_url = (
                f"postgresql+asyncpg://{encoded_user}:{encoded_password}"
                f"@{self.postgres_host}:{self.postgres_port}/{encoded_db}"
            )

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
