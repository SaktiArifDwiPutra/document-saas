"""FastAPI dependency providers for auth, DB sessions, rate limiting, and stub services."""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.rate_limit import SlidingWindowRateLimiter
from app.core.security import decode_access_token
from app.db.session import get_db_session
from app.models.auth import User
from app.services.auth import AuthService
from app.services.credit import CreditService
from app.services.email import EmailService, StubEmailService
from app.services.workspace import WorkspaceService

_bearer_scheme = HTTPBearer(auto_error=False)
_default_email_service = StubEmailService()
_default_rate_limiter: SlidingWindowRateLimiter | None = None


def get_email_service() -> EmailService:
    """Return configured transactional email service (stub in local/test)."""
    return _default_email_service


def get_rate_limiter(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SlidingWindowRateLimiter:
    """Return shared sliding-window rate limiter configured with per-scope bounded capacities."""
    global _default_rate_limiter
    scope_max_keys = settings.get_rate_limit_scope_max_keys()
    if (
        _default_rate_limiter is None
        or _default_rate_limiter.max_keys != settings.rate_limit_max_keys
        or _default_rate_limiter.scope_max_keys != scope_max_keys
    ):
        _default_rate_limiter = SlidingWindowRateLimiter(
            max_keys=settings.rate_limit_max_keys,
            scope_max_keys=scope_max_keys,
        )
    return _default_rate_limiter


def get_client_ip(request: Request) -> str:
    """Extract client IP address safely without trusting spoofable headers unconditionally."""
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


def get_auth_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    email_service: Annotated[EmailService, Depends(get_email_service)],
) -> AuthService:
    """Construct AuthService with injected DB session, settings, and email provider."""
    return AuthService(session=session, settings=settings, email_service=email_service)


def get_workspace_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    email_service: Annotated[EmailService, Depends(get_email_service)],
) -> WorkspaceService:
    """Construct WorkspaceService with injected DB session, settings, and email provider."""
    return WorkspaceService(session=session, settings=settings, email_service=email_service)


def get_credit_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreditService:
    """Construct CreditService with injected DB session and settings."""
    return CreditService(session=session, settings=settings)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    """Authenticate Bearer JWT and return active User from database."""
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AppError(
            code="UNAUTHENTICATED",
            message="Autentikasi diperlukan.",
            status_code=401,
        )

    user_id = decode_access_token(credentials.credentials, settings)
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None or user.status != "active":
        raise AppError(
            code="UNAUTHENTICATED",
            message="Pengguna tidak ditemukan atau tidak aktif.",
            status_code=401,
        )
    return user
