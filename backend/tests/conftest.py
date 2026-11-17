"""Shared pytest fixtures for backend foundation tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def test_settings() -> Settings:
    """Provide isolated test settings without external network or live DB dependency."""
    return Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        internal_hmac_secret="test-secret-for-unit-tests-only-minimum-32-chars",
        internal_hmac_timestamp_tolerance_seconds=300,
        beta_complimentary_credits=None,
    )


@pytest.fixture
async def client(test_settings: Settings) -> AsyncClient:  # type: ignore[misc]
    """Yield an async HTTPX test client against the FastAPI app."""
    app = create_app(test_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client
