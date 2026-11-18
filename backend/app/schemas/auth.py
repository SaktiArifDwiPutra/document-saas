"""Authentication request and response contracts (Milestone M2)."""

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_and_normalize_email(value: str) -> str:
    """Trim, lowercase, and validate basic email format."""
    normalized = value.strip().lower()
    if len(normalized) > 320 or not _EMAIL_PATTERN.match(normalized):
        raise ValueError("Format email tidak valid.")
    return normalized


class RegisterRequest(BaseModel):
    """Payload for POST /api/v1/auth/register."""

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=120)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return validate_and_normalize_email(value)

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Nama tampilan wajib diisi.")
        return stripped


class LoginRequest(BaseModel):
    """Payload for POST /api/v1/auth/login."""

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return validate_and_normalize_email(value)


class VerifyEmailRequest(BaseModel):
    """Payload for POST /api/v1/auth/verify-email."""

    token: str = Field(min_length=1, max_length=512)

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Token verifikasi wajib diisi.")
        return stripped


class ForgotPasswordRequest(BaseModel):
    """Payload for POST /api/v1/auth/forgot-password."""

    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return validate_and_normalize_email(value)


class ResetPasswordRequest(BaseModel):
    """Payload for POST /api/v1/auth/reset-password."""

    token: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Token pengaturan ulang wajib diisi.")
        return stripped


class UserProfileResponse(BaseModel):
    """Safe public representation of an authenticated user (never exposes password_hash)."""

    id: UUID
    email: str
    display_name: str
    email_verified: bool
    status: str
    created_at: datetime


class AuthTokenResponse(BaseModel):
    """Bearer access token response paired with HttpOnly refresh cookie and CSRF token."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    csrf_token: str
    user: UserProfileResponse


class MessageResponse(BaseModel):
    """Generic safe status message response."""

    message: str
