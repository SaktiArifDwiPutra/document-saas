"""Shared pytest fixtures for Python Worker tests."""

import pytest
from httpx import ASGITransport, AsyncClient

from worker_app.core.config import WorkerSettings
from worker_app.main import create_worker_app


@pytest.fixture
def worker_test_settings() -> WorkerSettings:
    """Provide isolated test settings for Python Worker."""
    return WorkerSettings(
        environment="test",
        backend_internal_base_url="http://localhost:8000",
        worker_callback_path="/api/v1/internal/workers/callback",
        internal_hmac_secret="worker-test-secret-minimum-32-characters-long",
    )


@pytest.fixture
async def worker_client(worker_test_settings: WorkerSettings) -> AsyncClient:  # type: ignore[misc]
    """Yield an async HTTPX test client against the Worker app."""
    app = create_worker_app(worker_test_settings)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
