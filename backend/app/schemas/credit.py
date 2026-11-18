"""Credit wallet, ledger, reservation, and package schemas (Milestone M4)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

TransactionType = Literal[
    "purchase",
    "grant",
    "commit",
    "release",
    "refund",
    "adjustment",
    "expiry",
]
ReservationStatus = Literal["reserved", "committed", "released", "expired"]


class CreditWalletResponse(BaseModel):
    """Workspace credit wallet summary."""

    id: UUID
    workspace_id: UUID
    available_balance: int
    reserved_balance: int
    total_balance: int
    updated_at: datetime


class CreditTransactionResponse(BaseModel):
    """Append-only credit ledger transaction item."""

    id: UUID
    wallet_id: UUID
    workspace_id: UUID
    transaction_type: TransactionType
    amount: int
    reference_type: str | None
    reference_id: str | None
    idempotency_key: str
    description: str
    created_by_user_id: UUID | None
    created_at: datetime


class CreditTransactionListResponse(BaseModel):
    """Paginated credit ledger transaction history."""

    items: list[CreditTransactionResponse]
    page: int
    page_size: int
    total: int


class CreditReservationResponse(BaseModel):
    """Credit reservation state representation."""

    id: UUID
    wallet_id: UUID
    workspace_id: UUID
    job_id: UUID | None
    amount: int
    status: ReservationStatus
    expires_at: datetime
    idempotency_key: str
    created_at: datetime
    resolved_at: datetime | None


class CreditPackageResponse(BaseModel):
    """Active credit package catalog item."""

    id: UUID
    name: str
    credits: int
    price_minor_units: int
    currency: str
    workspace_type: str
    is_active: bool


class CreditPackageListResponse(BaseModel):
    """Active credit package catalog list."""

    items: list[CreditPackageResponse]

