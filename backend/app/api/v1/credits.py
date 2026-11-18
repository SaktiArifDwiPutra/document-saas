"""Credit wallet, ledger history, and active package catalog endpoints (Milestone M4)."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.v1.deps import get_credit_service, get_current_user
from app.core.errors import ApiResponse
from app.models.auth import User
from app.schemas.credit import (
    CreditPackageListResponse,
    CreditTransactionListResponse,
    CreditWalletResponse,
)
from app.services.credit import CreditService

credits_router = APIRouter(tags=["credits"])


@credits_router.get(
    "/workspaces/{workspace_id}/credits",
    response_model=ApiResponse[CreditWalletResponse],
)
async def get_workspace_credits(
    workspace_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    credit_service: Annotated[CreditService, Depends(get_credit_service)],
) -> ApiResponse[CreditWalletResponse]:
    """Return credit wallet summary for an authorized workspace member."""
    data = await credit_service.get_wallet_summary(current_user, workspace_id=workspace_id)
    return ApiResponse(data=data)


@credits_router.get(
    "/workspaces/{workspace_id}/credits/transactions",
    response_model=ApiResponse[CreditTransactionListResponse],
)
async def list_workspace_credit_transactions(
    workspace_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    credit_service: Annotated[CreditService, Depends(get_credit_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ApiResponse[CreditTransactionListResponse]:
    """Return paginated append-only credit ledger transactions for an authorized member."""
    data = await credit_service.list_transactions(
        current_user,
        workspace_id=workspace_id,
        page=page,
        page_size=page_size,
    )
    return ApiResponse(data=data)


@credits_router.get(
    "/credit-packages",
    response_model=ApiResponse[CreditPackageListResponse],
)
async def list_credit_packages(
    credit_service: Annotated[CreditService, Depends(get_credit_service)],
    workspace_type: Annotated[Literal["personal", "organization"] | None, Query()] = None,
) -> ApiResponse[CreditPackageListResponse]:
    """Return active backend credit packages (live payments remain disabled)."""
    data = await credit_service.list_active_packages(workspace_type=workspace_type)
    return ApiResponse(data=data)

