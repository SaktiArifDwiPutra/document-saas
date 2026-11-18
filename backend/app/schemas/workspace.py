"""Workspace, membership, and invitation request/response schemas (Milestone M3)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.core.permissions import AssignableMemberRole, WorkspaceRole, WorkspaceType
from app.schemas.auth import validate_and_normalize_email


def _validate_non_empty_name(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("Nama workspace wajib diisi.")
    return stripped


class CreateWorkspaceRequest(BaseModel):
    """Payload for POST /api/v1/workspaces."""

    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_non_empty_name(value)


class UpdateWorkspaceRequest(BaseModel):
    """Payload for PATCH /api/v1/workspaces/{workspace_id}."""

    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _validate_non_empty_name(value)


class TransferOwnershipRequest(BaseModel):
    """Payload for POST /api/v1/workspaces/{workspace_id}/transfer-ownership."""

    new_owner_user_id: UUID


class WorkspaceResponse(BaseModel):
    """Workspace detail representation including caller's effective role."""

    id: UUID
    name: str
    type: WorkspaceType
    owner_user_id: UUID
    status: str
    my_role: WorkspaceRole
    created_at: datetime
    updated_at: datetime


class WorkspaceListResponse(BaseModel):
    """Paginated list of accessible workspaces."""

    items: list[WorkspaceResponse]
    page: int
    page_size: int
    total: int


class WorkspaceMemberResponse(BaseModel):
    """Active workspace membership summary."""

    user_id: UUID
    email: str
    display_name: str
    role: WorkspaceRole
    status: str
    joined_at: datetime


class WorkspaceMemberListResponse(BaseModel):
    """Paginated list of active workspace members."""

    items: list[WorkspaceMemberResponse]
    page: int
    page_size: int
    total: int


class UpdateMemberRoleRequest(BaseModel):
    """Payload for PATCH /api/v1/workspaces/{workspace_id}/members/{user_id}."""

    role: AssignableMemberRole


class CreateInvitationRequest(BaseModel):
    """Payload for POST /api/v1/workspaces/{workspace_id}/invitations."""

    email: str = Field(min_length=3, max_length=320)
    role: AssignableMemberRole

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        return validate_and_normalize_email(value)


class InvitationResponse(BaseModel):
    """Workspace invitation record without exposing raw secret token."""

    id: UUID
    workspace_id: UUID
    email_normalized: str
    role: AssignableMemberRole
    expires_at: datetime
    accepted_at: datetime | None
    cancelled_at: datetime | None
    created_by_user_id: UUID
    created_at: datetime


class InvitationListResponse(BaseModel):
    """Paginated list of workspace invitations."""

    items: list[InvitationResponse]
    page: int
    page_size: int
    total: int


class AcceptInvitationRequest(BaseModel):
    """Payload for POST /api/v1/invitations/accept."""

    token: str = Field(min_length=1, max_length=512)

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Token undangan wajib diisi.")
        return stripped

