"""Authentication, session security, rotation, CSRF, and rate-limit tests (Milestone M2)."""

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import jwt
import pytest
from argon2 import PasswordHasher
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.api.v1.deps import get_email_service, get_rate_limiter
from app.core.config import Settings
from app.core.errors import AppError
from app.core.rate_limit import SlidingWindowRateLimiter, account_rate_limit_key
from app.core.security import (
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
    REFRESH_COOKIE_NAME,
    hash_opaque_token,
    password_needs_rehash,
    verify_csrf_and_origin,
    verify_password,
    verify_password_constant_time,
)
from app.db.base import Base
from app.db.session import create_engine, create_session_factory, get_db_session
from app.main import create_app
from app.models.auth import AuthActionToken, AuthSession, User
from app.services import auth as auth_service_mod
from app.services.email import StubEmailService


async def test_register_hashes_password_sets_cookies_and_sends_verification(
    client: AsyncClient,
    test_settings: Settings,
    stub_email_service: StubEmailService,
) -> None:
    """Registration hashes password with Argon2id, sets HttpOnly cookie, and sends email."""
    res = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "  Sakti.Admin@Example.com ",
            "password": "StrongPassword123!",
            "display_name": "Sakti Arif",
        },
    )
    assert res.status_code == 201
    body = res.json()
    assert body["success"] is True
    data = body["data"]
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == test_settings.access_token_expire_minutes * 60
    assert data["user"]["email"] == "sakti.admin@example.com"
    assert data["user"]["display_name"] == "Sakti Arif"
    assert data["user"]["email_verified"] is False
    assert "password_hash" not in data["user"]

    # Cookies set cleanly
    assert REFRESH_COOKIE_NAME in client.cookies
    assert CSRF_COOKIE_NAME in client.cookies
    set_cookie_headers = res.headers.get_list("set-cookie")
    refresh_header = next(h for h in set_cookie_headers if f"{REFRESH_COOKIE_NAME}=" in h)
    assert "httponly" in refresh_header.lower()

    # Stub verification email dispatched
    assert len(stub_email_service.sent_messages) == 1
    sent = stub_email_service.sent_messages[0]
    assert sent.to_email == "sakti.admin@example.com"
    assert sent.template == "verify_email"

    # Password stored as Argon2id hash, never plaintext; refresh token stored hashed
    engine = create_engine(test_settings.database_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "sakti.admin@example.com"))
        assert user is not None
        assert user.password_hash.startswith("$argon2id$")
        assert "StrongPassword123!" not in user.password_hash

        raw_refresh = client.cookies.get(REFRESH_COOKIE_NAME)
        assert raw_refresh is not None
        auth_session = await session.scalar(
            select(AuthSession).where(AuthSession.user_id == user.id)
        )
        assert auth_session is not None
        assert auth_session.token_hash == hash_opaque_token(raw_refresh)
        assert auth_session.token_hash != raw_refresh
    await engine.dispose()


async def test_duplicate_normalized_email_rejected(client: AsyncClient) -> None:
    """Registering a duplicate normalized email returns 409 CONFLICT."""
    first = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "User@Example.com",
            "password": "StrongPassword123!",
            "display_name": "User One",
        },
    )
    assert first.status_code == 201

    second = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "user@example.com",
            "password": "AnotherPassword123!",
            "display_name": "User Two",
        },
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "CONFLICT"


async def test_me_requires_valid_bearer_token(client: AsyncClient) -> None:
    """GET /auth/me returns user profile with Bearer JWT and rejects invalid/missing tokens."""
    unauth = await client.get("/api/v1/auth/me")
    assert unauth.status_code == 401
    assert unauth.json()["error"]["code"] == "UNAUTHENTICATED"

    bad = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer not-a-valid-jwt"},
    )
    assert bad.status_code == 401

    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "me@example.com",
            "password": "StrongPassword123!",
            "display_name": "Me User",
        },
    )
    token = reg.json()["data"]["access_token"]

    me_res = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_res.status_code == 200
    assert me_res.json()["data"]["email"] == "me@example.com"


async def test_email_verification_is_single_use(
    client: AsyncClient,
    stub_email_service: StubEmailService,
) -> None:
    """POST /auth/verify-email verifies email once and rejects token reuse."""
    await client.post(
        "/api/v1/auth/register",
        json={
            "email": "verify@example.com",
            "password": "StrongPassword123!",
            "display_name": "Verify User",
        },
    )
    token = stub_email_service.sent_messages[0].token

    ok = await client.post("/api/v1/auth/verify-email", json={"token": token})
    assert ok.status_code == 200
    assert ok.json()["data"]["email_verified"] is True

    reused = await client.post("/api/v1/auth/verify-email", json={"token": token})
    assert reused.status_code == 400
    assert reused.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_login_and_rate_limit(client: AsyncClient) -> None:
    """Login validates credentials and enforces 5 requests/minute/IP rate limit."""
    await client.post(
        "/api/v1/auth/register",
        json={
            "email": "login@example.com",
            "password": "StrongPassword123!",
            "display_name": "Login User",
        },
    )

    for _ in range(4):
        bad = await client.post(
            "/api/v1/auth/login",
            json={"email": "login@example.com", "password": "WrongPassword!"},
        )
        assert bad.status_code == 401

    good = await client.post(
        "/api/v1/auth/login",
        json={"email": "LOGIN@example.com", "password": "StrongPassword123!"},
    )
    assert good.status_code == 200
    assert "access_token" in good.json()["data"]

    # 6th request in the 60s window triggers rate limit
    limited = await client.post(
        "/api/v1/auth/login",
        json={"email": "login@example.com", "password": "StrongPassword123!"},
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


async def test_refresh_rotation_reuse_detection_and_csrf_protection(
    client: AsyncClient,
) -> None:
    """Refresh rotates tokens, enforces CSRF/Origin, and revokes family upon token reuse."""
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "rotate@example.com",
            "password": "StrongPassword123!",
            "display_name": "Rotate User",
        },
    )
    csrf_1 = reg.json()["data"]["csrf_token"]
    refresh_1 = client.cookies.get(REFRESH_COOKIE_NAME)
    assert refresh_1 is not None

    # Missing CSRF header -> 403 FORBIDDEN
    no_csrf = await client.post("/api/v1/auth/refresh", headers={"Origin": "http://localhost:3000"})
    assert no_csrf.status_code == 403
    assert no_csrf.json()["error"]["code"] == "FORBIDDEN"

    # Disallowed Origin -> 403 FORBIDDEN
    bad_origin = await client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "https://evil.example.com", CSRF_HEADER_NAME: csrf_1},
    )
    assert bad_origin.status_code == 403

    # Valid refresh rotates refresh token and CSRF token
    rotated = await client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "http://localhost:3000", CSRF_HEADER_NAME: csrf_1},
    )
    assert rotated.status_code == 200
    csrf_2 = rotated.json()["data"]["csrf_token"]
    refresh_2 = client.cookies.get(REFRESH_COOKIE_NAME)
    assert refresh_2 is not None
    assert refresh_2 != refresh_1

    # Replaying old refresh_1 triggers family revocation -> 401
    client.cookies.set(REFRESH_COOKIE_NAME, refresh_1)
    client.cookies.set(CSRF_COOKIE_NAME, csrf_2)
    replayed = await client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "http://localhost:3000", CSRF_HEADER_NAME: csrf_2},
    )
    assert replayed.status_code == 401

    # Even the newer refresh_2 from the compromised family is now revoked -> 401
    client.cookies.set(REFRESH_COOKIE_NAME, refresh_2)
    after_compromise = await client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "http://localhost:3000", CSRF_HEADER_NAME: csrf_2},
    )
    assert after_compromise.status_code == 401


async def test_logout_revokes_refresh_session(client: AsyncClient) -> None:
    """POST /auth/logout revokes the refresh session and rejects subsequent refresh."""
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "logout@example.com",
            "password": "StrongPassword123!",
            "display_name": "Logout User",
        },
    )
    csrf = reg.json()["data"]["csrf_token"]
    saved_refresh = client.cookies.get(REFRESH_COOKIE_NAME)
    assert saved_refresh is not None

    logout_res = await client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "http://localhost:3000", CSRF_HEADER_NAME: csrf},
    )
    assert logout_res.status_code == 200

    # Even if an attacker kept the old cookie, refresh must fail
    client.cookies.set(REFRESH_COOKIE_NAME, saved_refresh)
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    refresh_after = await client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "http://localhost:3000", CSRF_HEADER_NAME: csrf},
    )
    assert refresh_after.status_code == 401


async def test_forgot_and_reset_password_revokes_sessions(
    client: AsyncClient,
    stub_email_service: StubEmailService,
) -> None:
    """Forgot-password avoids enumeration & rate limits; reset-password revokes old sessions."""
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "reset@example.com",
            "password": "OldPassword123!",
            "display_name": "Reset User",
        },
    )
    csrf = reg.json()["data"]["csrf_token"]
    saved_refresh = client.cookies.get(REFRESH_COOKIE_NAME)
    assert saved_refresh is not None

    # Non-existent account returns identical generic 200 OK
    missing_res = await client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "nobody@example.com"},
    )
    assert missing_res.status_code == 200

    # Existing account receives password reset email in stub outbox
    ok_res = await client.post(
        "/api/v1/auth/forgot-password",
        json={"email": "reset@example.com"},
    )
    assert ok_res.status_code == 200
    assert ok_res.json()["data"]["message"] == missing_res.json()["data"]["message"]

    reset_msg = [m for m in stub_email_service.sent_messages if m.template == "password_reset"][-1]
    reset_token = reset_msg.token

    # Reset password
    reset_res = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": reset_token, "new_password": "NewStrongPassword456!"},
    )
    assert reset_res.status_code == 200

    # Existing refresh session from before password reset is revoked
    client.cookies.set(REFRESH_COOKIE_NAME, saved_refresh)
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    refresh_res = await client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "http://localhost:3000", CSRF_HEADER_NAME: csrf},
    )
    assert refresh_res.status_code == 401

    # Old password fails, new password succeeds
    old_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "reset@example.com", "password": "OldPassword123!"},
    )
    assert old_login.status_code == 401

    new_login = await client.post(
        "/api/v1/auth/login",
        json={"email": "reset@example.com", "password": "NewStrongPassword456!"},
    )
    assert new_login.status_code == 200

    # Forgot-password rate limit: 3/hour/account (1 already sent; 2 more allowed, 4th -> 429)
    for _ in range(2):
        r = await client.post("/api/v1/auth/forgot-password", json={"email": "reset@example.com"})
        assert r.status_code == 200
    rate_limited = await client.post(
        "/api/v1/auth/forgot-password", json={"email": "reset@example.com"}
    )
    assert rate_limited.status_code == 429


async def test_register_rate_limit(client: AsyncClient) -> None:
    """Register enforces 5 requests/hour/IP rate limit."""
    for idx in range(5):
        res = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"ratelimit{idx}@example.com",
                "password": "StrongPassword123!",
                "display_name": f"Rate User {idx}",
            },
        )
        assert res.status_code == 201

    limited = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "ratelimit5@example.com",
            "password": "StrongPassword123!",
            "display_name": "Rate User 5",
        },
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "RATE_LIMITED"


def _cookie_attrs(header: str) -> list[str]:
    return [part.strip().lower() for part in header.split(";")]


def _assert_cookies_cleared(headers: list[str]) -> None:
    refresh_header = next(
        h for h in headers if h.lower().startswith(f"{REFRESH_COOKIE_NAME}=")
    )
    refresh_attrs = _cookie_attrs(refresh_header)
    assert "max-age=0" in refresh_attrs
    assert "httponly" in refresh_attrs
    assert "path=/api/v1/auth" in refresh_attrs

    csrf_header = next(h for h in headers if h.lower().startswith(f"{CSRF_COOKIE_NAME}="))
    csrf_attrs = _cookie_attrs(csrf_header)
    assert "max-age=0" in csrf_attrs
    assert "path=/" in csrf_attrs
    assert "path=/api/v1/auth" not in csrf_attrs
    assert "httponly" not in csrf_attrs


async def test_email_verification_rejects_expired_and_cross_type_tokens(
    client: AsyncClient,
    test_settings: Settings,
    stub_email_service: StubEmailService,
) -> None:
    """Expired verification tokens and password-reset tokens are rejected on /verify-email."""
    await client.post(
        "/api/v1/auth/register",
        json={
            "email": "expired-verify@example.com",
            "password": "StrongPassword123!",
            "display_name": "Verify Exp",
        },
    )
    verify_token = stub_email_service.sent_messages[-1].token

    engine = create_engine(test_settings.database_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        token_row = await session.scalar(
            select(AuthActionToken).where(
                AuthActionToken.token_hash == hash_opaque_token(verify_token)
            )
        )
        assert token_row is not None
        token_row.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await session.commit()

    expired_res = await client.post("/api/v1/auth/verify-email", json={"token": verify_token})
    assert expired_res.status_code == 400
    assert expired_res.json()["error"]["code"] == "VALIDATION_ERROR"

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "expired-verify@example.com"))
        assert user is not None
        assert user.email_verified_at is None
    await engine.dispose()

    # Cross-type token isolation: reset token cannot verify email; verify token cannot reset pw.
    await client.post("/api/v1/auth/forgot-password", json={"email": "expired-verify@example.com"})
    reset_token = [m for m in stub_email_service.sent_messages if m.template == "password_reset"][
        -1
    ].token
    wrong_verify = await client.post("/api/v1/auth/verify-email", json={"token": reset_token})
    assert wrong_verify.status_code == 400

    await client.post(
        "/api/v1/auth/register",
        json={
            "email": "fresh-verify@example.com",
            "password": "StrongPassword123!",
            "display_name": "Fresh Verify",
        },
    )
    fresh_verify_token = stub_email_service.sent_messages[-1].token
    wrong_reset = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": fresh_verify_token, "new_password": "NewStrongPassword123!"},
    )
    assert wrong_reset.status_code == 400


async def test_password_reset_rejects_expired_reused_and_superseded_tokens(
    client: AsyncClient,
    test_settings: Settings,
    stub_email_service: StubEmailService,
) -> None:
    """New reset requests invalidate old tokens; expired or reused reset tokens fail."""
    await client.post(
        "/api/v1/auth/register",
        json={
            "email": "reset-edge@example.com",
            "password": "StrongPassword123!",
            "display_name": "Reset Edge",
        },
    )
    await client.post("/api/v1/auth/forgot-password", json={"email": "reset-edge@example.com"})
    token1 = stub_email_service.sent_messages[-1].token

    await client.post("/api/v1/auth/forgot-password", json={"email": "reset-edge@example.com"})
    token2 = stub_email_service.sent_messages[-1].token

    # Superseded token1 fails
    res1 = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": token1, "new_password": "AnotherPassword123!"},
    )
    assert res1.status_code == 400

    # Expire token2 -> fails
    engine = create_engine(test_settings.database_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        token2_row = await session.scalar(
            select(AuthActionToken).where(AuthActionToken.token_hash == hash_opaque_token(token2))
        )
        assert token2_row is not None
        token2_row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()
    await engine.dispose()

    res2_expired = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": token2, "new_password": "AnotherPassword123!"},
    )
    assert res2_expired.status_code == 400

    # Issue token3 -> single use succeeds, replay fails
    await client.post("/api/v1/auth/forgot-password", json={"email": "reset-edge@example.com"})
    token3 = stub_email_service.sent_messages[-1].token
    res3_ok = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": token3, "new_password": "UpdatedPassword456!"},
    )
    assert res3_ok.status_code == 200
    res3_replay = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": token3, "new_password": "UpdatedPassword789!"},
    )
    assert res3_replay.status_code == 400


async def test_expired_and_invalid_refresh_returns_401_and_clears_cookies(
    client: AsyncClient,
    test_settings: Settings,
) -> None:
    """401 failures on /auth/refresh must expire both refresh and CSRF cookies."""
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "refresh-exp@example.com",
            "password": "StrongPassword123!",
            "display_name": "Refresh Exp",
        },
    )
    csrf = reg.json()["data"]["csrf_token"]
    raw_refresh = client.cookies.get(REFRESH_COOKIE_NAME)
    assert raw_refresh is not None

    engine = create_engine(test_settings.database_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        auth_session = await session.scalar(
            select(AuthSession).where(AuthSession.token_hash == hash_opaque_token(raw_refresh))
        )
        assert auth_session is not None
        auth_session.expires_at = datetime.now(UTC) - timedelta(minutes=5)
        await session.commit()
    await engine.dispose()

    expired_refresh = await client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "http://localhost:3000", CSRF_HEADER_NAME: csrf},
    )
    assert expired_refresh.status_code == 401
    assert expired_refresh.json()["error"]["code"] == "UNAUTHENTICATED"
    _assert_cookies_cleared(expired_refresh.headers.get_list("set-cookie"))

    # Bogus cookie on /auth/refresh also clears cookies cleanly
    client.cookies.set(REFRESH_COOKIE_NAME, "completely-invalid-refresh-token")
    client.cookies.set(CSRF_COOKIE_NAME, csrf)
    bogus_refresh = await client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "http://localhost:3000", CSRF_HEADER_NAME: csrf},
    )
    assert bogus_refresh.status_code == 401
    _assert_cookies_cleared(bogus_refresh.headers.get_list("set-cookie"))


async def test_jwt_claims_and_rejection_of_expired_or_invalid_claims(
    client: AsyncClient,
    test_settings: Settings,
) -> None:
    """Access JWTs bind iss/aud/jti and reject expired or mismatched-claim tokens."""
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "jwt-claims@example.com",
            "password": "StrongPassword123!",
            "display_name": "JWT User",
        },
    )
    token = reg.json()["data"]["access_token"]
    user_id = reg.json()["data"]["user"]["id"]

    decoded = jwt.decode(
        token,
        test_settings.jwt_secret_key,
        algorithms=[test_settings.jwt_algorithm],
        issuer=test_settings.jwt_issuer,
        audience=test_settings.jwt_audience,
    )
    assert decoded["sub"] == user_id
    assert decoded["type"] == "access"
    assert decoded["iss"] == test_settings.jwt_issuer
    assert decoded["aud"] == test_settings.jwt_audience
    assert isinstance(decoded["jti"], str) and len(decoded["jti"]) >= 16

    now = datetime.now(UTC)
    base_claims: dict[str, str | int] = {
        "sub": user_id,
        "type": "access",
        "iss": test_settings.jwt_issuer,
        "aud": test_settings.jwt_audience,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=10)).timestamp()),
        "jti": str(uuid4()),
    }
    bad_tokens = [
        jwt.encode(
            {**base_claims, "exp": int((now - timedelta(minutes=1)).timestamp())},
            test_settings.jwt_secret_key,
            algorithm="HS256",
        ),
        jwt.encode(
            {**base_claims, "iss": "wrong-issuer"},
            test_settings.jwt_secret_key,
            algorithm="HS256",
        ),
        jwt.encode(
            {**base_claims, "aud": "wrong-audience"},
            test_settings.jwt_secret_key,
            algorithm="HS256",
        ),
        jwt.encode(
            {**base_claims, "type": "refresh"},
            test_settings.jwt_secret_key,
            algorithm="HS256",
        ),
        jwt.encode(
            {k: v for k, v in base_claims.items() if k != "jti"},
            test_settings.jwt_secret_key,
            algorithm="HS256",
        ),
        jwt.encode(base_claims, key="", algorithm="none"),
    ]
    for bad in bad_tokens:
        res = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {bad}"})
        assert res.status_code == 401


async def test_disabled_user_rejected_on_login_me_and_refresh(
    client: AsyncClient,
    test_settings: Settings,
) -> None:
    """Disabled accounts are rejected on login, /auth/me, and /auth/refresh."""
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "disabled@example.com",
            "password": "StrongPassword123!",
            "display_name": "Disabled User",
        },
    )
    access_token = reg.json()["data"]["access_token"]
    csrf = reg.json()["data"]["csrf_token"]
    raw_refresh = client.cookies.get(REFRESH_COOKIE_NAME)
    assert raw_refresh is not None

    engine = create_engine(test_settings.database_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "disabled@example.com"))
        assert user is not None
        user.status = "disabled"
        await session.commit()

    login_res = await client.post(
        "/api/v1/auth/login",
        json={"email": "disabled@example.com", "password": "StrongPassword123!"},
    )
    assert login_res.status_code == 401

    me_res = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert me_res.status_code == 401

    refresh_res = await client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "http://localhost:3000", CSRF_HEADER_NAME: csrf},
    )
    assert refresh_res.status_code == 401
    _assert_cookies_cleared(refresh_res.headers.get_list("set-cookie"))

    async with session_factory() as session:
        auth_session = await session.scalar(
            select(AuthSession).where(AuthSession.token_hash == hash_opaque_token(raw_refresh))
        )
        assert auth_session is not None
        assert auth_session.revoked_at is not None
    await engine.dispose()


async def test_constant_time_dummy_verify_and_argon2_rehash_on_login(
    client: AsyncClient,
    test_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nonexistent logins run dummy Argon2id verification; outdated user hashes rehash on login."""
    assert verify_password_constant_time("anything", None) is False

    observed_calls: list[str | None] = []
    original_verify = verify_password_constant_time

    def spy_verify(password: str, password_hash: str | None) -> bool:
        observed_calls.append(password_hash)
        return original_verify(password, password_hash)

    monkeypatch.setattr(auth_service_mod, "verify_password_constant_time", spy_verify)

    missing_res = await client.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "SomePassword123!"},
    )
    assert missing_res.status_code == 401
    assert observed_calls == [None]

    await client.post(
        "/api/v1/auth/register",
        json={
            "email": "rehash@example.com",
            "password": "StrongPassword123!",
            "display_name": "Rehash User",
        },
    )
    legacy_hasher = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
    legacy_hash = legacy_hasher.hash("StrongPassword123!")
    assert password_needs_rehash(legacy_hash) is True

    engine = create_engine(test_settings.database_url)
    session_factory = create_session_factory(engine)
    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.email == "rehash@example.com"))
        assert user is not None
        user.password_hash = legacy_hash
        await session.commit()

    login_res = await client.post(
        "/api/v1/auth/login",
        json={"email": "rehash@example.com", "password": "StrongPassword123!"},
    )
    assert login_res.status_code == 200

    async with session_factory() as session:
        upgraded = await session.scalar(select(User).where(User.email == "rehash@example.com"))
        assert upgraded is not None
        assert upgraded.password_hash != legacy_hash
        assert password_needs_rehash(upgraded.password_hash) is False
        assert verify_password("StrongPassword123!", upgraded.password_hash) is True
    await engine.dispose()


async def test_csrf_origin_and_referer_enforcement_matrix(
    client: AsyncClient,
    test_settings: Settings,
) -> None:
    """Cookie endpoints require valid Origin or Referer allowlist + double-submit CSRF."""
    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "csrf-matrix@example.com",
            "password": "StrongPassword123!",
            "display_name": "CSRF Matrix",
        },
    )
    csrf = reg.json()["data"]["csrf_token"]

    # Missing both Origin and Referer -> 403 and cookies are NOT cleared on 403
    no_origin_refresh = await client.post(
        "/api/v1/auth/refresh",
        headers={CSRF_HEADER_NAME: csrf},
    )
    assert no_origin_refresh.status_code == 403
    assert no_origin_refresh.headers.get_list("set-cookie") == []

    # Disallowed Origin / Referer / malformed Referer / missing or wrong CSRF -> 403
    for bad_headers in [
        {"Origin": "https://evil.example", CSRF_HEADER_NAME: csrf},
        {"Referer": "https://evil.example/steal", CSRF_HEADER_NAME: csrf},
        {"Referer": "not-a-valid-url", CSRF_HEADER_NAME: csrf},
        {"Origin": "http://localhost:3000"},
        {"Origin": "http://localhost:3000", CSRF_HEADER_NAME: "wrong-csrf"},
    ]:
        bad_refresh = await client.post("/api/v1/auth/refresh", headers=bad_headers)
        assert bad_refresh.status_code == 403
        bad_logout = await client.post("/api/v1/auth/logout", headers=bad_headers)
        assert bad_logout.status_code == 403

    # Missing Origin + allowed Referer fallback + matching CSRF -> 200 OK
    referer_refresh = await client.post(
        "/api/v1/auth/refresh",
        headers={"Referer": "http://localhost:3000/login?next=/app", CSRF_HEADER_NAME: csrf},
    )
    assert referer_refresh.status_code == 200
    next_csrf = referer_refresh.json()["data"]["csrf_token"]

    # /auth/logout missing Origin & Referer -> 403
    bad_logout_missing = await client.post(
        "/api/v1/auth/logout",
        headers={CSRF_HEADER_NAME: next_csrf},
    )
    assert bad_logout_missing.status_code == 403

    # /auth/logout with valid Referer fallback + matching CSRF -> 200 OK
    ok_logout = await client.post(
        "/api/v1/auth/logout",
        headers={"Referer": "http://localhost:3000/app", CSRF_HEADER_NAME: next_csrf},
    )
    assert ok_logout.status_code == 200

    # Explicit non-production opt-out allows missing Origin/Referer when CSRF matches
    relaxed_settings = test_settings.model_copy(update={"require_origin_or_referer": False})
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/refresh",
        "headers": [
            (b"cookie", b"csrf_token=csrf-ok"),
            (b"x-csrf-token", b"csrf-ok"),
        ],
    }
    verify_csrf_and_origin(Request(scope), relaxed_settings)
    with pytest.raises(AppError):
        verify_csrf_and_origin(Request(scope), test_settings)


def test_sliding_window_rate_limiter_pruning_saturation_and_hashing() -> None:
    """Rate limiter hashes account keys, purges expired buckets, and never evicts active keys."""
    key_a = account_rate_limit_key("login", "  Victim@Example.COM ")
    key_b = account_rate_limit_key("login", "victim@example.com")
    assert key_a == key_b
    assert "victim" not in key_a
    assert "@" not in key_a

    limiter = SlidingWindowRateLimiter(max_keys=3)
    limiter.check("k1", max_requests=2, window_seconds=10, now_monotonic=0.0)
    limiter.check("k2", max_requests=2, window_seconds=10, now_monotonic=0.0)
    limiter.check("k3", max_requests=2, window_seconds=10, now_monotonic=0.0)
    assert limiter.key_count == 3

    removed = limiter.prune_expired(now_monotonic=15.0)
    assert removed == 3
    assert limiter.key_count == 0

    # Expired buckets are purged on insert before rejecting unseen keys
    limiter.check("short-lived", max_requests=2, window_seconds=5, now_monotonic=0.0)
    limiter.check("active-1", max_requests=2, window_seconds=100, now_monotonic=10.0)
    limiter.check("active-2", max_requests=1, window_seconds=100, now_monotonic=10.0)
    limiter.check("active-3", max_requests=2, window_seconds=100, now_monotonic=12.0)
    assert limiter.key_count == 3
    assert "short-lived" not in limiter._buckets

    # Touch active-1, then try inserting active-4 while all 3 buckets are active -> fail-closed 429
    limiter.check("active-1", max_requests=5, window_seconds=100, now_monotonic=13.0)
    with pytest.raises(AppError) as sat_exc:
        limiter.check("active-4", max_requests=2, window_seconds=100, now_monotonic=14.0)
    assert sat_exc.value.status_code == 429
    assert sat_exc.value.code == "RATE_LIMITED"

    # Active buckets were never evicted and their existing counters stay enforced
    assert limiter.key_count == 3
    assert set(limiter._buckets.keys()) == {"active-1", "active-2", "active-3"}
    assert limiter.saturation_rejections("default") == 1
    assert limiter.saturation_rejections() == 1
    with pytest.raises(AppError) as active2_exc:
        limiter.check("active-2", max_requests=1, window_seconds=100, now_monotonic=14.5)
    assert active2_exc.value.status_code == 429


def test_rate_limiter_per_scope_isolation_no_active_eviction_and_safe_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scopes are capacity-isolated, reject unseen keys on saturation, and never log PII."""
    limiter = SlidingWindowRateLimiter(
        max_keys=10,
        scope_max_keys={
            "login:ip": 2,
            "login:acct": 2,
            "forgot:acct": 2,
            "register:ip": 2,
        },
    )
    assert limiter.max_keys_for_scope("login:acct") == 2
    assert limiter.max_keys_for_scope("forgot:ip") == 10

    victim_key = account_rate_limit_key("login", "victim@example.com")
    other_key = account_rate_limit_key("login", "other@example.com")
    flood_key_1 = account_rate_limit_key("login", "attacker-flood-1@example.com")
    flood_key_2 = account_rate_limit_key("login", "attacker-flood-2@example.com")

    # Saturate victim's account bucket and fill login:acct scope capacity (2 keys)
    limiter.check(victim_key, max_requests=2, window_seconds=300, now_monotonic=1.0)
    limiter.check(victim_key, max_requests=2, window_seconds=300, now_monotonic=2.0)
    limiter.check(other_key, max_requests=2, window_seconds=300, now_monotonic=3.0)
    assert limiter.scope_key_count("login:acct") == 2

    with caplog.at_level(logging.WARNING, logger="app.core.rate_limit"):
        # Attacker flooding new account keys fails closed with 429 and cannot evict victim_key
        for flood_key in (flood_key_1, flood_key_2):
            with pytest.raises(AppError) as exc:
                limiter.check(flood_key, max_requests=2, window_seconds=300, now_monotonic=10.0)
            assert exc.value.status_code == 429
            assert exc.value.code == "RATE_LIMITED"

        # Victim's active bucket remains throttled (not evicted or reset)
        with pytest.raises(AppError) as victim_exc:
            limiter.check(victim_key, max_requests=2, window_seconds=300, now_monotonic=11.0)
        assert victim_exc.value.status_code == 429

        # Saturating register:ip as well must not block forgot:ip, forgot:acct, or login:ip
        limiter.check(
            "register:ip:203.0.113.10", max_requests=5, window_seconds=3600, now_monotonic=12.0
        )
        limiter.check(
            "register:ip:203.0.113.11", max_requests=5, window_seconds=3600, now_monotonic=12.0
        )
        with pytest.raises(AppError) as reg_sat_exc:
            limiter.check(
                "register:ip:203.0.113.12",
                max_requests=5,
                window_seconds=3600,
                now_monotonic=12.0,
            )
        assert reg_sat_exc.value.status_code == 429

    # Independent scopes remain completely unaffected
    limiter.check("login:ip:198.51.100.50", max_requests=5, window_seconds=60, now_monotonic=12.0)
    limiter.check(
        "forgot:ip:203.0.113.12", max_requests=10, window_seconds=3600, now_monotonic=12.0
    )
    limiter.check(
        account_rate_limit_key("forgot", "victim@example.com"),
        max_requests=3,
        window_seconds=3600,
        now_monotonic=12.0,
    )
    assert limiter.scope_key_count("login:ip") == 1
    assert limiter.scope_key_count("forgot:ip") == 1
    assert limiter.scope_key_count("forgot:acct") == 1

    # Verify saturation metrics and safe warning logs without raw identifiers
    assert limiter.saturation_metrics() == {"login:acct": 2, "register:ip": 1}
    assert limiter.saturation_rejections() == 3
    saturation_logs = [
        rec.getMessage()
        for rec in caplog.records
        if "rate_limit_scope_saturated" in rec.getMessage()
    ]
    assert len(saturation_logs) == 3
    assert any(
        "scope=login:acct" in msg and "active_keys=2" in msg and "max_keys=2" in msg
        for msg in saturation_logs
    )
    for msg in saturation_logs:
        assert "victim" not in msg.lower()
        assert "attacker" not in msg.lower()
        assert "203.0.113" not in msg
        assert victim_key not in msg
        assert flood_key_1 not in msg

    # After sliding window expires, login:acct purges stale buckets and accepts new keys again
    limiter.check(flood_key_1, max_requests=2, window_seconds=300, now_monotonic=310.0)
    assert limiter.scope_key_count("login:acct") == 1


async def test_rate_limiter_fail_closed_saturation_availability_behavior(
    tmp_path: Path,
    stub_email_service: StubEmailService,
) -> None:
    """Saturated login:ip fails closed (429) for unseen IPs; tracked IPs & other scopes pass."""
    db_file = tmp_path / "saturation_avail.sqlite"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{db_file}",
        internal_hmac_secret="test-secret-for-unit-tests-only-minimum-32-chars",
        jwt_secret_key="test-jwt-secret-for-unit-tests-only-minimum-32-chars",
        rate_limit_login_ip_max=5,
        rate_limit_login_ip_max_keys=2,
        rate_limit_login_account_max_keys=10,
        rate_limit_register_ip_max_keys=10,
        rate_limit_forgot_ip_max_keys=10,
        rate_limit_forgot_account_max_keys=10,
    )
    engine = create_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    limiter = SlidingWindowRateLimiter(
        max_keys=settings.rate_limit_max_keys,
        scope_max_keys=settings.get_rate_limit_scope_max_keys(),
    )

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app(settings)
    app.dependency_overrides[get_db_session] = override_get_db
    app.dependency_overrides[get_email_service] = lambda: stub_email_service
    app.dependency_overrides[get_rate_limiter] = lambda: limiter

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("198.51.100.1", 1000)),
        base_url="http://testserver",
    ) as reg_client:
        reg = await reg_client.post(
            "/api/v1/auth/register",
            json={
                "email": "sat-user@example.com",
                "password": "StrongPassword123!",
                "display_name": "Sat User",
            },
        )
        assert reg.status_code == 201

    # Saturate login:ip scope (max_keys=2) with 2 distinct client IPs
    for ip_addr in ("198.51.100.10", "198.51.100.11"):
        async with AsyncClient(
            transport=ASGITransport(app=app, client=(ip_addr, 2000)),
            base_url="http://testserver",
        ) as c:
            res = await c.post(
                "/api/v1/auth/login",
                json={"email": "sat-user@example.com", "password": "WrongPassword123!"},
            )
            assert res.status_code == 401

    # 3rd unseen IP fails closed on /login with 429 due to login:ip saturation
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("198.51.100.12", 2002)),
        base_url="http://testserver",
    ) as unseen_client:
        sat_res = await unseen_client.post(
            "/api/v1/auth/login",
            json={"email": "sat-user@example.com", "password": "StrongPassword123!"},
        )
        assert sat_res.status_code == 429
        assert sat_res.json()["error"]["code"] == "RATE_LIMITED"

        # Other scopes (register & forgot-password) remain available from the 3rd IP
        reg_other = await unseen_client.post(
            "/api/v1/auth/register",
            json={
                "email": "another-sat@example.com",
                "password": "StrongPassword123!",
                "display_name": "Another Sat",
            },
        )
        assert reg_other.status_code == 201
        forgot_other = await unseen_client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "sat-user@example.com"},
        )
        assert forgot_other.status_code == 200

    # Already-tracked IP with remaining quota can still log in cleanly
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("198.51.100.10", 2003)),
        base_url="http://testserver",
    ) as existing_ip_client:
        ok_login = await existing_ip_client.post(
            "/api/v1/auth/login",
            json={"email": "sat-user@example.com", "password": "StrongPassword123!"},
        )
        assert ok_login.status_code == 200

    assert limiter.saturation_metrics() == {"login:ip": 1}
    await engine.dispose()


async def test_login_and_forgot_password_dual_ip_and_hashed_account_limits(
    tmp_path: Path,
    stub_email_service: StubEmailService,
) -> None:
    """Login and forgot-password enforce independent IP and hashed-account limits across IPs."""
    db_file = tmp_path / "dual_limit.sqlite"
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{db_file}",
        internal_hmac_secret="test-secret-for-unit-tests-only-minimum-32-chars",
        jwt_secret_key="test-jwt-secret-for-unit-tests-only-minimum-32-chars",
        rate_limit_login_ip_max=5,
        rate_limit_login_account_max=5,
        rate_limit_forgot_ip_max=10,
        rate_limit_forgot_account_max=3,
    )
    engine = create_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    limiter = SlidingWindowRateLimiter(max_keys=100)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app(settings)
    app.dependency_overrides[get_db_session] = override_get_db
    app.dependency_overrides[get_email_service] = lambda: stub_email_service
    app.dependency_overrides[get_rate_limiter] = lambda: limiter

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("198.51.100.1", 1000)),
        base_url="http://testserver",
    ) as reg_client:
        reg = await reg_client.post(
            "/api/v1/auth/register",
            json={
                "email": "victim@example.com",
                "password": "StrongPassword123!",
                "display_name": "Victim",
            },
        )
        assert reg.status_code == 201

    # Rotating IPs cannot bypass the per-account login limit
    for idx in range(5):
        async with AsyncClient(
            transport=ASGITransport(app=app, client=(f"198.51.100.{10 + idx}", 2000 + idx)),
            base_url="http://testserver",
        ) as attacker_client:
            attempt = await attacker_client.post(
                "/api/v1/auth/login",
                json={"email": "Victim@example.com", "password": "WrongPassword123!"},
            )
            assert attempt.status_code == 401

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("198.51.100.99", 2099)),
        base_url="http://testserver",
    ) as blocked_client:
        blocked = await blocked_client.post(
            "/api/v1/auth/login",
            json={"email": "victim@example.com", "password": "StrongPassword123!"},
        )
        assert blocked.status_code == 429

        # Another account from this unused IP is unaffected by victim's account throttle
        other_attempt = await blocked_client.post(
            "/api/v1/auth/login",
            json={"email": "other@example.com", "password": "WrongPassword123!"},
        )
        assert other_attempt.status_code == 401

    # Rotating IPs cannot bypass the per-account forgot-password limit
    for idx in range(3):
        async with AsyncClient(
            transport=ASGITransport(app=app, client=(f"203.0.113.{10 + idx}", 3000 + idx)),
            base_url="http://testserver",
        ) as forgot_client:
            res = await forgot_client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "victim@example.com"},
            )
            assert res.status_code == 200

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.99", 3099)),
        base_url="http://testserver",
    ) as forgot_blocked_client:
        forgot_blocked = await forgot_blocked_client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "victim@example.com"},
        )
        assert forgot_blocked.status_code == 429

    # Raw email addresses must never be stored in rate-limiter bucket keys
    for bucket_key in limiter._buckets:
        assert "victim@example.com" not in bucket_key.lower()
        assert "other@example.com" not in bucket_key.lower()

    await engine.dispose()


async def test_cookie_attributes_creation_and_deletion_paths(
    client: AsyncClient,
    test_settings: Settings,
) -> None:
    """CSRF cookie uses Path=/ while refresh cookie stays HttpOnly under /api/v1/auth."""
    expected_max_age = f"max-age={test_settings.refresh_token_expire_days * 86400}"

    def _assert_session_cookie_headers(headers: list[str]) -> None:
        refresh_header = next(
            h for h in headers if h.lower().startswith(f"{REFRESH_COOKIE_NAME}=")
        )
        refresh_attrs = _cookie_attrs(refresh_header)
        assert "httponly" in refresh_attrs
        assert "path=/api/v1/auth" in refresh_attrs
        assert f"samesite={test_settings.cookie_samesite}" in refresh_attrs
        assert expected_max_age in refresh_attrs

        csrf_header = next(h for h in headers if h.lower().startswith(f"{CSRF_COOKIE_NAME}="))
        csrf_attrs = _cookie_attrs(csrf_header)
        assert "httponly" not in csrf_attrs
        assert "path=/" in csrf_attrs
        assert "path=/api/v1/auth" not in csrf_attrs
        assert f"samesite={test_settings.cookie_samesite}" in csrf_attrs
        assert expected_max_age in csrf_attrs

    reg = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "cookie-paths@example.com",
            "password": "StrongPassword123!",
            "display_name": "Cookie Paths",
        },
    )
    assert reg.status_code == 201
    _assert_session_cookie_headers(reg.headers.get_list("set-cookie"))

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "cookie-paths@example.com", "password": "StrongPassword123!"},
    )
    assert login.status_code == 200
    _assert_session_cookie_headers(login.headers.get_list("set-cookie"))
    csrf_after_login = login.json()["data"]["csrf_token"]

    # Double-submit CSRF remains enforced on refresh and logout
    bad_refresh = await client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "http://localhost:3000", CSRF_HEADER_NAME: "wrong-csrf"},
    )
    assert bad_refresh.status_code == 403

    refreshed = await client.post(
        "/api/v1/auth/refresh",
        headers={"Origin": "http://localhost:3000", CSRF_HEADER_NAME: csrf_after_login},
    )
    assert refreshed.status_code == 200
    _assert_session_cookie_headers(refreshed.headers.get_list("set-cookie"))
    csrf_after_refresh = refreshed.json()["data"]["csrf_token"]

    bad_logout = await client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "http://localhost:3000", CSRF_HEADER_NAME: csrf_after_login},
    )
    assert bad_logout.status_code == 403

    logout = await client.post(
        "/api/v1/auth/logout",
        headers={"Origin": "http://localhost:3000", CSRF_HEADER_NAME: csrf_after_refresh},
    )
    assert logout.status_code == 200
    logout_headers = logout.headers.get_list("set-cookie")
    _assert_cookies_cleared(logout_headers)
    for header in logout_headers:
        assert f"samesite={test_settings.cookie_samesite}" in _cookie_attrs(header)
    assert client.cookies.get(REFRESH_COOKIE_NAME) is None
    assert client.cookies.get(CSRF_COOKIE_NAME) is None
