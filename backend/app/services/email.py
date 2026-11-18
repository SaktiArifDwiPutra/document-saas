"""Email provider interface and local development stub."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol

EmailTemplate = Literal["verify_email", "password_reset"]


@dataclass(frozen=True)
class OutboxEmail:
    """Captured email record in the local stub outbox."""

    to_email: str
    template: EmailTemplate
    token: str
    sent_at: datetime


class EmailService(Protocol):
    """Protocol for transactional email delivery."""

    async def send_verification_email(self, *, to_email: str, token: str) -> None:
        """Send an email verification link/token."""
        ...

    async def send_password_reset_email(self, *, to_email: str, token: str) -> None:
        """Send a password reset link/token."""
        ...


@dataclass
class StubEmailService:
    """In-memory stub email service for local development and tests.

    Stores messages in memory without writing tokens or user emails to logs.
    """

    sent_messages: list[OutboxEmail] = field(default_factory=list)

    async def send_verification_email(self, *, to_email: str, token: str) -> None:
        self.sent_messages.append(
            OutboxEmail(
                to_email=to_email,
                template="verify_email",
                token=token,
                sent_at=datetime.now(UTC),
            )
        )

    async def send_password_reset_email(self, *, to_email: str, token: str) -> None:
        self.sent_messages.append(
            OutboxEmail(
                to_email=to_email,
                template="password_reset",
                token=token,
                sent_at=datetime.now(UTC),
            )
        )
