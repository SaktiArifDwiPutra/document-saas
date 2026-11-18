"""FastAPI dependency providers for auth, DB sessions, rate limiting, and stub services."""

from typing import Annotated

import httpx
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.core.internal_auth import InMemoryReplayGuard
from app.core.rate_limit import SlidingWindowRateLimiter
from app.core.security import decode_access_token
from app.db.session import get_db_session
from app.models.auth import User
from app.services.auth import AuthService
from app.services.credit import CreditService
from app.services.document import DocumentService
from app.services.email import EmailService, StubEmailService
from app.services.gemini import (
    DeterministicMeetingMinutesProvider,
    GeminiProvider,
    HttpGeminiProvider,
)
from app.services.internal_workflow import InternalWorkflowService
from app.services.storage import InMemoryObjectStorage, ObjectStorageService
from app.services.workspace import WorkspaceService

_bearer_scheme = HTTPBearer(auto_error=False)
_default_email_service = StubEmailService()
_default_object_storage = InMemoryObjectStorage()
_default_internal_replay_guard: InMemoryReplayGuard | None = None
_default_rate_limiter: SlidingWindowRateLimiter | None = None


def get_internal_replay_guard(
    settings: Annotated[Settings, Depends(get_settings)],
) -> InMemoryReplayGuard:
    """Return shared internal replay guard bound to configured timestamp tolerance."""
    global _default_internal_replay_guard
    if _default_internal_replay_guard is None:
        _default_internal_replay_guard = InMemoryReplayGuard(
            tolerance_seconds=settings.internal_hmac_timestamp_tolerance_seconds
        )
    return _default_internal_replay_guard


def get_email_service() -> EmailService:
    """Return configured transactional email service (stub in local/test)."""
    return _default_email_service


def get_object_storage() -> ObjectStorageService:
    """Return configured private object storage service (in-memory in local/test)."""
    return _default_object_storage


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


def get_document_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    storage: Annotated[ObjectStorageService, Depends(get_object_storage)],
) -> DocumentService:
    """Construct DocumentService with injected DB session, settings, and storage."""
    return DocumentService(session=session, settings=settings, storage=storage)


def get_internal_workflow_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    replay_guard: Annotated[InMemoryReplayGuard, Depends(get_internal_replay_guard)],
) -> InternalWorkflowService:
    """Construct InternalWorkflowService with DB session, settings, and replay guard."""
    return InternalWorkflowService(
        session=session,
        settings=settings,
        replay_guard=replay_guard,
    )


def get_gemini_provider(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> GeminiProvider:
    """Return configured Gemini provider (or app.state override for test transports)."""
    override = getattr(request.app.state, "gemini_provider", None)
    if override is not None:
        return override  # type: ignore[no-any-return]
    if settings.gemini_provider_mode == "http":
        return HttpGeminiProvider(settings=settings)
    return DeterministicMeetingMinutesProvider(model_name=settings.gemini_model)


def get_worker_http_client(request: Request) -> httpx.AsyncClient | None:
    """Return optional Worker AsyncClient bound on app.state (used in ASGI/integration tests)."""
    client = getattr(request.app.state, "worker_http_client", None)
    return client if isinstance(client, httpx.AsyncClient) else None


def get_n8n_http_client(request: Request) -> httpx.AsyncClient | None:
    """Return optional n8n AsyncClient bound on app.state (used in ASGI/integration tests)."""
    client = getattr(request.app.state, "n8n_http_client", None)
    return client if isinstance(client, httpx.AsyncClient) else None


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
