"""Workspace, RBAC membership, and invitation endpoints under /api/v1."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.v1.deps import get_current_user, get_workspace_service
from app.core.errors import ApiResponse
from app.models.auth import User
from app.schemas.auth import MessageResponse
from app.schemas.workspace import (
    AcceptInvitationRequest,
    CreateInvitationRequest,
    CreateWorkspaceRequest,
    InvitationListResponse,
    InvitationResponse,
    TransferOwnershipRequest,
    UpdateMemberRoleRequest,
    UpdateWorkspaceRequest,
    WorkspaceListResponse,
    WorkspaceMemberListResponse,
    WorkspaceMemberResponse,
    WorkspaceResponse,
)
from app.services.workspace import WorkspaceService

workspaces_router = APIRouter(prefix="/workspaces", tags=["workspaces"])
invitations_router = APIRouter(prefix="/invitations", tags=["invitations"])


@workspaces_router.get("", response_model=ApiResponse[WorkspaceListResponse])
async def list_workspaces(
    current_user: Annotated[User, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ApiResponse[WorkspaceListResponse]:
    """List workspaces accessible to the authenticated user."""
    data = await workspace_service.list_workspaces(current_user, page=page, page_size=page_size)
    return ApiResponse(data=data)


@workspaces_router.post("", response_model=ApiResponse[WorkspaceResponse], status_code=201)
async def create_workspace(
    payload: CreateWorkspaceRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> ApiResponse[WorkspaceResponse]:
    """Create an organization workspace owned by the authenticated user."""
    data = await workspace_service.create_organization_workspace(current_user, name=payload.name)
    return ApiResponse(data=data)


@workspaces_router.get("/{workspace_id}", response_model=ApiResponse[WorkspaceResponse])
async def get_workspace(
    workspace_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> ApiResponse[WorkspaceResponse]:
    """Retrieve workspace details for an active member."""
    data = await workspace_service.get_workspace(current_user, workspace_id=workspace_id)
    return ApiResponse(data=data)


@workspaces_router.patch("/{workspace_id}", response_model=ApiResponse[WorkspaceResponse])
async def update_workspace(
    workspace_id: UUID,
    payload: UpdateWorkspaceRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> ApiResponse[WorkspaceResponse]:
    """Update workspace settings under RBAC policy."""
    data = await workspace_service.update_workspace(
        current_user, workspace_id=workspace_id, name=payload.name
    )
    return ApiResponse(data=data)


@workspaces_router.delete("/{workspace_id}", response_model=ApiResponse[MessageResponse])
async def delete_workspace(
    workspace_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> ApiResponse[MessageResponse]:
    """Delete/archive an organization workspace under owner policy."""
    await workspace_service.delete_workspace(current_user, workspace_id=workspace_id)
    return ApiResponse(data=MessageResponse(message="Workspace berhasil dihapus."))


@workspaces_router.post(
    "/{workspace_id}/transfer-ownership",
    response_model=ApiResponse[WorkspaceResponse],
)
async def transfer_ownership(
    workspace_id: UUID,
    payload: TransferOwnershipRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> ApiResponse[WorkspaceResponse]:
    """Transfer organization workspace ownership to another active member."""
    data = await workspace_service.transfer_ownership(
        current_user,
        workspace_id=workspace_id,
        new_owner_user_id=payload.new_owner_user_id,
    )
    return ApiResponse(data=data)


@workspaces_router.get(
    "/{workspace_id}/members",
    response_model=ApiResponse[WorkspaceMemberListResponse],
)
async def list_members(
    workspace_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ApiResponse[WorkspaceMemberListResponse]:
    """List active members in the workspace."""
    data = await workspace_service.list_members(
        current_user, workspace_id=workspace_id, page=page, page_size=page_size
    )
    return ApiResponse(data=data)


@workspaces_router.post(
    "/{workspace_id}/invitations",
    response_model=ApiResponse[InvitationResponse],
    status_code=201,
)
async def create_invitation(
    workspace_id: UUID,
    payload: CreateInvitationRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> ApiResponse[InvitationResponse]:
    """Create a single-use organization workspace invitation."""
    data = await workspace_service.create_invitation(
        current_user,
        workspace_id=workspace_id,
        email=payload.email,
        role=payload.role,
    )
    return ApiResponse(data=data)


@workspaces_router.get(
    "/{workspace_id}/invitations",
    response_model=ApiResponse[InvitationListResponse],
)
async def list_invitations(
    workspace_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ApiResponse[InvitationListResponse]:
    """List invitations for an organization workspace."""
    data = await workspace_service.list_invitations(
        current_user, workspace_id=workspace_id, page=page, page_size=page_size
    )
    return ApiResponse(data=data)


@workspaces_router.delete(
    "/{workspace_id}/invitations/{invitation_id}",
    response_model=ApiResponse[MessageResponse],
)
async def cancel_invitation(
    workspace_id: UUID,
    invitation_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> ApiResponse[MessageResponse]:
    """Cancel a pending invitation."""
    await workspace_service.cancel_invitation(
        current_user, workspace_id=workspace_id, invitation_id=invitation_id
    )
    return ApiResponse(data=MessageResponse(message="Undangan berhasil dibatalkan."))


@workspaces_router.patch(
    "/{workspace_id}/members/{user_id}",
    response_model=ApiResponse[WorkspaceMemberResponse],
)
async def update_member_role(
    workspace_id: UUID,
    user_id: UUID,
    payload: UpdateMemberRoleRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> ApiResponse[WorkspaceMemberResponse]:
    """Change a member's role within RBAC policy."""
    data = await workspace_service.change_member_role(
        current_user,
        workspace_id=workspace_id,
        target_user_id=user_id,
        new_role=payload.role,
    )
    return ApiResponse(data=data)


@workspaces_router.delete(
    "/{workspace_id}/members/{user_id}",
    response_model=ApiResponse[MessageResponse],
)
async def remove_member(
    workspace_id: UUID,
    user_id: UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> ApiResponse[MessageResponse]:
    """Remove a member from the workspace without deleting their documents."""
    await workspace_service.remove_member(
        current_user, workspace_id=workspace_id, target_user_id=user_id
    )
    return ApiResponse(data=MessageResponse(message="Anggota berhasil dihapus dari workspace."))


@invitations_router.post("/accept", response_model=ApiResponse[WorkspaceResponse])
async def accept_invitation(
    payload: AcceptInvitationRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
) -> ApiResponse[WorkspaceResponse]:
    """Accept a workspace invitation using its single-use token."""
    data = await workspace_service.accept_invitation(current_user, raw_token=payload.token)
    return ApiResponse(data=data)
