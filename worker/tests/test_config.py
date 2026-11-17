"""Tests for Python Worker configuration and DB isolation."""

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from worker_app.core import config as worker_config
from worker_app.core.config import WorkerSettings


def test_worker_has_no_database_dependencies_in_pyproject() -> None:
    """Worker package must not declare SQLAlchemy, Alembic, or PostgreSQL drivers."""
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    deps = " ".join(data["project"]["dependencies"]).lower()

    for forbidden in ("sqlalchemy", "alembic", "asyncpg", "psycopg"):
        assert forbidden not in deps


def test_worker_rejects_database_url_configuration() -> None:
    """WorkerSettings must reject direct core database configuration."""
    assert "database_url" not in WorkerSettings.model_fields
    with pytest.raises(ValidationError):
        WorkerSettings(database_url="postgresql+asyncpg://user:pass@localhost/db")  # type: ignore[call-arg]


def test_worker_upload_safety_defaults() -> None:
    """Worker applies bounded file size, expansion ratio, and zip entry limits."""
    settings = WorkerSettings()
    assert settings.max_file_size_bytes == 10 * 1024 * 1024
    assert settings.max_archive_expansion_ratio == 10
    assert settings.max_zip_entries == 1000


def test_worker_rejects_placeholder_secret_in_production() -> None:
    """Production worker rejects placeholder HMAC secrets."""
    with pytest.raises(ValidationError):
        WorkerSettings(
            environment="production",
            internal_hmac_secret="replace-with-local-dev-hmac-secret-minimum-32-chars",
        )


def test_worker_loads_only_allowlisted_keys_from_root_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker inherits allowlisted root .env keys while ignoring DB/n8n secrets."""
    root_env = tmp_path / ".env"
    root_env.write_text(
        "\n".join(
            [
                "POSTGRES_PASSWORD=super-secret-db-password",
                "DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/db",
                "N8N_ENCRYPTION_KEY=super-secret-n8n-key",
                "INTERNAL_HMAC_SECRET=shared-test-hmac-secret-at-least-32-chars",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(worker_config, "_ROOT_ENV_FILE", root_env)

    settings = WorkerSettings()
    assert settings.internal_hmac_secret == "shared-test-hmac-secret-at-least-32-chars"
    assert not hasattr(settings, "database_url")
    assert not hasattr(settings, "postgres_password")
    assert not hasattr(settings, "n8n_encryption_key")
