"""Workspace, RBAC membership, invitation, and audit logging service (Milestone M3)."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.core.permissions import (
    AssignableMemberRole,
    WorkspaceRole,
    WorkspaceType,
    authorize_member_removal,
    authorize_member_role_change,
    require_workspace_action,
)
from app.core.security import (
    ensure_utc,
    generate_opaque_token,
    hash_opaque_token,
    normalize_email,
)
from app.models.auth import User
from app.models.workspace import Invitation, Workspace, WorkspaceMember
from app.schemas.workspace import (
    InvitationListResponse,
    InvitationResponse,
    WorkspaceListResponse,
    WorkspaceMemberListResponse,
    WorkspaceMemberResponse,
    WorkspaceResponse,
)
from app.services.credit import CreditService
from app.services.email import EmailService
from app.services.workspace_access import (
    record_workspace_audit,
    require_active_workspace_membership,
)


def _to_workspace_response(workspace: Workspace, member: WorkspaceMember) -> WorkspaceResponse:
    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        type=cast(WorkspaceType, workspace.type),
        owner_user_id=workspace.owner_user_id,
        status=workspace.status,
        my_role=cast(WorkspaceRole, member.role),
        created_at=ensure_utc(workspace.created_at),
        updated_at=ensure_utc(workspace.updated_at),
    )


def _to_member_response(member: WorkspaceMember, user: User) -> WorkspaceMemberResponse:
    return WorkspaceMemberResponse(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=cast(WorkspaceRole, member.role),
        status=member.status,
        joined_at=ensure_utc(member.joined_at),
    )


def _to_invitation_response(invitation: Invitation) -> InvitationResponse:
    return InvitationResponse(
        id=invitation.id,
        workspace_id=invitation.workspace_id,
        email_normalized=invitation.email_normalized,
        role=cast(AssignableMemberRole, invitation.role),
        expires_at=ensure_utc(invitation.expires_at),
        accepted_at=ensure_utc(invitation.accepted_at) if invitation.accepted_at else None,
        cancelled_at=ensure_utc(invitation.cancelled_at) if invitation.cancelled_at else None,
        created_by_user_id=invitation.created_by_user_id,
        created_at=ensure_utc(invitation.created_at),
    )


class WorkspaceService:
    """Owns workspace lifecycle, RBAC membership management, and invitations."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        email_service: EmailService,
    ) -> None:
        self._session = session
        self._settings = settings
        self._email_service = email_service

    def _record_audit(
        self,
        *,
        workspace_id: UUID,
        actor_user_id: UUID | None,
        action: str,
        resource_type: str,
        resource_id: str,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        record_workspace_audit(
            self._session,
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata,
        )

    async def _require_active_membership(
        self,
        *,
        workspace_id: UUID,
        user_id: UUID,
        lock_workspace: bool = False,
    ) -> tuple[Workspace, WorkspaceMember]:
        return await require_active_workspace_membership(
            self._session,
            workspace_id=workspace_id,
            user_id=user_id,
            lock_workspace=lock_workspace,
        )

    async def list_workspaces(
        self,
        user: User,
        *,
        page: int,
        page_size: int,
    ) -> WorkspaceListResponse:
        """List active workspaces accessible to the authenticated user."""
        base_filters = (
            WorkspaceMember.user_id == user.id,
            WorkspaceMember.status == "active",
            Workspace.status == "active",
            Workspace.deleted_at.is_(None),
        )
        total = (
            await self._session.scalar(
                select(func.count(Workspace.id))
                .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
                .where(*base_filters)
            )
            or 0
        )
        rows = (
            await self._session.execute(
                select(Workspace, WorkspaceMember)
                .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
                .where(*base_filters)
                .order_by(Workspace.type.desc(), Workspace.created_at.asc(), Workspace.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        items = [_to_workspace_response(ws, m) for ws, m in rows]
        return WorkspaceListResponse(items=items, page=page, page_size=page_size, total=total)

    async def create_organization_workspace(
        self,
        user: User,
        *,
        name: str,
    ) -> WorkspaceResponse:
        """Create a new organization workspace with the creator as Owner."""
        now = datetime.now(UTC)
        workspace = Workspace(
            name=name.strip(),
            type="organization",
            owner_user_id=user.id,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self._session.add(workspace)
        await self._session.flush()

        owner_member = WorkspaceMember(
            workspace_id=workspace.id,
            user_id=user.id,
            role="owner",
            status="active",
            joined_at=now,
            created_at=now,
            updated_at=now,
        )
        self._session.add(owner_member)
        await CreditService(
            session=self._session, settings=self._settings
        ).initialize_workspace_wallet(
            workspace.id,
            created_by_user_id=user.id,
            commit=False,
        )
        self._record_audit(
            workspace_id=workspace.id,
            actor_user_id=user.id,
            action="workspace.created",
            resource_type="workspace",
            resource_id=str(workspace.id),
            metadata={"type": "organization"},
        )
        await self._session.commit()
        return _to_workspace_response(workspace, owner_member)

    async def get_workspace(self, user: User, *, workspace_id: UUID) -> WorkspaceResponse:
        """Return workspace details when user is an active member."""
        workspace, member = await self._require_active_membership(
            workspace_id=workspace_id,
            user_id=user.id,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="view",
        )
        return _to_workspace_response(workspace, member)

    async def update_workspace(
        self,
        user: User,
        *,
        workspace_id: UUID,
        name: str,
    ) -> WorkspaceResponse:
        """Update permitted workspace settings under RBAC policy."""
        workspace, member = await self._require_active_membership(
            workspace_id=workspace_id,
            user_id=user.id,
            lock_workspace=True,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="update_settings",
        )
        workspace.name = name.strip()
        workspace.updated_at = datetime.now(UTC)
        self._record_audit(
            workspace_id=workspace.id,
            actor_user_id=user.id,
            action="workspace.updated",
            resource_type="workspace",
            resource_id=str(workspace.id),
        )
        await self._session.commit()
        return _to_workspace_response(workspace, member)

    async def delete_workspace(self, user: User, *, workspace_id: UUID) -> None:
        """Soft-delete an organization workspace under owner policy."""
        workspace, member = await self._require_active_membership(
            workspace_id=workspace_id,
            user_id=user.id,
            lock_workspace=True,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="delete_workspace",
        )
        now = datetime.now(UTC)
        workspace.status = "archived"
        workspace.deleted_at = now
        workspace.updated_at = now
        await self._session.execute(
            update(Invitation)
            .where(
                Invitation.workspace_id == workspace.id,
                Invitation.accepted_at.is_(None),
                Invitation.cancelled_at.is_(None),
            )
            .values(cancelled_at=now)
        )
        self._record_audit(
            workspace_id=workspace.id,
            actor_user_id=user.id,
            action="workspace.deleted",
            resource_type="workspace",
            resource_id=str(workspace.id),
        )
        await self._session.commit()

    async def transfer_ownership(
        self,
        user: User,
        *,
        workspace_id: UUID,
        new_owner_user_id: UUID,
    ) -> WorkspaceResponse:
        """Transfer organization workspace ownership to another active member."""
        workspace, actor_member = await self._require_active_membership(
            workspace_id=workspace_id,
            user_id=user.id,
            lock_workspace=True,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, actor_member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="transfer_ownership",
        )
        if new_owner_user_id == user.id:
            await self._session.rollback()
            raise AppError(
                code="VALIDATION_ERROR",
                message="Anda sudah menjadi pemilik workspace ini.",
                status_code=400,
            )

        locked_members = (
            await self._session.scalars(
                select(WorkspaceMember)
                .where(
                    WorkspaceMember.workspace_id == workspace.id,
                    WorkspaceMember.user_id.in_([user.id, new_owner_user_id]),
                    WorkspaceMember.status == "active",
                )
                .order_by(WorkspaceMember.user_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
        by_user = {m.user_id: m for m in locked_members}
        target_member = by_user.get(new_owner_user_id)
        current_actor = by_user.get(user.id)
        target_user = await self._session.scalar(
            select(User).where(User.id == new_owner_user_id, User.status == "active")
        )
        if (
            target_member is None
            or current_actor is None
            or current_actor.role != "owner"
            or target_user is None
        ):
            await self._session.rollback()
            raise AppError(
                code="NOT_FOUND",
                message="Anggota tujuan tidak ditemukan di workspace ini.",
                status_code=404,
            )

        now = datetime.now(UTC)
        workspace.owner_user_id = new_owner_user_id
        workspace.updated_at = now
        target_member.role = "owner"
        target_member.updated_at = now
        current_actor.role = "admin"
        current_actor.updated_at = now
        self._record_audit(
            workspace_id=workspace.id,
            actor_user_id=user.id,
            action="workspace.ownership_transferred",
            resource_type="workspace",
            resource_id=str(workspace.id),
            metadata={
                "previous_owner_user_id": str(user.id),
                "new_owner_user_id": str(new_owner_user_id),
            },
        )
        await self._session.commit()
        return _to_workspace_response(workspace, current_actor)

    async def list_members(
        self,
        user: User,
        *,
        workspace_id: UUID,
        page: int,
        page_size: int,
    ) -> WorkspaceMemberListResponse:
        """List active workspace members."""
        workspace, member = await self._require_active_membership(
            workspace_id=workspace_id,
            user_id=user.id,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="view_members",
        )
        filters = (
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.status == "active",
            User.status == "active",
        )
        total = (
            await self._session.scalar(
                select(func.count(WorkspaceMember.id))
                .join(User, User.id == WorkspaceMember.user_id)
                .where(*filters)
            )
            or 0
        )
        rows = (
            await self._session.execute(
                select(WorkspaceMember, User)
                .join(User, User.id == WorkspaceMember.user_id)
                .where(*filters)
                .order_by(WorkspaceMember.joined_at.asc(), WorkspaceMember.user_id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        items = [_to_member_response(m, u) for m, u in rows]
        return WorkspaceMemberListResponse(items=items, page=page, page_size=page_size, total=total)

    async def change_member_role(
        self,
        user: User,
        *,
        workspace_id: UUID,
        target_user_id: UUID,
        new_role: AssignableMemberRole,
    ) -> WorkspaceMemberResponse:
        """Change a non-owner member's role within RBAC policy."""
        workspace, actor_member = await self._require_active_membership(
            workspace_id=workspace_id,
            user_id=user.id,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, actor_member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="manage_members",
        )
        target_member = await self._session.scalar(
            select(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == target_user_id,
                WorkspaceMember.status == "active",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if target_member is None:
            raise AppError(
                code="NOT_FOUND",
                message="Anggota workspace tidak ditemukan.",
                status_code=404,
            )

        authorize_member_role_change(
            actor_role=cast(WorkspaceRole, actor_member.role),
            target_current_role=cast(WorkspaceRole, target_member.role),
        )
        target_user = await self._session.scalar(
            select(User).where(User.id == target_user_id, User.status == "active")
        )
        if target_user is None:
            raise AppError(
                code="NOT_FOUND",
                message="Anggota workspace tidak ditemukan.",
                status_code=404,
            )

        previous_role = target_member.role
        target_member.role = new_role
        target_member.updated_at = datetime.now(UTC)
        self._record_audit(
            workspace_id=workspace.id,
            actor_user_id=user.id,
            action="workspace_member.role_updated",
            resource_type="workspace_member",
            resource_id=str(target_user_id),
            metadata={"previous_role": previous_role, "new_role": new_role},
        )
        await self._session.commit()
        return _to_member_response(target_member, target_user)

    async def remove_member(
        self,
        user: User,
        *,
        workspace_id: UUID,
        target_user_id: UUID,
    ) -> None:
        """Remove a non-owner member from workspace without deleting their documents."""
        workspace, actor_member = await self._require_active_membership(
            workspace_id=workspace_id,
            user_id=user.id,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, actor_member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="manage_members",
        )
        target_member = await self._session.scalar(
            select(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == target_user_id,
                WorkspaceMember.status == "active",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if target_member is None:
            raise AppError(
                code="NOT_FOUND",
                message="Anggota workspace tidak ditemukan.",
                status_code=404,
            )

        authorize_member_removal(
            actor_role=cast(WorkspaceRole, actor_member.role),
            target_current_role=cast(WorkspaceRole, target_member.role),
        )
        target_member.status = "removed"
        target_member.updated_at = datetime.now(UTC)
        self._record_audit(
            workspace_id=workspace.id,
            actor_user_id=user.id,
            action="workspace_member.removed",
            resource_type="workspace_member",
            resource_id=str(target_user_id),
            metadata={"removed_role": target_member.role},
        )
        await self._session.commit()

    async def create_invitation(
        self,
        user: User,
        *,
        workspace_id: UUID,
        email: str,
        role: AssignableMemberRole,
    ) -> InvitationResponse:
        """Create a single-use hashed invitation and dispatch stub email."""
        workspace, actor_member = await self._require_active_membership(
            workspace_id=workspace_id,
            user_id=user.id,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, actor_member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="manage_invitations",
        )

        normalized = normalize_email(email)
        existing_active_member = await self._session.scalar(
            select(WorkspaceMember.id)
            .join(User, User.id == WorkspaceMember.user_id)
            .where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.status == "active",
                User.email == normalized,
            )
        )
        if existing_active_member is not None:
            raise AppError(
                code="CONFLICT",
                message="Pengguna sudah menjadi anggota aktif workspace ini.",
                status_code=409,
            )

        now = datetime.now(UTC)
        pending_ids = (
            await self._session.scalars(
                select(Invitation.id)
                .where(
                    Invitation.workspace_id == workspace.id,
                    Invitation.email_normalized == normalized,
                    Invitation.accepted_at.is_(None),
                    Invitation.cancelled_at.is_(None),
                )
                .order_by(Invitation.id)
                .with_for_update()
            )
        ).all()
        if pending_ids:
            await self._session.execute(
                update(Invitation)
                .where(
                    Invitation.id.in_(pending_ids),
                    Invitation.accepted_at.is_(None),
                    Invitation.cancelled_at.is_(None),
                )
                .values(cancelled_at=now)
            )

        raw_token = generate_opaque_token()
        invitation = Invitation(
            workspace_id=workspace.id,
            email_normalized=normalized,
            role=role,
            token_hash=hash_opaque_token(raw_token),
            expires_at=now + timedelta(hours=self._settings.workspace_invitation_expire_hours),
            created_by_user_id=user.id,
            created_at=now,
        )
        self._session.add(invitation)
        await self._session.flush()
        self._record_audit(
            workspace_id=workspace.id,
            actor_user_id=user.id,
            action="workspace_invitation.created",
            resource_type="invitation",
            resource_id=str(invitation.id),
            metadata={"role": role},
        )
        await self._session.commit()

        await self._email_service.send_workspace_invitation_email(
            to_email=normalized,
            token=raw_token,
            workspace_name=workspace.name,
            role=role,
        )
        return _to_invitation_response(invitation)

    async def list_invitations(
        self,
        user: User,
        *,
        workspace_id: UUID,
        page: int,
        page_size: int,
    ) -> InvitationListResponse:
        """List workspace invitations for authorized Owner/Admin."""
        workspace, actor_member = await self._require_active_membership(
            workspace_id=workspace_id,
            user_id=user.id,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, actor_member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="manage_invitations",
        )
        total = (
            await self._session.scalar(
                select(func.count(Invitation.id)).where(Invitation.workspace_id == workspace.id)
            )
            or 0
        )
        rows = (
            await self._session.scalars(
                select(Invitation)
                .where(Invitation.workspace_id == workspace.id)
                .order_by(Invitation.created_at.desc(), Invitation.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        items = [_to_invitation_response(inv) for inv in rows]
        return InvitationListResponse(items=items, page=page, page_size=page_size, total=total)

    async def cancel_invitation(
        self,
        user: User,
        *,
        workspace_id: UUID,
        invitation_id: UUID,
    ) -> None:
        """Cancel a pending workspace invitation."""
        workspace, actor_member = await self._require_active_membership(
            workspace_id=workspace_id,
            user_id=user.id,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, actor_member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="manage_invitations",
        )
        invitation = await self._session.scalar(
            select(Invitation)
            .where(
                Invitation.id == invitation_id,
                Invitation.workspace_id == workspace.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if invitation is None:
            raise AppError(
                code="NOT_FOUND",
                message="Undangan tidak ditemukan.",
                status_code=404,
            )
        if invitation.accepted_at is not None:
            await self._session.rollback()
            raise AppError(
                code="CONFLICT",
                message="Undangan sudah diterima dan tidak dapat dibatalkan.",
                status_code=409,
            )
        if invitation.cancelled_at is None:
            invitation.cancelled_at = datetime.now(UTC)
            self._record_audit(
                workspace_id=workspace.id,
                actor_user_id=user.id,
                action="workspace_invitation.cancelled",
                resource_type="invitation",
                resource_id=str(invitation.id),
            )
            await self._session.commit()

    async def accept_invitation(self, user: User, *, raw_token: str) -> WorkspaceResponse:
        """Atomically validate and consume an invitation token for the matching user email."""
        now = datetime.now(UTC)
        token_hash = hash_opaque_token(raw_token)
        invitation = await self._session.scalar(
            select(Invitation)
            .where(Invitation.token_hash == token_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            invitation is None
            or invitation.accepted_at is not None
            or invitation.cancelled_at is not None
            or ensure_utc(invitation.expires_at) <= now
        ):
            await self._session.rollback()
            raise AppError(
                code="VALIDATION_ERROR",
                message="Token undangan tidak valid atau telah kedaluwarsa.",
                status_code=400,
            )

        if normalize_email(user.email) != invitation.email_normalized:
            await self._session.rollback()
            raise AppError(
                code="FORBIDDEN",
                message="Email akun Anda tidak cocok dengan undangan ini.",
                status_code=403,
            )

        workspace = await self._session.scalar(
            select(Workspace)
            .where(
                Workspace.id == invitation.workspace_id,
                Workspace.status == "active",
                Workspace.deleted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if workspace is None or workspace.type != "organization":
            await self._session.rollback()
            raise AppError(
                code="VALIDATION_ERROR",
                message="Workspace tujuan sudah tidak aktif.",
                status_code=400,
            )

        existing_member = await self._session.scalar(
            select(WorkspaceMember)
            .where(
                WorkspaceMember.workspace_id == workspace.id,
                WorkspaceMember.user_id == user.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing_member is not None and existing_member.status == "active":
            await self._session.rollback()
            raise AppError(
                code="CONFLICT",
                message="Anda sudah menjadi anggota aktif workspace ini.",
                status_code=409,
            )

        consumed_id = await self._session.scalar(
            update(Invitation)
            .where(
                Invitation.id == invitation.id,
                Invitation.accepted_at.is_(None),
                Invitation.cancelled_at.is_(None),
                Invitation.expires_at > now,
            )
            .values(accepted_at=now)
            .returning(Invitation.id)
            .execution_options(synchronize_session=False)
        )
        if consumed_id is None:
            await self._session.rollback()
            raise AppError(
                code="VALIDATION_ERROR",
                message="Token undangan tidak valid atau telah kedaluwarsa.",
                status_code=400,
            )

        if existing_member is not None:
            existing_member.role = invitation.role
            existing_member.status = "active"
            existing_member.joined_at = now
            existing_member.updated_at = now
            member = existing_member
        else:
            member = WorkspaceMember(
                workspace_id=workspace.id,
                user_id=user.id,
                role=invitation.role,
                status="active",
                joined_at=now,
                created_at=now,
                updated_at=now,
            )
            self._session.add(member)

        self._record_audit(
            workspace_id=workspace.id,
            actor_user_id=user.id,
            action="workspace_invitation.accepted",
            resource_type="invitation",
            resource_id=str(invitation.id),
            metadata={"role": invitation.role},
        )
        await self._session.commit()
        return _to_workspace_response(workspace, member)

