"""Workspace, RBAC, invitation, tenant isolation, and PG16 concurrency tests (Milestone M3)."""

import asyncio
import socket
import subprocess
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.api.v1.deps import get_email_service, get_rate_limiter
from app.core.config import Settings
from app.core.errors import AppError
from app.core.permissions import (
    DocumentAction,
    WorkspaceRole,
    authorize_document_access,
    can_access_document,
)
from app.core.rate_limit import SlidingWindowRateLimiter
from app.core.security import hash_opaque_token
from app.db.base import Base
from app.db.session import create_engine, create_session_factory, get_db_session
from app.main import create_app
from app.models.workspace import AuditLog, Invitation, Workspace, WorkspaceMember
from app.services.email import StubEmailService


async def _register_user(
    app: FastAPI,
    *,
    email: str,
    display_name: str,
    client_ip: str,
) -> tuple[str, str]:
    async with AsyncClient(
        transport=ASGITransport(app=app, client=(client_ip, 5000)),
        base_url="http://testserver",
    ) as client:
        res = await client.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": "StrongPassword123!",
                "display_name": display_name,
            },
        )
        assert res.status_code == 201
        data = res.json()["data"]
        return str(data["access_token"]), str(data["user"]["id"])


def _build_workspace_test_app(
    tmp_path: Path,
    *,
    db_name: str = "workspaces.sqlite",
) -> tuple[AsyncEngine, FastAPI, StubEmailService, Settings]:
    db_file = tmp_path / db_name
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{db_file}",
        internal_hmac_secret="test-secret-for-unit-tests-only-minimum-32-chars",
        jwt_secret_key="test-jwt-secret-for-unit-tests-only-minimum-32-chars",
        rate_limit_register_ip_max=100,
    )
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    email_service = StubEmailService()
    rate_limiter = SlidingWindowRateLimiter()

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app(settings)
    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_email_service] = lambda: email_service
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter
    return engine, app, email_service, settings


async def test_registration_creates_single_personal_workspace_and_db_enforces_uniqueness(
    tmp_path: Path,
) -> None:
    """Registration creates one Personal Workspace; DB rejects a duplicate personal workspace."""
    engine, app, _, _ = _build_workspace_test_app(tmp_path, db_name="personal_ws.sqlite")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)

    try:
        token, user_id = await _register_user(
            app,
            email="personal@example.com",
            display_name="Personal Owner",
            client_ip="198.51.100.1",
        )
        headers = {"Authorization": f"Bearer {token}"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            listed = await c.get("/api/v1/workspaces", headers=headers)
            assert listed.status_code == 200
            items = listed.json()["data"]["items"]
            assert len(items) == 1
            personal = items[0]
            assert personal["type"] == "personal"
            assert personal["my_role"] == "owner"
            assert personal["owner_user_id"] == user_id
            ws_id = personal["id"]

            # Owner can rename their personal workspace
            renamed = await c.patch(
                f"/api/v1/workspaces/{ws_id}",
                headers=headers,
                json={"name": "Catatan Pribadi Sakti"},
            )
            assert renamed.status_code == 200
            assert renamed.json()["data"]["name"] == "Catatan Pribadi Sakti"

            # Organization-only operations are rejected on Personal Workspace
            assert (
                await c.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
            ).status_code == 400
            assert (
                await c.post(
                    f"/api/v1/workspaces/{ws_id}/transfer-ownership",
                    headers=headers,
                    json={"new_owner_user_id": user_id},
                )
            ).status_code == 400
            assert (
                await c.post(
                    f"/api/v1/workspaces/{ws_id}/invitations",
                    headers=headers,
                    json={"email": "other@example.com", "role": "member"},
                )
            ).status_code == 400

        # DB-level partial unique index prevents a second personal workspace for the same owner
        async with session_factory() as session:
            session.add(
                Workspace(
                    name="Duplicate Personal",
                    type="personal",
                    owner_user_id=UUID(user_id),
                    status="active",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()
    finally:
        await engine.dispose()


def test_organization_document_permission_matrix() -> None:
    """Enforce exact Owner/Admin/Editor/Member permissions on own vs. other org documents."""
    actor_id = uuid4()
    other_owner_id = uuid4()
    all_actions: tuple[DocumentAction, ...] = (
        "view",
        "edit",
        "submit_source",
        "process",
        "approve",
        "generate",
        "delete",
    )

    expected_other_doc_allowed: dict[WorkspaceRole, set[DocumentAction]] = {
        "owner": set(all_actions),
        "admin": set(all_actions),
        "editor": {"view"},
        "member": set(),
    }

    for role, allowed_other in expected_other_doc_allowed.items():
        for action in all_actions:
            # Own document: all roles have full access
            assert (
                can_access_document(
                    role=role,
                    workspace_type="organization",
                    actor_user_id=actor_id,
                    document_owner_user_id=actor_id,
                    action=action,
                )
                is True
            )
            authorize_document_access(
                role=role,
                workspace_type="organization",
                actor_user_id=actor_id,
                document_owner_user_id=actor_id,
                action=action,
            )

            # Other member's document: strictly follow permission matrix
            expected = action in allowed_other
            assert (
                can_access_document(
                    role=role,
                    workspace_type="organization",
                    actor_user_id=actor_id,
                    document_owner_user_id=other_owner_id,
                    action=action,
                )
                is expected
            )
            if expected:
                authorize_document_access(
                    role=role,
                    workspace_type="organization",
                    actor_user_id=actor_id,
                    document_owner_user_id=other_owner_id,
                    action=action,
                )
            else:
                with pytest.raises(AppError) as exc:
                    authorize_document_access(
                        role=role,
                        workspace_type="organization",
                        actor_user_id=actor_id,
                        document_owner_user_id=other_owner_id,
                        action=action,
                    )
                assert exc.value.status_code == 403
                assert exc.value.code == "FORBIDDEN"

    # Personal workspace only allows its owner
    assert (
        can_access_document(
            role="owner",
            workspace_type="personal",
            actor_user_id=actor_id,
            document_owner_user_id=actor_id,
            action="approve",
        )
        is True
    )
    assert (
        can_access_document(
            role="owner",
            workspace_type="personal",
            actor_user_id=actor_id,
            document_owner_user_id=other_owner_id,
            action="view",
        )
        is False
    )


async def test_organization_workspace_invitations_lifecycle_and_email_match(
    tmp_path: Path,
) -> None:
    """Invitations store hashed tokens, enforce email match, expiry, supersession, & single use."""
    engine, app, email_service, _ = _build_workspace_test_app(tmp_path, db_name="invites.sqlite")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)

    try:
        owner_token, _ = await _register_user(
            app, email="org-owner@example.com", display_name="Org Owner", client_ip="198.51.100.10"
        )
        invitee_token, _ = await _register_user(
            app, email="invitee@example.com", display_name="Invitee", client_ip="198.51.100.11"
        )
        wrong_token, _ = await _register_user(
            app, email="wrong@example.com", display_name="Wrong User", client_ip="198.51.100.12"
        )
        owner_headers = {"Authorization": f"Bearer {owner_token}"}
        invitee_headers = {"Authorization": f"Bearer {invitee_token}"}
        wrong_headers = {"Authorization": f"Bearer {wrong_token}"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            created = await c.post(
                "/api/v1/workspaces",
                headers=owner_headers,
                json={"name": "OSIS SMA Nusantara"},
            )
            assert created.status_code == 201
            org_id = created.json()["data"]["id"]
            assert created.json()["data"]["my_role"] == "owner"

            # First invitation -> superseded by second invitation for same email
            inv1 = await c.post(
                f"/api/v1/workspaces/{org_id}/invitations",
                headers=owner_headers,
                json={"email": "  Invitee@Example.com ", "role": "member"},
            )
            assert inv1.status_code == 201
            raw_token_1 = [
                m for m in email_service.sent_messages if m.template == "workspace_invitation"
            ][-1].token

            inv2 = await c.post(
                f"/api/v1/workspaces/{org_id}/invitations",
                headers=owner_headers,
                json={"email": "invitee@example.com", "role": "editor"},
            )
            assert inv2.status_code == 201
            raw_token_2 = [
                m for m in email_service.sent_messages if m.template == "workspace_invitation"
            ][-1].token
            assert raw_token_2 != raw_token_1

            # DB stores hashed token only
            async with session_factory() as session:
                inv_row = await session.scalar(
                    select(Invitation).where(Invitation.id == UUID(inv2.json()["data"]["id"]))
                )
                assert inv_row is not None
                assert inv_row.token_hash == hash_opaque_token(raw_token_2)
                assert inv_row.token_hash != raw_token_2

            # Superseded token_1 fails
            superseded_res = await c.post(
                "/api/v1/invitations/accept",
                headers=invitee_headers,
                json={"token": raw_token_1},
            )
            assert superseded_res.status_code == 400

            # Wrong email user gets 403 FORBIDDEN without consuming token_2
            wrong_accept = await c.post(
                "/api/v1/invitations/accept",
                headers=wrong_headers,
                json={"token": raw_token_2},
            )
            assert wrong_accept.status_code == 403

            # Intended user accepts token_2 cleanly
            accepted = await c.post(
                "/api/v1/invitations/accept",
                headers=invitee_headers,
                json={"token": raw_token_2},
            )
            assert accepted.status_code == 200
            assert accepted.json()["data"]["id"] == org_id
            assert accepted.json()["data"]["my_role"] == "editor"

            # Reusing token_2 fails (single-use)
            replayed = await c.post(
                "/api/v1/invitations/accept",
                headers=invitee_headers,
                json={"token": raw_token_2},
            )
            assert replayed.status_code == 400

            # Inviting an already-active member returns 409 CONFLICT
            dup_inv = await c.post(
                f"/api/v1/workspaces/{org_id}/invitations",
                headers=owner_headers,
                json={"email": "invitee@example.com", "role": "admin"},
            )
            assert dup_inv.status_code == 409

            # Cancelled invitation cannot be accepted
            cancel_target = await c.post(
                f"/api/v1/workspaces/{org_id}/invitations",
                headers=owner_headers,
                json={"email": "cancel@example.com", "role": "member"},
            )
            assert cancel_target.status_code == 201
            cancel_id = cancel_target.json()["data"]["id"]
            cancel_token = [
                m for m in email_service.sent_messages if m.template == "workspace_invitation"
            ][-1].token
            assert (
                await c.delete(
                    f"/api/v1/workspaces/{org_id}/invitations/{cancel_id}",
                    headers=owner_headers,
                )
            ).status_code == 200
            assert (
                await c.post(
                    "/api/v1/invitations/accept",
                    headers=wrong_headers,
                    json={"token": cancel_token},
                )
            ).status_code == 400

            # Expired invitation cannot be accepted
            exp_inv = await c.post(
                f"/api/v1/workspaces/{org_id}/invitations",
                headers=owner_headers,
                json={"email": "wrong@example.com", "role": "member"},
            )
            assert exp_inv.status_code == 201
            exp_id = exp_inv.json()["data"]["id"]
            exp_token = [
                m for m in email_service.sent_messages if m.template == "workspace_invitation"
            ][-1].token
            async with session_factory() as session:
                row = await session.scalar(
                    select(Invitation).where(Invitation.id == UUID(exp_id))
                )
                assert row is not None
                row.expires_at = datetime.now(UTC) - timedelta(minutes=5)
                await session.commit()
            assert (
                await c.post(
                    "/api/v1/invitations/accept",
                    headers=wrong_headers,
                    json={"token": exp_token},
                )
            ).status_code == 400

            # Audit log records workspace and invitation events
            async with session_factory() as session:
                actions = (
                    await session.scalars(
                        select(AuditLog.action).where(AuditLog.workspace_id == UUID(org_id))
                    )
                ).all()
                assert "workspace.created" in actions
                assert "workspace_invitation.created" in actions
                assert "workspace_invitation.accepted" in actions
                assert "workspace_invitation.cancelled" in actions
    finally:
        await engine.dispose()


async def test_workspace_rbac_hierarchy_owner_protection_transfer_and_member_removal(
    tmp_path: Path,
) -> None:
    """Enforce role hierarchy, Owner protection, ownership transfer, and member removal."""
    engine, app, email_service, _ = _build_workspace_test_app(tmp_path, db_name="rbac.sqlite")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        owner_tok, owner_id = await _register_user(
            app, email="owner@example.com", display_name="Owner", client_ip="198.51.100.20"
        )
        admin_tok, admin_id = await _register_user(
            app, email="admin@example.com", display_name="Admin", client_ip="198.51.100.21"
        )
        editor_tok, editor_id = await _register_user(
            app, email="editor@example.com", display_name="Editor", client_ip="198.51.100.22"
        )
        member_tok, member_id = await _register_user(
            app, email="member@example.com", display_name="Member", client_ip="198.51.100.23"
        )
        owner_h = {"Authorization": f"Bearer {owner_tok}"}
        admin_h = {"Authorization": f"Bearer {admin_tok}"}
        editor_h = {"Authorization": f"Bearer {editor_tok}"}
        member_h = {"Authorization": f"Bearer {member_tok}"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            ws_res = await c.post(
                "/api/v1/workspaces", headers=owner_h, json={"name": "Karang Taruna"}
            )
            assert ws_res.status_code == 201
            org_id = ws_res.json()["data"]["id"]

            for email, role, user_h in (
                ("admin@example.com", "admin", admin_h),
                ("editor@example.com", "editor", editor_h),
                ("member@example.com", "member", member_h),
            ):
                inv = await c.post(
                    f"/api/v1/workspaces/{org_id}/invitations",
                    headers=owner_h,
                    json={"email": email, "role": role},
                )
                assert inv.status_code == 201
                raw_tok = [
                    m for m in email_service.sent_messages if m.template == "workspace_invitation"
                ][-1].token
                acc = await c.post(
                    "/api/v1/invitations/accept", headers=user_h, json={"token": raw_tok}
                )
                assert acc.status_code == 200

            # Editor and Member cannot manage settings, invitations, or members
            for low_h in (editor_h, member_h):
                assert (
                    await c.patch(
                        f"/api/v1/workspaces/{org_id}",
                        headers=low_h,
                        json={"name": "Hijacked"},
                    )
                ).status_code == 403
                assert (
                    await c.post(
                        f"/api/v1/workspaces/{org_id}/invitations",
                        headers=low_h,
                        json={"email": "x@example.com", "role": "member"},
                    )
                ).status_code == 403
                assert (
                    await c.patch(
                        f"/api/v1/workspaces/{org_id}/members/{member_id}",
                        headers=low_h,
                        json={"role": "admin"},
                    )
                ).status_code == 403

            # Admin can rename workspace and promote member -> editor
            assert (
                await c.patch(
                    f"/api/v1/workspaces/{org_id}",
                    headers=admin_h,
                    json={"name": "Karang Taruna Maju"},
                )
            ).status_code == 200
            promoted = await c.patch(
                f"/api/v1/workspaces/{org_id}/members/{member_id}",
                headers=admin_h,
                json={"role": "editor"},
            )
            assert promoted.status_code == 200
            assert promoted.json()["data"]["role"] == "editor"

            # Admin CANNOT modify Owner, remove Owner, transfer ownership, or delete workspace
            assert (
                await c.patch(
                    f"/api/v1/workspaces/{org_id}/members/{owner_id}",
                    headers=admin_h,
                    json={"role": "member"},
                )
            ).status_code == 403
            assert (
                await c.delete(f"/api/v1/workspaces/{org_id}/members/{owner_id}", headers=admin_h)
            ).status_code == 403
            assert (
                await c.post(
                    f"/api/v1/workspaces/{org_id}/transfer-ownership",
                    headers=admin_h,
                    json={"new_owner_user_id": admin_id},
                )
            ).status_code == 403
            assert (
                await c.delete(f"/api/v1/workspaces/{org_id}", headers=admin_h)
            ).status_code == 403

            # Removing a member revokes their access immediately while leaving user/docs intact
            assert (
                await c.delete(
                    f"/api/v1/workspaces/{org_id}/members/{member_id}",
                    headers=admin_h,
                )
            ).status_code == 200
            assert (
                await c.get(f"/api/v1/workspaces/{org_id}", headers=member_h)
            ).status_code == 403
            member_ws_res = await c.get("/api/v1/workspaces", headers=member_h)
            member_ws = member_ws_res.json()["data"]["items"]
            assert [w["type"] for w in member_ws] == ["personal"]
            # Remaining Owner/Admin retain full access & Editor retains read-only access to
            # documents whose owner was removed from the workspace.
            assert (
                can_access_document(
                    role="admin",
                    workspace_type="organization",
                    actor_user_id=UUID(admin_id),
                    document_owner_user_id=UUID(member_id),
                    action="edit",
                )
                is True
            )
            assert (
                can_access_document(
                    role="editor",
                    workspace_type="organization",
                    actor_user_id=UUID(editor_id),
                    document_owner_user_id=UUID(member_id),
                    action="view",
                )
                is True
            )

            # Owner transfers ownership to Editor; old Owner becomes Admin
            xfer = await c.post(
                f"/api/v1/workspaces/{org_id}/transfer-ownership",
                headers=owner_h,
                json={"new_owner_user_id": editor_id},
            )
            assert xfer.status_code == 200
            assert xfer.json()["data"]["owner_user_id"] == editor_id
            assert xfer.json()["data"]["my_role"] == "admin"

            # Old owner (now admin) can no longer delete workspace; new owner can
            assert (
                await c.delete(f"/api/v1/workspaces/{org_id}", headers=owner_h)
            ).status_code == 403
            assert (
                await c.delete(f"/api/v1/workspaces/{org_id}", headers=editor_h)
            ).status_code == 200
            assert (
                await c.get(f"/api/v1/workspaces/{org_id}", headers=editor_h)
            ).status_code == 403
    finally:
        await engine.dispose()


async def test_cross_tenant_workspace_isolation(tmp_path: Path) -> None:
    """Non-members cannot read or mutate another tenant's Personal or Organization workspace."""
    engine, app, _, _ = _build_workspace_test_app(tmp_path, db_name="isolation.sqlite")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        tok_a, id_a = await _register_user(
            app, email="tenant-a@example.com", display_name="Tenant A", client_ip="198.51.100.30"
        )
        tok_b, id_b = await _register_user(
            app, email="tenant-b@example.com", display_name="Tenant B", client_ip="198.51.100.31"
        )
        headers_a = {"Authorization": f"Bearer {tok_a}"}
        headers_b = {"Authorization": f"Bearer {tok_b}"}

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            personal_a_id = (
                await c.get("/api/v1/workspaces", headers=headers_a)
            ).json()["data"]["items"][0]["id"]
            org_a_id = (
                await c.post(
                    "/api/v1/workspaces", headers=headers_a, json={"name": "Org Tenant A"}
                )
            ).json()["data"]["id"]

            for target_ws_id in (personal_a_id, org_a_id):
                assert (
                    await c.get(f"/api/v1/workspaces/{target_ws_id}", headers=headers_b)
                ).status_code == 403
                assert (
                    await c.patch(
                        f"/api/v1/workspaces/{target_ws_id}",
                        headers=headers_b,
                        json={"name": "Pwned"},
                    )
                ).status_code == 403
                assert (
                    await c.delete(f"/api/v1/workspaces/{target_ws_id}", headers=headers_b)
                ).status_code == 403
                assert (
                    await c.post(
                        f"/api/v1/workspaces/{target_ws_id}/transfer-ownership",
                        headers=headers_b,
                        json={"new_owner_user_id": id_b},
                    )
                ).status_code == 403
                assert (
                    await c.get(f"/api/v1/workspaces/{target_ws_id}/members", headers=headers_b)
                ).status_code == 403
                assert (
                    await c.patch(
                        f"/api/v1/workspaces/{target_ws_id}/members/{id_a}",
                        headers=headers_b,
                        json={"role": "member"},
                    )
                ).status_code == 403
                assert (
                    await c.delete(
                        f"/api/v1/workspaces/{target_ws_id}/members/{id_a}",
                        headers=headers_b,
                    )
                ).status_code == 403
                assert (
                    await c.get(
                        f"/api/v1/workspaces/{target_ws_id}/invitations",
                        headers=headers_b,
                    )
                ).status_code == 403
                assert (
                    await c.post(
                        f"/api/v1/workspaces/{target_ws_id}/invitations",
                        headers=headers_b,
                        json={"email": "x@example.com", "role": "admin"},
                    )
                ).status_code == 403
                assert (
                    await c.delete(
                        f"/api/v1/workspaces/{target_ws_id}/invitations/{uuid4()}",
                        headers=headers_b,
                    )
                ).status_code == 403
    finally:
        await engine.dispose()


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


async def _create_pg_database(admin_url: str, db_name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def pg_workspace_db_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Start an ephemeral PostgreSQL 16 cluster, run migrations, and yield its async URL."""
    pg_bin = Path("/usr/lib/postgresql/16/bin")
    initdb_bin = pg_bin / "initdb"
    pg_ctl_bin = pg_bin / "pg_ctl"
    if not initdb_bin.exists() or not pg_ctl_bin.exists():
        pytest.skip("PostgreSQL 16 binaries not installed locally")

    pg_root = tmp_path_factory.mktemp("pg16_workspace_concurrency")
    pg_data = pg_root / "pgdata"
    pg_log = pg_root / "pg.log"
    port = _find_free_port()

    subprocess.run(
        [str(initdb_bin), "-D", str(pg_data), "-U", "postgres", "--auth=trust"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            str(pg_ctl_bin),
            "-D",
            str(pg_data),
            "-l",
            str(pg_log),
            "-o",
            f"-k {pg_root} -p {port}",
            "start",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    try:
        admin_url = f"postgresql+asyncpg://postgres@127.0.0.1:{port}/postgres"
        test_db_url = f"postgresql+asyncpg://postgres@127.0.0.1:{port}/doc_saas_ws_test"
        asyncio.run(_create_pg_database(admin_url, "doc_saas_ws_test"))

        backend_dir = Path(__file__).resolve().parents[1]
        alembic_cfg = Config(str(backend_dir / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", test_db_url)
        command.upgrade(alembic_cfg, "head")
        yield test_db_url
    finally:
        subprocess.run(
            [str(pg_ctl_bin), "-D", str(pg_data), "stop", "-m", "immediate"],
            check=False,
            capture_output=True,
            text=True,
        )


async def test_concurrent_invitation_accept_consumes_token_once_on_pg(
    pg_workspace_db_url: str,
) -> None:
    """Concurrent /invitations/accept requests consume the token once on PostgreSQL 16."""
    settings = Settings(
        environment="test",
        database_url=pg_workspace_db_url,
        internal_hmac_secret="pg-ws-concurrency-hmac-secret-minimum-32-chars",
        jwt_secret_key="pg-ws-concurrency-jwt-secret-minimum-32-chars",
        rate_limit_register_ip_max=100,
    )
    engine = create_engine(pg_workspace_db_url)
    session_factory = create_session_factory(engine)
    email_service = StubEmailService()
    rate_limiter = SlidingWindowRateLimiter()

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app(settings)
    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_email_service] = lambda: email_service
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter

    try:
        owner_tok, _ = await _register_user(
            app, email="pg-ws-owner@example.com", display_name="PG Owner", client_ip="198.51.100.40"
        )
        invitee_tok, invitee_id = await _register_user(
            app,
            email="pg-ws-invitee@example.com",
            display_name="PG Invitee",
            client_ip="198.51.100.41",
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            ws_res = await c.post(
                "/api/v1/workspaces",
                headers={"Authorization": f"Bearer {owner_tok}"},
                json={"name": "PG Concurrent Org"},
            )
            assert ws_res.status_code == 201
            org_id = ws_res.json()["data"]["id"]

            inv_res = await c.post(
                f"/api/v1/workspaces/{org_id}/invitations",
                headers={"Authorization": f"Bearer {owner_tok}"},
                json={"email": "pg-ws-invitee@example.com", "role": "editor"},
            )
            assert inv_res.status_code == 201
            raw_token = [
                m for m in email_service.sent_messages if m.template == "workspace_invitation"
            ][-1].token

        async def send_accept() -> Response:
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
            ) as c:
                return await c.post(
                    "/api/v1/invitations/accept",
                    headers={"Authorization": f"Bearer {invitee_tok}"},
                    json={"token": raw_token},
                )

        responses = list(await asyncio.gather(*[send_accept() for _ in range(6)]))
        statuses = [res.status_code for res in responses]
        assert statuses.count(200) == 1
        assert statuses.count(400) == 5

        async with session_factory() as session:
            inv = await session.scalar(
                select(Invitation).where(Invitation.token_hash == hash_opaque_token(raw_token))
            )
            assert inv is not None
            assert inv.accepted_at is not None

            member = await session.scalar(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == UUID(org_id),
                    WorkspaceMember.user_id == UUID(invitee_id),
                )
            )
            assert member is not None
            assert member.status == "active"
            assert member.role == "editor"
    finally:
        await engine.dispose()
