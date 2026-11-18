"""Real PostgreSQL 16 concurrency tests for M2 auth locking and atomic consumption."""

import asyncio
import socket
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.api.v1.deps import get_email_service, get_rate_limiter
from app.core.config import Settings
from app.core.rate_limit import SlidingWindowRateLimiter
from app.core.security import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    REFRESH_COOKIE_NAME,
    hash_opaque_token,
)
from app.db.session import create_engine, create_session_factory, get_db_session
from app.main import create_app
from app.models.auth import AuthActionToken, AuthSession, User
from app.services.email import StubEmailService


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


async def _create_pg_database(admin_url: str, db_name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def pg_database_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Start an ephemeral PostgreSQL 16 cluster, run migrations, and yield its async URL."""
    pg_bin = Path("/usr/lib/postgresql/16/bin")
    initdb_bin = pg_bin / "initdb"
    pg_ctl_bin = pg_bin / "pg_ctl"
    if not initdb_bin.exists() or not pg_ctl_bin.exists():
        pytest.skip("PostgreSQL 16 binaries not installed locally")

    pg_root = tmp_path_factory.mktemp("pg16_concurrency")
    pg_data = pg_root / "pgdata"
    pg_log = pg_root / "pg.log"
    port = _find_free_port()

    subprocess.run(
        [str(initdb_bin), "-D", str(pg_data), "-U", "postgres", "--auth=trust"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            str(pg_ctl_bin),
            "-D",
            str(pg_data),
            "-l",
            str(pg_log),
            "-o",
            f"-k {pg_root} -p {port}",
            "start",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    try:
        admin_url = f"postgresql+asyncpg://postgres@127.0.0.1:{port}/postgres"
        test_db_url = f"postgresql+asyncpg://postgres@127.0.0.1:{port}/doc_saas_concurrency_test"
        asyncio.run(_create_pg_database(admin_url, "doc_saas_concurrency_test"))

        backend_dir = Path(__file__).resolve().parents[1]
        alembic_cfg = Config(str(backend_dir / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", test_db_url)
        command.upgrade(alembic_cfg, "head")

        yield test_db_url
    finally:
        subprocess.run(
            [str(pg_ctl_bin), "-D", str(pg_data), "stop", "-m", "immediate"],
            check=False,
            capture_output=True,
            text=True,
        )


def _build_pg_app(
    pg_database_url: str,
) -> tuple[AsyncEngine, FastAPI, StubEmailService, Settings]:
    settings = Settings(
        environment="test",
        database_url=pg_database_url,
        internal_hmac_secret="pg-concurrency-test-hmac-secret-minimum-32-chars",
        jwt_secret_key="pg-concurrency-test-jwt-secret-minimum-32-chars",
        cookie_secure=False,
        cors_origins=["http://localhost:3000"],
    )
    engine = create_engine(pg_database_url)
    session_factory = create_session_factory(engine)
    email_service = StubEmailService()
    rate_limiter = SlidingWindowRateLimiter()

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app(settings)
    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_email_service] = lambda: email_service
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter
    return engine, app, email_service, settings


async def test_concurrent_refresh_allows_one_rotation_and_revokes_family_on_pg(
    pg_database_url: str,
) -> None:
    """Concurrent /auth/refresh allows one rotation and preserves reuse detection."""
    engine, app, _, _ = _build_pg_app(pg_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as reg_client:
            reg = await reg_client.post(
                "/api/v1/auth/register",
                json={
                    "email": "pg-concurrent-refresh@example.com",
                    "password": "StrongPassword123!",
                    "display_name": "PG Refresh",
                },
            )
            assert reg.status_code == 201
            csrf = reg.json()["data"]["csrf_token"]
            raw_refresh = reg_client.cookies.get(REFRESH_COOKIE_NAME)
            assert raw_refresh is not None

        async def send_refresh() -> Response:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
                cookies={REFRESH_COOKIE_NAME: raw_refresh, CSRF_COOKIE_NAME: csrf},
            ) as c:
                return await c.post(
                    "/api/v1/auth/refresh",
                    headers={"Origin": "http://localhost:3000", CSRF_HEADER_NAME: csrf},
                )

        responses = list(await asyncio.gather(*[send_refresh() for _ in range(6)]))
        statuses = [res.status_code for res in responses]
        assert statuses.count(200) == 1
        assert statuses.count(401) == 5

        winning_res = next(res for res in responses if res.status_code == 200)
        winning_child_refresh = winning_res.cookies.get(REFRESH_COOKIE_NAME)
        winning_child_csrf = winning_res.json()["data"]["csrf_token"]
        assert winning_child_refresh is not None

        for failed_res in [res for res in responses if res.status_code == 401]:
            lowered = [h.lower() for h in failed_res.headers.get_list("set-cookie")]
            assert any(
                h.startswith(f"{REFRESH_COOKIE_NAME}=") and "max-age=0" in h for h in lowered
            )
            assert any(h.startswith(f"{CSRF_COOKIE_NAME}=") and "max-age=0" in h for h in lowered)

        async with session_factory() as session:
            parent = await session.scalar(
                select(AuthSession).where(AuthSession.token_hash == hash_opaque_token(raw_refresh))
            )
            assert parent is not None
            family_sessions = (
                await session.scalars(
                    select(AuthSession)
                    .where(AuthSession.family_id == parent.family_id)
                    .order_by(AuthSession.created_at)
                )
            ).all()
            assert len(family_sessions) == 2
            child = next(s for s in family_sessions if s.id != parent.id)
            assert parent.replaced_by_id == child.id
            assert parent.revoked_at is not None
            # Replay by concurrent losers triggered family reuse revocation on child too.
            assert child.revoked_at is not None

        # Follow-up refresh with child cookie is rejected (family stays revoked).
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
            cookies={
                REFRESH_COOKIE_NAME: winning_child_refresh,
                CSRF_COOKIE_NAME: winning_child_csrf,
            },
        ) as followup_client:
            followup = await followup_client.post(
                "/api/v1/auth/refresh",
                headers={
                    "Origin": "http://localhost:3000",
                    CSRF_HEADER_NAME: winning_child_csrf,
                },
            )
            assert followup.status_code == 401
    finally:
        await engine.dispose()


async def test_concurrent_reset_password_consumes_token_once_on_pg(
    pg_database_url: str,
) -> None:
    """Concurrent /auth/reset-password requests allow exactly one success and revoke sessions."""
    engine, app, email_service, _ = _build_pg_app(pg_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            reg = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": "pg-concurrent-reset@example.com",
                    "password": "OldPassword123!",
                    "display_name": "PG Reset",
                },
            )
            assert reg.status_code == 201
            raw_refresh = client.cookies.get(REFRESH_COOKIE_NAME)
            assert raw_refresh is not None

            forgot = await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "pg-concurrent-reset@example.com"},
            )
            assert forgot.status_code == 200
            reset_token = [
                m for m in email_service.sent_messages if m.template == "password_reset"
            ][-1].token

        async def send_reset() -> Response:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
            ) as c:
                return await c.post(
                    "/api/v1/auth/reset-password",
                    json={"token": reset_token, "new_password": "NewStrongPassword456!"},
                )

        responses = list(await asyncio.gather(*[send_reset() for _ in range(6)]))
        statuses = [res.status_code for res in responses]
        assert statuses.count(200) == 1
        assert statuses.count(400) == 5

        async with session_factory() as session:
            token_row = await session.scalar(
                select(AuthActionToken).where(
                    AuthActionToken.token_hash == hash_opaque_token(reset_token)
                )
            )
            assert token_row is not None
            assert token_row.used_at is not None

            pre_reset_session = await session.scalar(
                select(AuthSession).where(AuthSession.token_hash == hash_opaque_token(raw_refresh))
            )
            assert pre_reset_session is not None
            assert pre_reset_session.revoked_at is not None

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as login_client:
            ok_login = await login_client.post(
                "/api/v1/auth/login",
                json={
                    "email": "pg-concurrent-reset@example.com",
                    "password": "NewStrongPassword456!",
                },
            )
            assert ok_login.status_code == 200
    finally:
        await engine.dispose()


async def test_concurrent_verify_email_consumes_token_once_on_pg(
    pg_database_url: str,
) -> None:
    """Concurrent /auth/verify-email requests consume the verification token exactly once."""
    engine, app, email_service, _ = _build_pg_app(pg_database_url)
    session_factory = create_session_factory(engine)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            reg = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": "pg-concurrent-verify@example.com",
                    "password": "StrongPassword123!",
                    "display_name": "PG Verify",
                },
            )
            assert reg.status_code == 201
            verify_token = [m for m in email_service.sent_messages if m.template == "verify_email"][
                -1
            ].token

        async def send_verify() -> Response:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
            ) as c:
                return await c.post(
                    "/api/v1/auth/verify-email",
                    json={"token": verify_token},
                )

        responses = list(await asyncio.gather(*[send_verify() for _ in range(6)]))
        statuses = [res.status_code for res in responses]
        assert statuses.count(200) == 1
        assert statuses.count(400) == 5

        async with session_factory() as session:
            user = await session.scalar(
                select(User).where(User.email == "pg-concurrent-verify@example.com")
            )
            assert user is not None
            assert user.email_verified_at is not None

            token_row = await session.scalar(
                select(AuthActionToken).where(
                    AuthActionToken.token_hash == hash_opaque_token(verify_token)
                )
            )
            assert token_row is not None
            assert token_row.used_at is not None
    finally:
        await engine.dispose()
