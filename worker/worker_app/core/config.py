"""Worker configuration with strict isolation from core PostgreSQL credentials."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EnvironmentName = Literal["local", "test", "staging", "production"]

_PLACEHOLDER_PREFIXES = ("replace-with-", "changeme", "placeholder", "example")


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return any(lowered.startswith(prefix) or prefix in lowered for prefix in _PLACEHOLDER_PREFIXES)


class WorkerSettings(BaseSettings):
    """Validated runtime settings for Python Worker.

    Extra fields (such as database_url) are explicitly forbidden so the Worker
    never holds direct core database configuration.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        case_sensitive=False,
    )

    app_name: str = "Document SaaS Worker"
    app_version: str = "0.1.0"
    environment: EnvironmentName = "local"

    backend_internal_base_url: str = "http://localhost:8000"
    worker_callback_path: str = "/api/v1/internal/workers/callback"
    internal_hmac_secret: str = "replace-with-local-dev-hmac-secret-minimum-32-chars"

    # Upload and archive safety bounds
    max_file_size_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_archive_expansion_ratio: int = Field(default=10, ge=1, le=100)
    max_zip_entries: int = Field(default=1000, ge=1, le=10000)

    @field_validator("worker_callback_path")
    @classmethod
    def validate_callback_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("/"):
            raise ValueError("worker_callback_path must start with '/'")
        return normalized

    @field_validator("internal_hmac_secret")
    @classmethod
    def validate_hmac_secret_length(cls, value: str) -> str:
        if len(value.strip()) < 32:
            raise ValueError("internal_hmac_secret must be at least 32 characters")
        return value

    @model_validator(mode="after")
    def validate_non_local_security(self) -> "WorkerSettings":
        if self.environment in ("staging", "production") and _looks_like_placeholder(
            self.internal_hmac_secret
        ):
            raise ValueError(
                "internal_hmac_secret cannot use a placeholder value in staging/production"
            )
        return self


@lru_cache(maxsize=1)
def get_worker_settings() -> WorkerSettings:
    """Return cached validated Worker settings."""
    return WorkerSettings()
