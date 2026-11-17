"""Tests for backend configuration and security validation."""

import pytest
from pydantic import ValidationError

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
    """Production/staging must reject placeholder database URLs and HMAC secrets."""
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            database_url="postgresql+asyncpg://user:pass@localhost:5432/prod_db",
            internal_hmac_secret="replace-with-local-dev-hmac-secret-minimum-32-chars",
        )

    with pytest.raises(ValidationError):
        Settings(
            environment="staging",
            database_url="sqlite+aiosqlite:///./staging.db",
            internal_hmac_secret="a-real-non-placeholder-secret-at-least-32-chars-long",
        )


def test_accepts_valid_non_placeholder_production_settings() -> None:
    """Valid production settings pass validation."""
    settings = Settings(
        environment="production",
        database_url="postgresql+asyncpg://prod_user:s3cureValue!@db.internal:5432/doc_saas",
        internal_hmac_secret="prod-hmac-key-9d8f7e6c5b4a32109876543210fedcba",
    )
    assert settings.environment == "production"
