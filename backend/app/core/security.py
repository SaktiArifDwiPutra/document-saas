"""Password hashing, token generation, JWT helpers, and CSRF/origin verification."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request
from jwt import PyJWTError

from app.core.config import Settings
from app.core.errors import AppError

REFRESH_COOKIE_NAME = "refresh_token"
CSRF_COOKIE_NAME = "csrf_token"
CSRF_HEADER_NAME = "X-CSRF-Token"

_password_hasher = PasswordHasher()
# Precompute once at import using identical Argon2id cost parameters for timing equalization.
_DUMMY_PASSWORD_HASH = _password_hasher.hash("dummy-timing-equalization-secret")


def ensure_utc(dt: datetime) -> datetime:
    """Normalize naive datetimes (such as SQLite returns) to UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def normalize_email(email: str) -> str:
    """Normalize email identity consistently."""
    return email.strip().lower()


def hash_password(password: str) -> str:
    """Hash a plaintext password using Argon2id."""
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify plaintext password against an Argon2id hash."""
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def verify_password_constant_time(password: str, password_hash: str | None) -> bool:
    """Verify password with a dummy Argon2id check when no stored hash exists."""
    if password_hash is None:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        return False
    return verify_password(password, password_hash)


def password_needs_rehash(password_hash: str) -> bool:
    """Return True when a stored Argon2id hash should be upgraded to current parameters."""
    return _password_hasher.check_needs_rehash(password_hash)


def generate_opaque_token() -> str:
    """Generate a high-entropy URL-safe random token."""
    return secrets.token_urlsafe(48)


def hash_opaque_token(token: str) -> str:
    """Compute SHA-256 hex digest of an opaque token for database storage."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_access_token(
    *,
    user_id: UUID,
    settings: Settings,
    now: datetime | None = None,
) -> tuple[str, int]:
    """Issue a short-lived JWT access token and return (token, expires_in_seconds)."""
    issued_at = ensure_utc(now) if now is not None else datetime.now(UTC)
    expires_delta = timedelta(minutes=settings.access_token_expire_minutes)
    expires_at = issued_at + expires_delta
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": str(uuid4()),
    }
    encoded = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return encoded, int(expires_delta.total_seconds())


def decode_access_token(token: str, settings: Settings) -> UUID:
    """Validate and decode a Bearer access JWT, returning the user UUID."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            options={"require": ["exp", "iat", "sub", "type", "jti", "iss", "aud"]},
        )
        if payload.get("type") != "access":
            raise ValueError("Invalid token type")
        sub = payload.get("sub")
        jti = payload.get("jti")
        if not isinstance(sub, str) or not isinstance(jti, str) or not jti.strip():
            raise ValueError("Missing required token claims")
        return UUID(sub)
    except (PyJWTError, ValueError) as exc:
        raise AppError(
            code="UNAUTHENTICATED",
            message="Token akses tidak valid atau telah kedaluwarsa.",
            status_code=401,
        ) from exc


def verify_csrf_and_origin(request: Request, settings: Settings) -> None:
    """Enforce Origin/Referer allowlist and double-submit CSRF verification for cookie endpoints."""
    allowed_origins = {
        origin.strip().rstrip("/").lower() for origin in settings.cors_origins if origin.strip()
    }
    origin_header = request.headers.get("origin")
    referer_header = request.headers.get("referer")

    if origin_header is not None:
        normalized_origin = origin_header.strip().rstrip("/").lower()
        if not normalized_origin or normalized_origin not in allowed_origins:
            raise AppError(
                code="FORBIDDEN",
                message="Asal permintaan (Origin) tidak diizinkan.",
                status_code=403,
            )
    elif referer_header is not None:
        parsed_referer = urlparse(referer_header.strip())
        referer_origin = (
            f"{parsed_referer.scheme}://{parsed_referer.netloc}".lower()
            if parsed_referer.scheme and parsed_referer.netloc
            else ""
        )
        if not referer_origin or referer_origin not in allowed_origins:
            raise AppError(
                code="FORBIDDEN",
                message="Asal permintaan (Referer) tidak diizinkan.",
                status_code=403,
            )
    elif settings.require_origin_or_referer:
        raise AppError(
            code="FORBIDDEN",
            message="Header Origin atau Referer wajib disertakan.",
            status_code=403,
        )

    csrf_cookie = request.cookies.get(CSRF_COOKIE_NAME, "").strip()
    csrf_header = request.headers.get(CSRF_HEADER_NAME, "").strip()
    if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
        raise AppError(
            code="FORBIDDEN",
            message="Verifikasi CSRF tidak valid.",
            status_code=403,
        )
