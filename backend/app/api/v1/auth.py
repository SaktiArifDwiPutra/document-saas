"""Authentication endpoints under /api/v1/auth."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.api.v1.deps import (
    get_auth_service,
    get_client_ip,
    get_current_user,
    get_rate_limiter,
)
from app.core.config import Settings, get_settings
from app.core.errors import ApiErrorResponse, ApiResponse, AppError, ErrorDetail
from app.core.rate_limit import SlidingWindowRateLimiter, account_rate_limit_key
from app.core.security import (
    CSRF_COOKIE_NAME,
    REFRESH_COOKIE_NAME,
    normalize_email,
    verify_csrf_and_origin,
)
from app.models.auth import User
from app.schemas.auth import (
    AuthTokenResponse,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResetPasswordRequest,
    UserProfileResponse,
    VerifyEmailRequest,
)
from app.services.auth import AuthService, to_user_profile

router = APIRouter(prefix="/auth", tags=["auth"])

_CSRF_COOKIE_PATH = "/"


def _auth_cookie_path(settings: Settings) -> str:
    return f"{settings.api_v1_prefix}/auth"


def _set_session_cookies(
    *,
    response: Response,
    settings: Settings,
    raw_refresh_token: str,
    csrf_token: str,
) -> None:
    max_age = settings.refresh_token_expire_days * 86400
    refresh_cookie_path = _auth_cookie_path(settings)
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=raw_refresh_token,
        max_age=max_age,
        path=refresh_cookie_path,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        max_age=max_age,
        path=_CSRF_COOKIE_PATH,
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )


def _clear_session_cookies(*, response: Response, settings: Settings) -> None:
    refresh_cookie_path = _auth_cookie_path(settings)
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        path=refresh_cookie_path,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )
    response.delete_cookie(
        key=CSRF_COOKIE_NAME,
        path=_CSRF_COOKIE_PATH,
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,
    )


@router.post("/register", response_model=ApiResponse[AuthTokenResponse], status_code=201)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    rate_limiter: Annotated[SlidingWindowRateLimiter, Depends(get_rate_limiter)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ApiResponse[AuthTokenResponse]:
    """Register user account (Personal Workspace deferred to M3)."""
    client_ip = get_client_ip(request)
    rate_limiter.check(
        f"register:ip:{client_ip}",
        max_requests=settings.rate_limit_register_ip_max,
        window_seconds=settings.rate_limit_register_ip_window_seconds,
        scope="register:ip",
    )

    token_response, raw_refresh_token = await auth_service.register(
        email=payload.email,
        password=payload.password,
        display_name=payload.display_name,
        ip_address=client_ip,
        user_agent=request.headers.get("user-agent"),
    )
    _set_session_cookies(
        response=response,
        settings=settings,
        raw_refresh_token=raw_refresh_token,
        csrf_token=token_response.csrf_token,
    )
    return ApiResponse(data=token_response)


@router.post("/login", response_model=ApiResponse[AuthTokenResponse])
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    rate_limiter: Annotated[SlidingWindowRateLimiter, Depends(get_rate_limiter)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ApiResponse[AuthTokenResponse]:
    """Authenticate user credentials and issue access JWT + HttpOnly refresh cookie."""
    client_ip = get_client_ip(request)
    rate_limiter.check(
        f"login:ip:{client_ip}",
        max_requests=settings.rate_limit_login_ip_max,
        window_seconds=settings.rate_limit_login_ip_window_seconds,
        scope="login:ip",
    )
    rate_limiter.check(
        account_rate_limit_key("login", payload.email),
        max_requests=settings.rate_limit_login_account_max,
        window_seconds=settings.rate_limit_login_account_window_seconds,
        scope="login:acct",
    )

    token_response, raw_refresh_token = await auth_service.login(
        email=payload.email,
        password=payload.password,
        ip_address=client_ip,
        user_agent=request.headers.get("user-agent"),
    )
    _set_session_cookies(
        response=response,
        settings=settings,
        raw_refresh_token=raw_refresh_token,
        csrf_token=token_response.csrf_token,
    )
    return ApiResponse(data=token_response)


@router.post("/refresh", response_model=ApiResponse[AuthTokenResponse])
async def refresh(
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ApiResponse[AuthTokenResponse] | JSONResponse:
    """Rotate refresh token cookie after verifying origin and double-submit CSRF token."""
    verify_csrf_and_origin(request, settings)
    raw_refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)

    try:
        token_response, new_raw_refresh_token = await auth_service.refresh(
            raw_refresh_token=raw_refresh_token,
            ip_address=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    except AppError as exc:
        if exc.status_code == 401:
            error_body = ApiErrorResponse(
                error=ErrorDetail(code=exc.code, message=exc.message, details=exc.details)
            ).model_dump(mode="json")
            error_response = JSONResponse(status_code=401, content=error_body)
            _clear_session_cookies(response=error_response, settings=settings)
            return error_response
        raise

    _set_session_cookies(
        response=response,
        settings=settings,
        raw_refresh_token=new_raw_refresh_token,
        csrf_token=token_response.csrf_token,
    )
    return ApiResponse(data=token_response)


@router.post("/logout", response_model=ApiResponse[MessageResponse])
async def logout(
    request: Request,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ApiResponse[MessageResponse]:
    """Revoke refresh session and clear authentication cookies."""
    verify_csrf_and_origin(request, settings)
    raw_refresh_token = request.cookies.get(REFRESH_COOKIE_NAME)
    await auth_service.logout(raw_refresh_token=raw_refresh_token)
    _clear_session_cookies(response=response, settings=settings)
    return ApiResponse(data=MessageResponse(message="Berhasil keluar dari sesi."))


@router.get("/me", response_model=ApiResponse[UserProfileResponse])
async def me(
    current_user: Annotated[User, Depends(get_current_user)],
) -> ApiResponse[UserProfileResponse]:
    """Return authenticated user profile."""
    return ApiResponse(data=to_user_profile(current_user))


@router.post("/verify-email", response_model=ApiResponse[UserProfileResponse])
async def verify_email(
    payload: VerifyEmailRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> ApiResponse[UserProfileResponse]:
    """Verify email address using single-use token."""
    profile = await auth_service.verify_email(token=payload.token)
    return ApiResponse(data=profile)


@router.post("/forgot-password", response_model=ApiResponse[MessageResponse])
async def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    rate_limiter: Annotated[SlidingWindowRateLimiter, Depends(get_rate_limiter)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ApiResponse[MessageResponse]:
    """Request password reset email with generic enumeration-safe response."""
    client_ip = get_client_ip(request)
    normalized = normalize_email(payload.email)
    rate_limiter.check(
        f"forgot:ip:{client_ip}",
        max_requests=settings.rate_limit_forgot_ip_max,
        window_seconds=settings.rate_limit_forgot_ip_window_seconds,
        scope="forgot:ip",
    )
    rate_limiter.check(
        account_rate_limit_key("forgot", normalized),
        max_requests=settings.rate_limit_forgot_account_max,
        window_seconds=settings.rate_limit_forgot_account_window_seconds,
        scope="forgot:acct",
    )
    await auth_service.forgot_password(email=normalized)
    return ApiResponse(
        data=MessageResponse(
            message="Jika email terdaftar, tautan pengaturan ulang kata sandi telah dikirim."
        )
    )


@router.post("/reset-password", response_model=ApiResponse[MessageResponse])
async def reset_password(
    payload: ResetPasswordRequest,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> ApiResponse[MessageResponse]:
    """Reset user password with single-use token and revoke active sessions."""
    await auth_service.reset_password(token=payload.token, new_password=payload.new_password)
    return ApiResponse(data=MessageResponse(message="Kata sandi berhasil diperbarui."))
