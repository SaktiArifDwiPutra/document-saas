"""Shared pytest fixtures for backend foundation and auth tests."""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_email_service, get_rate_limiter
from app.core.config import Settings
from app.core.rate_limit import SlidingWindowRateLimiter
from app.db.base import Base
from app.db.session import create_engine, create_session_factory, get_db_session
from app.main import create_app
from app.services.email import StubEmailService


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    """Provide isolated test settings with ephemeral SQLite DB."""
    db_file = tmp_path / "test_app.sqlite"
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{db_file}",
        internal_hmac_secret="test-secret-for-unit-tests-only-minimum-32-chars",
        jwt_secret_key="test-jwt-secret-for-unit-tests-only-minimum-32-chars",
        internal_hmac_timestamp_tolerance_seconds=300,
        beta_complimentary_credits=None,
        cookie_secure=False,
        cors_origins=["http://localhost:3000"],
    )


@pytest.fixture
def stub_email_service() -> StubEmailService:
    """Provide isolated in-memory email outbox per test."""
    return StubEmailService()


@pytest.fixture
def rate_limiter(test_settings: Settings) -> SlidingWindowRateLimiter:
    """Provide isolated rate limiter per test."""
    return SlidingWindowRateLimiter(
        max_keys=test_settings.rate_limit_max_keys,
        scope_max_keys=test_settings.get_rate_limit_scope_max_keys(),
    )


@pytest.fixture
async def db_session(test_settings: Settings) -> AsyncIterator[AsyncSession]:
    """Create schema tables and yield an async SQLAlchemy session."""
    engine = create_engine(test_settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


@pytest.fixture
async def client(
    test_settings: Settings,
    stub_email_service: StubEmailService,
    rate_limiter: SlidingWindowRateLimiter,
) -> AsyncIterator[AsyncClient]:
    """Yield an async HTTPX test client wired to an isolated test DB and stub providers."""
    engine = create_engine(test_settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app(test_settings)
    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_email_service] = lambda: stub_email_service
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
            yield async_client
    finally:
        await engine.dispose()
