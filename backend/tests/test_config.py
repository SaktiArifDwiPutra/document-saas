"""Tests for backend configuration and security validation."""

from urllib.parse import urlparse

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from app.core.config import Settings


def test_beta_complimentary_credits_is_configurable_and_defaults_to_none() -> None:
    """Beta complimentary credits must remain configurable without a hardcoded amount."""
    default_settings = Settings(environment="local")
    assert default_settings.beta_complimentary_credits is None

    configured = Settings(environment="local", beta_complimentary_credits=25)
    assert configured.beta_complimentary_credits == 25


def test_rejects_short_hmac_secret() -> None:
    """Internal HMAC secret shorter than 32 chars must fail startup validation."""
    with pytest.raises(ValidationError):
        Settings(environment="local", internal_hmac_secret="too-short")


def test_rejects_placeholder_secrets_in_production() -> None:
    """Production/staging must reject placeholder DB URLs, HMAC secrets, and JWT secrets."""
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            database_url="postgresql+asyncpg://user:pass@localhost:5432/prod_db",
            internal_hmac_secret="replace-with-local-dev-hmac-secret-minimum-32-chars",
            jwt_secret_key="prod-jwt-key-9d8f7e6c5b4a32109876543210fedcba",
        )

    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            database_url="postgresql+asyncpg://user:pass@localhost:5432/prod_db",
            internal_hmac_secret="a-real-non-placeholder-secret-at-least-32-chars-long",
            jwt_secret_key="replace-with-local-dev-jwt-secret-minimum-32-chars",
        )

    with pytest.raises(ValidationError):
        Settings(
            environment="staging",
            database_url="sqlite+aiosqlite:///./staging.db",
            internal_hmac_secret="a-real-non-placeholder-secret-at-least-32-chars-long",
            jwt_secret_key="staging-jwt-key-9d8f7e6c5b4a32109876543210fedcba",
        )


def test_accepts_valid_non_placeholder_production_settings() -> None:
    """Valid production settings pass validation and enforce secure cookies and origin checks."""
    settings = Settings(
        environment="production",
        database_url="postgresql+asyncpg://prod_user:s3cureValue!@db.internal:5432/doc_saas",
        internal_hmac_secret="prod-hmac-key-9d8f7e6c5b4a32109876543210fedcba",
        jwt_secret_key="prod-jwt-key-9d8f7e6c5b4a32109876543210fedcba",
        cookie_secure=False,
        require_origin_or_referer=False,
    )
    assert settings.environment == "production"
    assert settings.cookie_secure is True
    assert settings.require_origin_or_referer is True


def test_derives_database_url_from_postgres_password_when_url_has_placeholder() -> None:
    """When POSTGRES_PASSWORD is set and DATABASE_URL has placeholder password, sync them."""
    settings = Settings(
        environment="local",
        postgres_user="doc_user",
        postgres_password="custom/pass+word=123",
        postgres_db="doc_db",
        postgres_port=5433,
        database_url=(
            "postgresql+asyncpg://document_saas:replace-with-local-dev-password"
            "@localhost:5432/document_saas_dev"
        ),
    )
    parsed = urlparse(settings.database_url)
    assert parsed.username == "doc_user"
    assert parsed.port == 5433
    assert parsed.path == "/doc_db"
    assert make_url(settings.database_url).password == "custom/pass+word=123"
    assert settings.database_url.startswith(
        "postgresql+asyncpg://doc_user:custom%2Fpass%2Bword%3D123@"
    )


def test_preserves_explicit_non_placeholder_database_url() -> None:
    """Explicit DATABASE_URL overrides POSTGRES_* derivation."""
    explicit_url = "postgresql+asyncpg://explicit_user:explicit_pass@db-host:5432/explicit_db"
    settings = Settings(
        environment="local",
        postgres_password="different-password",
        database_url=explicit_url,
    )
    assert settings.database_url == explicit_url


def test_rejects_short_jwt_secret() -> None:
    """JWT secret shorter than 32 chars must fail startup validation."""
    with pytest.raises(ValidationError):
        Settings(environment="local", jwt_secret_key="too-short")


def test_rejects_unsupported_jwt_algorithm() -> None:
    """Only explicit HS256/HS384/HS512 JWT algorithms are permitted."""
    for bad_alg in ("none", "RS256"):
        with pytest.raises(ValidationError):
            Settings(environment="local", jwt_algorithm=bad_alg)


def test_rejects_identical_jwt_and_hmac_secrets() -> None:
    """JWT signing secret and internal HMAC secret must be distinct."""
    shared_secret = "shared-secret-value-at-least-32-characters-long"
    with pytest.raises(ValidationError):
        Settings(
            environment="local",
            internal_hmac_secret=shared_secret,
            jwt_secret_key=shared_secret,
        )


def test_rejects_wildcard_cors_origins_in_staging_and_production() -> None:
    """Staging and production must reject wildcard CORS origins."""
    for env_name in ("staging", "production"):
        with pytest.raises(ValidationError):
            Settings(
                environment=env_name,
                database_url="postgresql+asyncpg://prod_user:s3cureValue!@db.internal:5432/doc_saas",
                internal_hmac_secret="prod-hmac-key-9d8f7e6c5b4a32109876543210fedcba",
                jwt_secret_key="prod-jwt-key-9d8f7e6c5b4a32109876543210fedcba",
                cors_origins=["*"],
            )


def test_rate_limit_per_scope_capacity_configuration() -> None:
    """Per-scope rate-limit capacities fall back to rate_limit_max_keys and allow overrides."""
    default_settings = Settings(environment="local", rate_limit_max_keys=500)
    assert default_settings.get_rate_limit_scope_max_keys() == {
        "register:ip": 500,
        "login:ip": 500,
        "login:acct": 500,
        "forgot:ip": 500,
        "forgot:acct": 500,
    }

    custom_settings = Settings(
        environment="local",
        rate_limit_max_keys=1000,
        rate_limit_login_ip_max_keys=250,
        rate_limit_login_account_max_keys=400,
        rate_limit_forgot_account_max_keys=150,
    )
    assert custom_settings.get_rate_limit_scope_max_keys() == {
        "register:ip": 1000,
        "login:ip": 250,
        "login:acct": 400,
        "forgot:ip": 1000,
        "forgot:acct": 150,
    }

    with pytest.raises(ValidationError):
        Settings(environment="local", rate_limit_login_account_max_keys=0)
