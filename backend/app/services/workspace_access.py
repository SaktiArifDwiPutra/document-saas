"""Shared workspace membership verification and audit helpers."""

from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.workspace import AuditLog, Workspace, WorkspaceMember


def record_workspace_audit(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    actor_user_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: str,
    metadata: Mapping[str, object] | None = None,
) -> None:
    """Append a non-sensitive workspace audit event to the current DB transaction."""
    session.add(
        AuditLog(
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata_json=dict(metadata) if metadata else {},
        )
    )


async def require_active_workspace_membership(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    user_id: UUID,
    lock_workspace: bool = False,
) -> tuple[Workspace, WorkspaceMember]:
    """Return active Workspace and WorkspaceMember for user, raising 403 otherwise."""
    workspace = await session.scalar(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.status == "active",
            Workspace.deleted_at.is_(None),
        )
    )
    member = await session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
            WorkspaceMember.status == "active",
        )
    )
    if workspace is None or member is None:
        raise AppError(
            code="FORBIDDEN",
            message="Anda tidak memiliki akses ke workspace ini.",
            status_code=403,
        )

    if lock_workspace:
        locked_workspace = await session.scalar(
            select(Workspace)
            .where(
                Workspace.id == workspace_id,
                Workspace.status == "active",
                Workspace.deleted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        locked_member = await session.scalar(
            select(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
                WorkspaceMember.status == "active",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked_workspace is None or locked_member is None:
            await session.rollback()
            raise AppError(
                code="FORBIDDEN",
                message="Anda tidak memiliki akses ke workspace ini.",
                status_code=403,
            )
        return locked_workspace, locked_member

    return workspace, member

