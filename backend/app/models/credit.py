"""Credit wallet, append-only ledger, reservation, and package ORM models (Milestone M4)."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.auth import utc_now


class CreditWallet(Base):
    """Workspace credit balance summary synchronized with ledger and reservations."""

    __tablename__ = "credit_wallets"
    __table_args__ = (
        CheckConstraint("available_balance >= 0", name="available_balance_non_negative"),
        CheckConstraint("reserved_balance >= 0", name="reserved_balance_non_negative"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    available_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reserved_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    transactions: Mapped[list["CreditTransaction"]] = relationship(
        back_populates="wallet", cascade="all, delete-orphan"
    )
    reservations: Mapped[list["CreditReservation"]] = relationship(
        back_populates="wallet", cascade="all, delete-orphan"
    )


class CreditTransaction(Base):
    """Append-only credit ledger entry."""

    __tablename__ = "credit_transactions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_credit_transactions_workspace_idempotency",
        ),
        CheckConstraint(
            "transaction_type IN ('purchase', 'grant', 'commit', 'release', "
            "'refund', 'adjustment', 'expiry')",
            name="transaction_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    wallet_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("credit_wallets.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    transaction_type: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    wallet: Mapped[CreditWallet] = relationship(back_populates="transactions")


class CreditReservation(Base):
    """Idempotent credit hold bound to an asynchronous job lifecycle."""

    __tablename__ = "credit_reservations"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "idempotency_key",
            name="uq_credit_reservations_workspace_idempotency",
        ),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "status IN ('reserved', 'committed', 'released', 'expired')",
            name="status",
        ),
        Index("ix_credit_reservations_status_expires", "status", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    wallet_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("credit_wallets.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    job_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        index=True,
        nullable=True,
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    wallet: Mapped[CreditWallet] = relationship(back_populates="reservations")


class CreditPackage(Base):
    """Authoritative backend catalog of credit packages (tariffs remain TBD)."""

    __tablename__ = "credit_packages"
    __table_args__ = (
        CheckConstraint("credits > 0", name="credits_positive"),
        CheckConstraint("price_minor_units >= 0", name="price_non_negative"),
        CheckConstraint(
            "workspace_type IN ('personal', 'organization', 'all')",
            name="workspace_type",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    price_minor_units: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(16), default="IDR", nullable=False)
    workspace_type: Mapped[str] = mapped_column(String(32), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

