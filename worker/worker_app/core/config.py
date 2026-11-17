"""Worker configuration with strict isolation from core PostgreSQL credentials."""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EnvironmentName = Literal["local", "test", "staging", "production"]

_PLACEHOLDER_PREFIXES = ("replace-with-", "changeme", "placeholder", "example")
_WORKER_DIR = Path(__file__).resolve().parents[2]
_WORKER_ENV_FILE = _WORKER_DIR / ".env"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ROOT_ENV_FILE = _REPO_ROOT / ".env"

# Only allowlisted worker-relevant keys may be inherited from the root .env file.
# PostgreSQL, DATABASE_URL, and N8N_* keys are intentionally excluded.
_ALLOWLISTED_ROOT_ENV_KEYS: tuple[str, ...] = (
    "environment",
    "backend_internal_base_url",
    "worker_callback_path",
    "internal_hmac_secret",
    "max_file_size_bytes",
    "max_archive_expansion_ratio",
    "max_zip_entries",
)


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    return any(lowered.startswith(prefix) or prefix in lowered for prefix in _PLACEHOLDER_PREFIXES)


def _read_allowlisted_root_env(root_env_path: Path) -> dict[str, str]:
    if not root_env_path.is_file():
        return {}
    raw_values = dotenv_values(root_env_path)
    normalized: dict[str, str] = {}
    for key, value in raw_values.items():
        if value is None:
            continue
        lower_key = key.strip().lower()
        if lower_key in _ALLOWLISTED_ROOT_ENV_KEYS:
            normalized[lower_key] = value
    return normalized


class WorkerSettings(BaseSettings):
    """Validated runtime settings for Python Worker.

    Extra fields (such as database_url) are explicitly forbidden so the Worker
    never holds direct core database configuration.
    """

    model_config = SettingsConfigDict(
        env_file=str(_WORKER_ENV_FILE),
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

    @model_validator(mode="before")
    @classmethod
    def apply_allowlisted_root_env_fallback(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        root_values = _read_allowlisted_root_env(_ROOT_ENV_FILE)
        if not root_values:
            return data

        merged = dict(data)
        existing_lower = {str(k).lower() for k in merged}
        env_lower = {k.lower() for k in os.environ}

        for key, value in root_values.items():
            if key not in existing_lower and key not in env_lower:
                merged[key] = value
        return merged

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
