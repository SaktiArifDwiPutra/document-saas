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

    # Authentication & session settings (M2)
    jwt_secret_key: str = "replace-with-local-dev-jwt-secret-minimum-32-chars"
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    jwt_issuer: str = "document-saas"
    jwt_audience: str = "document-saas-api"
    access_token_expire_minutes: int = Field(default=15, ge=1, le=120)
    refresh_token_expire_days: int = Field(default=14, ge=1, le=90)
    email_verification_expire_hours: int = Field(default=24, ge=1, le=168)
    password_reset_expire_minutes: int = Field(default=60, ge=5, le=240)
    workspace_invitation_expire_hours: int = Field(default=168, ge=1, le=720)
    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    require_origin_or_referer: bool = True

    # In-memory sliding-window rate limits (dev/beta single-instance only;
    # production multi-instance deployments require a shared backing store).
    rate_limit_max_keys: int = Field(default=10_000, ge=10, le=1_000_000)
    rate_limit_register_ip_max_keys: int | None = Field(default=None, ge=1, le=1_000_000)
    rate_limit_login_ip_max_keys: int | None = Field(default=None, ge=1, le=1_000_000)
    rate_limit_login_account_max_keys: int | None = Field(default=None, ge=1, le=1_000_000)
    rate_limit_forgot_ip_max_keys: int | None = Field(default=None, ge=1, le=1_000_000)
    rate_limit_forgot_account_max_keys: int | None = Field(default=None, ge=1, le=1_000_000)
    rate_limit_register_ip_max: int = Field(default=5, ge=1)
    rate_limit_register_ip_window_seconds: int = Field(default=3600, ge=10)
    rate_limit_login_ip_max: int = Field(default=5, ge=1)
    rate_limit_login_ip_window_seconds: int = Field(default=60, ge=10)
    rate_limit_login_account_max: int = Field(default=5, ge=1)
    rate_limit_login_account_window_seconds: int = Field(default=300, ge=10)
    rate_limit_forgot_ip_max: int = Field(default=10, ge=1)
    rate_limit_forgot_ip_window_seconds: int = Field(default=3600, ge=10)
    rate_limit_forgot_account_max: int = Field(default=3, ge=1)
    rate_limit_forgot_account_window_seconds: int = Field(default=3600, ge=10)

    # Configurable beta complimentary credits and AI generation cost; exact values remain TBD.
    beta_complimentary_credits: int | None = Field(default=None, ge=0)
    ai_generation_credit_cost: int | None = Field(default=None, ge=1)
    credit_reservation_ttl_seconds: int = Field(default=900, ge=60, le=86400)

    # M5 upload & archive safety limits (10 MB per file, max 5 files per document).
    max_source_files_per_document: int = Field(default=5, ge=1, le=50)
    max_upload_file_size_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_archive_expansion_ratio: int = Field(default=10, ge=1, le=100)
    max_zip_entries: int = Field(default=1000, ge=1, le=10000)
    object_storage_endpoint: str = "https://r2.local.invalid"
    object_storage_bucket: str = "document-saas-private"

    # M6 AI document engine & internal workflow orchestration settings.
    source_bundle_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    max_callback_payload_bytes: int = Field(default=512 * 1024, ge=4096, le=5 * 1024 * 1024)
    workflow_orchestrator_provider: Literal["none", "python", "n8n"] = "none"
    worker_provider_mode: Literal["local", "http"] = "local"
    worker_base_url: str = "http://localhost:8001"
    worker_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)
    n8n_base_url: str = "http://localhost:5678"
    n8n_timeout_seconds: float = Field(default=10.0, ge=1.0, le=120.0)
    gemini_provider_mode: Literal["deterministic", "http"] = "deterministic"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_seconds: float = Field(default=30.0, ge=1.0, le=120.0)

    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])

    @field_validator("api_v1_prefix")
    @classmethod
    def validate_api_prefix(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("/"):
            raise ValueError("api_v1_prefix must start with '/'")
        return normalized.rstrip("/") or "/api/v1"

    @field_validator("internal_hmac_secret", "jwt_secret_key")
    @classmethod
    def validate_secret_length(cls, value: str) -> str:
        if len(value.strip()) < 32:
            raise ValueError("Secret keys must be at least 32 characters")
        return value

    @field_validator("jwt_issuer", "jwt_audience")
    @classmethod
    def validate_jwt_claims(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("JWT issuer and audience must not be empty")
        return normalized

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

        if self.jwt_secret_key.strip() == self.internal_hmac_secret.strip():
            raise ValueError("jwt_secret_key must be distinct from internal_hmac_secret")

        if self.environment in ("staging", "production"):
            self.cookie_secure = True
            self.require_origin_or_referer = True
            if _looks_like_placeholder(self.internal_hmac_secret):
                raise ValueError(
                    "internal_hmac_secret cannot use a placeholder value in staging/production"
                )
            if _looks_like_placeholder(self.jwt_secret_key):
                raise ValueError(
                    "jwt_secret_key cannot use a placeholder value in staging/production"
                )
            if _looks_like_placeholder(self.database_url):
                raise ValueError(
                    "database_url cannot use a placeholder value in staging/production"
                )
            if self.database_url.startswith("sqlite"):
                raise ValueError("SQLite is not permitted in staging/production")
            if any("*" in origin for origin in self.cors_origins):
                raise ValueError("Wildcard CORS origins are not permitted in staging/production")
        return self

    def get_rate_limit_scope_max_keys(self) -> dict[str, int]:
        """Return configured per-scope maximum key capacities."""
        overrides = {
            "register:ip": self.rate_limit_register_ip_max_keys,
            "login:ip": self.rate_limit_login_ip_max_keys,
            "login:acct": self.rate_limit_login_account_max_keys,
            "forgot:ip": self.rate_limit_forgot_ip_max_keys,
            "forgot:acct": self.rate_limit_forgot_account_max_keys,
        }
        return {
            scope: capacity if capacity is not None else self.rate_limit_max_keys
            for scope, capacity in overrides.items()
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached validated application settings."""
    return Settings()
