"""Centralized workspace RBAC and Organization Document Permission Policy (Milestone M3)."""

from typing import Literal
from uuid import UUID

from app.core.errors import AppError

WorkspaceType = Literal["personal", "organization"]
WorkspaceRole = Literal["owner", "admin", "editor", "member"]
AssignableMemberRole = Literal["admin", "editor", "member"]
WorkspaceAction = Literal[
    "view",
    "update_settings",
    "delete_workspace",
    "transfer_ownership",
    "view_members",
    "manage_invitations",
    "manage_members",
    "manage_templates",
    "create_document",
]
DocumentAction = Literal[
    "view",
    "edit",
    "submit_source",
    "process",
    "approve",
    "generate",
    "delete",
]

_ORG_ONLY_WORKSPACE_ACTIONS: frozenset[WorkspaceAction] = frozenset(
    {
        "delete_workspace",
        "transfer_ownership",
        "manage_invitations",
        "manage_members",
        "manage_templates",
    }
)

_COMMON_READ_ACTIONS: frozenset[WorkspaceAction] = frozenset(
    {"view", "view_members", "create_document"}
)


def can_perform_workspace_action(
    *,
    role: WorkspaceRole,
    workspace_type: WorkspaceType,
    action: WorkspaceAction,
) -> bool:
    """Return True when role is permitted to perform action in workspace_type."""
    if action in _COMMON_READ_ACTIONS:
        return True

    if workspace_type == "personal":
        if action == "update_settings":
            return role == "owner"
        return False

    if action == "update_settings":
        return role in ("owner", "admin")
    if action in ("delete_workspace", "transfer_ownership"):
        return role == "owner"
    if action in ("manage_invitations", "manage_members"):
        return role in ("owner", "admin")
    if action == "manage_templates":
        return role in ("owner", "admin", "editor")
    return False


def require_workspace_action(
    *,
    role: WorkspaceRole,
    workspace_type: WorkspaceType,
    action: WorkspaceAction,
) -> None:
    """Enforce workspace action permissions, raising 400 for personal-only limits or 403."""
    if workspace_type == "personal" and action in _ORG_ONLY_WORKSPACE_ACTIONS:
        raise AppError(
            code="VALIDATION_ERROR",
            message="Operasi ini tidak didukung pada Workspace Pribadi.",
            status_code=400,
        )
    if not can_perform_workspace_action(
        role=role, workspace_type=workspace_type, action=action
    ):
        raise AppError(
            code="FORBIDDEN",
            message="Anda tidak memiliki izin untuk melakukan tindakan ini di workspace.",
            status_code=403,
        )


def can_access_document(
    *,
    role: WorkspaceRole,
    workspace_type: WorkspaceType,
    actor_user_id: UUID,
    document_owner_user_id: UUID,
    action: DocumentAction,
) -> bool:
    """Evaluate the Organization Document Permission Policy cleanly in one place."""
    if workspace_type == "personal":
        return role == "owner" and actor_user_id == document_owner_user_id

    if role in ("owner", "admin"):
        return True
    if role == "editor":
        if actor_user_id == document_owner_user_id:
            return True
        return action == "view"
    if role == "member":
        return actor_user_id == document_owner_user_id
    return False


def authorize_document_access(
    *,
    role: WorkspaceRole,
    workspace_type: WorkspaceType,
    actor_user_id: UUID,
    document_owner_user_id: UUID,
    action: DocumentAction,
) -> None:
    """Raise 403 FORBIDDEN when document access is denied by RBAC policy."""
    if not can_access_document(
        role=role,
        workspace_type=workspace_type,
        actor_user_id=actor_user_id,
        document_owner_user_id=document_owner_user_id,
        action=action,
    ):
        raise AppError(
            code="FORBIDDEN",
            message="Anda tidak memiliki izin untuk mengakses atau mengubah dokumen ini.",
            status_code=403,
        )


def _authorize_member_mutation(
    *,
    actor_role: WorkspaceRole,
    target_current_role: WorkspaceRole,
    owner_protected_message: str,
) -> None:
    if actor_role not in ("owner", "admin"):
        raise AppError(
            code="FORBIDDEN",
            message="Anda tidak memiliki izin untuk mengelola anggota workspace.",
            status_code=403,
        )
    if target_current_role == "owner":
        raise AppError(
            code="FORBIDDEN",
            message=owner_protected_message,
            status_code=403,
        )


def authorize_member_role_change(
    *,
    actor_role: WorkspaceRole,
    target_current_role: WorkspaceRole,
) -> None:
    """Ensure only Owner/Admin can change member roles and Owner role cannot be altered here."""
    _authorize_member_mutation(
        actor_role=actor_role,
        target_current_role=target_current_role,
        owner_protected_message="Peran pemilik workspace tidak dapat diubah secara langsung.",
    )


def authorize_member_removal(
    *,
    actor_role: WorkspaceRole,
    target_current_role: WorkspaceRole,
) -> None:
    """Ensure only Owner/Admin can remove members and Owner cannot be removed."""
    _authorize_member_mutation(
        actor_role=actor_role,
        target_current_role=target_current_role,
        owner_protected_message="Pemilik workspace tidak dapat dihapus dari workspace.",
    )

