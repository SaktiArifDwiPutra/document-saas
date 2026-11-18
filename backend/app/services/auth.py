"""Authentication and session lifecycle service."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.core.security import (
    create_access_token,
    ensure_utc,
    generate_opaque_token,
    hash_opaque_token,
    hash_password,
    normalize_email,
    password_needs_rehash,
    verify_password_constant_time,
)
from app.models.auth import AuthActionToken, AuthSession, User
from app.models.workspace import Workspace, WorkspaceMember
from app.schemas.auth import AuthTokenResponse, UserProfileResponse
from app.services.email import EmailService


def to_user_profile(user: User) -> UserProfileResponse:
    """Map User ORM entity to safe public profile contract."""
    return UserProfileResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        email_verified=user.email_verified_at is not None,
        status=user.status,
        created_at=ensure_utc(user.created_at),
    )


class AuthService:
    """Owns user registration, authentication, token rotation, and password recovery."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        email_service: EmailService,
    ) -> None:
        self._session = session
        self._settings = settings
        self._email_service = email_service

    async def _issue_session_tokens(
        self,
        user: User,
        *,
        family_id: UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[AuthTokenResponse, str, AuthSession]:
        now = datetime.now(UTC)
        raw_refresh_token = generate_opaque_token()
        csrf_token = generate_opaque_token()
        refresh_expires_at = now + timedelta(days=self._settings.refresh_token_expire_days)

        auth_session = AuthSession(
            user_id=user.id,
            token_hash=hash_opaque_token(raw_refresh_token),
            family_id=family_id or uuid4(),
            expires_at=refresh_expires_at,
            created_at=now,
            ip_address=ip_address[:64] if ip_address else None,
            user_agent=user_agent[:512] if user_agent else None,
        )
        self._session.add(auth_session)
        await self._session.flush()

        access_token, expires_in = create_access_token(
            user_id=user.id,
            settings=self._settings,
            now=now,
        )
        response = AuthTokenResponse(
            access_token=access_token,
            expires_in=expires_in,
            csrf_token=csrf_token,
            user=to_user_profile(user),
        )
        return response, raw_refresh_token, auth_session

    async def _create_action_token(
        self,
        *,
        user_id: UUID,
        token_type: str,
        expires_delta: timedelta,
    ) -> str:
        now = datetime.now(UTC)
        existing_ids = (
            await self._session.scalars(
                select(AuthActionToken.id)
                .where(
                    AuthActionToken.user_id == user_id,
                    AuthActionToken.token_type == token_type,
                    AuthActionToken.used_at.is_(None),
                )
                .order_by(AuthActionToken.id)
                .with_for_update()
            )
        ).all()
        if existing_ids:
            await self._session.execute(
                update(AuthActionToken)
                .where(AuthActionToken.id.in_(existing_ids), AuthActionToken.used_at.is_(None))
                .values(used_at=now)
            )

        raw_token = generate_opaque_token()
        action_token = AuthActionToken(
            user_id=user_id,
            token_type=token_type,
            token_hash=hash_opaque_token(raw_token),
            expires_at=now + expires_delta,
            created_at=now,
        )
        self._session.add(action_token)
        return raw_token

    async def _revoke_family(self, family_id: UUID, now: datetime) -> None:
        session_ids = (
            await self._session.scalars(
                select(AuthSession.id)
                .where(AuthSession.family_id == family_id, AuthSession.revoked_at.is_(None))
                .order_by(AuthSession.id)
                .with_for_update()
            )
        ).all()
        if session_ids:
            await self._session.execute(
                update(AuthSession)
                .where(AuthSession.id.in_(session_ids), AuthSession.revoked_at.is_(None))
                .values(revoked_at=now)
            )

    async def _revoke_all_user_sessions(self, user_id: UUID, now: datetime) -> None:
        session_ids = (
            await self._session.scalars(
                select(AuthSession.id)
                .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
                .order_by(AuthSession.id)
                .with_for_update()
            )
        ).all()
        if session_ids:
            await self._session.execute(
                update(AuthSession)
                .where(AuthSession.id.in_(session_ids), AuthSession.revoked_at.is_(None))
                .values(revoked_at=now)
            )

    async def register(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[AuthTokenResponse, str]:
        """Register a new user, issue initial session, and queue stub verification email."""
        normalized = normalize_email(email)
        existing = await self._session.scalar(select(User).where(User.email == normalized))
        if existing is not None:
            raise AppError(
                code="CONFLICT",
                message="Email sudah terdaftar.",
                status_code=409,
            )

        user = User(
            email=normalized,
            password_hash=hash_password(password),
            display_name=display_name.strip(),
            status="active",
        )
        self._session.add(user)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise AppError(
                code="CONFLICT",
                message="Email sudah terdaftar.",
                status_code=409,
            ) from exc

        personal_workspace = Workspace(
            name=f"Workspace Pribadi - {user.display_name}"[:120],
            type="personal",
            owner_user_id=user.id,
            status="active",
        )
        self._session.add(personal_workspace)
        await self._session.flush()
        self._session.add(
            WorkspaceMember(
                workspace_id=personal_workspace.id,
                user_id=user.id,
                role="owner",
                status="active",
            )
        )

        verification_token = await self._create_action_token(
            user_id=user.id,
            token_type="email_verification",
            expires_delta=timedelta(hours=self._settings.email_verification_expire_hours),
        )
        token_response, raw_refresh_token, _ = await self._issue_session_tokens(
            user,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self._session.commit()

        await self._email_service.send_verification_email(
            to_email=user.email,
            token=verification_token,
        )
        return token_response, raw_refresh_token

    async def login(
        self,
        *,
        email: str,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[AuthTokenResponse, str]:
        """Verify credentials and issue short-lived access JWT + refresh session."""
        normalized = normalize_email(email)
        user = await self._session.scalar(select(User).where(User.email == normalized))
        password_ok = verify_password_constant_time(
            password, user.password_hash if user is not None else None
        )
        if not password_ok or user is None or user.status != "active":
            raise AppError(
                code="UNAUTHENTICATED",
                message="Email atau kata sandi tidak valid.",
                status_code=401,
            )

        if password_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)

        token_response, raw_refresh_token, _ = await self._issue_session_tokens(
            user,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        await self._session.commit()
        return token_response, raw_refresh_token

    async def refresh(
        self,
        *,
        raw_refresh_token: str | None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple[AuthTokenResponse, str]:
        """Rotate refresh session and revoke the entire family on token reuse."""
        if not raw_refresh_token:
            raise AppError(
                code="UNAUTHENTICATED",
                message="Sesi refresh tidak ditemukan.",
                status_code=401,
            )

        now = datetime.now(UTC)
        token_hash = hash_opaque_token(raw_refresh_token)
        existing_session = await self._session.scalar(
            select(AuthSession)
            .where(AuthSession.token_hash == token_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing_session is None:
            raise AppError(
                code="UNAUTHENTICATED",
                message="Sesi refresh tidak valid.",
                status_code=401,
            )

        family_id = existing_session.family_id

        # Reuse or post-revocation replay detected -> release single-row lock, then revoke family.
        if existing_session.revoked_at is not None or existing_session.replaced_by_id is not None:
            await self._session.rollback()
            await self._revoke_family(family_id, now)
            await self._session.commit()
            raise AppError(
                code="UNAUTHENTICATED",
                message="Sesi refresh telah dicabut atau digunakan ulang.",
                status_code=401,
            )

        if ensure_utc(existing_session.expires_at) <= now:
            await self._session.execute(
                update(AuthSession)
                .where(AuthSession.id == existing_session.id, AuthSession.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            await self._session.commit()
            raise AppError(
                code="UNAUTHENTICATED",
                message="Sesi refresh telah kedaluwarsa.",
                status_code=401,
            )

        user = await self._session.scalar(select(User).where(User.id == existing_session.user_id))
        if user is None or user.status != "active":
            await self._session.rollback()
            await self._revoke_family(family_id, now)
            await self._session.commit()
            raise AppError(
                code="UNAUTHENTICATED",
                message="Akun pengguna tidak aktif.",
                status_code=401,
            )

        token_response, new_raw_refresh_token, new_session = await self._issue_session_tokens(
            user,
            family_id=family_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        rotated_id = await self._session.scalar(
            update(AuthSession)
            .where(
                AuthSession.id == existing_session.id,
                AuthSession.revoked_at.is_(None),
                AuthSession.replaced_by_id.is_(None),
            )
            .values(revoked_at=now, last_used_at=now, replaced_by_id=new_session.id)
            .returning(AuthSession.id)
            .execution_options(synchronize_session=False)
        )
        if rotated_id is None:
            await self._session.rollback()
            await self._revoke_family(family_id, now)
            await self._session.commit()
            raise AppError(
                code="UNAUTHENTICATED",
                message="Sesi refresh telah dicabut atau digunakan ulang.",
                status_code=401,
            )

        await self._session.commit()
        return token_response, new_raw_refresh_token

    async def logout(self, *, raw_refresh_token: str | None) -> None:
        """Revoke the current refresh session if present."""
        if not raw_refresh_token:
            return
        token_hash = hash_opaque_token(raw_refresh_token)
        existing_session = await self._session.scalar(
            select(AuthSession)
            .where(AuthSession.token_hash == token_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing_session is not None and existing_session.revoked_at is None:
            existing_session.revoked_at = datetime.now(UTC)
            await self._session.commit()

    async def _lock_and_consume_action_token(
        self,
        *,
        raw_token: str,
        token_type: str,
        now: datetime,
        expired_or_used_message: str,
        invalid_user_message: str,
    ) -> User:
        token_hash = hash_opaque_token(raw_token)
        action_token = await self._session.scalar(
            select(AuthActionToken)
            .where(
                AuthActionToken.token_hash == token_hash,
                AuthActionToken.token_type == token_type,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            action_token is None
            or action_token.used_at is not None
            or ensure_utc(action_token.expires_at) <= now
        ):
            await self._session.rollback()
            raise AppError(
                code="VALIDATION_ERROR",
                message=expired_or_used_message,
                status_code=400,
            )

        user = await self._session.scalar(
            select(User)
            .where(User.id == action_token.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if user is None or user.status != "active":
            await self._session.rollback()
            raise AppError(
                code="VALIDATION_ERROR",
                message=invalid_user_message,
                status_code=400,
            )

        consumed_id = await self._session.scalar(
            update(AuthActionToken)
            .where(
                AuthActionToken.id == action_token.id,
                AuthActionToken.used_at.is_(None),
                AuthActionToken.expires_at > now,
            )
            .values(used_at=now)
            .returning(AuthActionToken.id)
            .execution_options(synchronize_session=False)
        )
        if consumed_id is None:
            await self._session.rollback()
            raise AppError(
                code="VALIDATION_ERROR",
                message=expired_or_used_message,
                status_code=400,
            )
        return user

    async def verify_email(self, *, token: str) -> UserProfileResponse:
        """Consume single-use email verification token and mark user verified."""
        now = datetime.now(UTC)
        user = await self._lock_and_consume_action_token(
            raw_token=token,
            token_type="email_verification",
            now=now,
            expired_or_used_message="Token verifikasi email tidak valid atau telah kedaluwarsa.",
            invalid_user_message="Token verifikasi email tidak valid.",
        )
        if user.email_verified_at is None:
            user.email_verified_at = now
        await self._session.commit()
        await self._session.refresh(user)
        return to_user_profile(user)

    async def forgot_password(self, *, email: str) -> None:
        """Initiate password reset without disclosing whether the email exists."""
        normalized = normalize_email(email)
        user = await self._session.scalar(select(User).where(User.email == normalized))
        if user is None or user.status != "active":
            return

        reset_token = await self._create_action_token(
            user_id=user.id,
            token_type="password_reset",
            expires_delta=timedelta(minutes=self._settings.password_reset_expire_minutes),
        )
        await self._session.commit()
        await self._email_service.send_password_reset_email(
            to_email=user.email,
            token=reset_token,
        )

    async def reset_password(self, *, token: str, new_password: str) -> None:
        """Validate reset token, update Argon2id password hash, and revoke active sessions."""
        now = datetime.now(UTC)
        user = await self._lock_and_consume_action_token(
            raw_token=token,
            token_type="password_reset",
            now=now,
            expired_or_used_message=(
                "Token pengaturan ulang kata sandi tidak valid atau telah kedaluwarsa."
            ),
            invalid_user_message="Token pengaturan ulang kata sandi tidak valid.",
        )
        user.password_hash = hash_password(new_password)
        await self._revoke_all_user_sessions(user.id, now)
        await self._session.commit()
