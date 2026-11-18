"""M7 Human Review, Provenance Integrity, Conflict Resolution, Versioning, and Approval tests."""

import asyncio
import copy
import socket
import subprocess
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.api.v1.deps import (
    get_email_service,
    get_internal_replay_guard,
    get_object_storage,
    get_rate_limiter,
)
from app.core.config import Settings
from app.core.internal_auth import InMemoryReplayGuard
from app.core.rate_limit import SlidingWindowRateLimiter
from app.db.base import Base
from app.db.session import create_engine, create_session_factory, get_db_session
from app.main import create_app
from app.models.document import Document, DocumentVersion
from app.models.workspace import AuditLog
from app.orchestration.base import WorkflowDispatchRequest, WorkflowInputReference
from app.orchestration.python_adapter import PythonWorkflowAdapter
from app.services.email import StubEmailService
from app.services.gemini import DeterministicMeetingMinutesProvider
from app.services.storage import InMemoryObjectStorage

INTERNAL_SECRET = "m7-internal-hmac-test-secret-minimum-32-chars"


def _build_m7_app(
    tmp_path: Path,
    *,
    db_name: str,
) -> tuple[AsyncEngine, FastAPI, Settings, StubEmailService]:
    db_file = tmp_path / db_name
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{db_file}",
        internal_hmac_secret=INTERNAL_SECRET,
        jwt_secret_key="m7-jwt-secret-for-unit-tests-only-minimum-32-chars",
        rate_limit_register_ip_max=100,
        rate_limit_login_ip_max=100,
    )
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    email_service = StubEmailService()
    storage = InMemoryObjectStorage()
    rate_limiter = SlidingWindowRateLimiter()
    replay_guard = InMemoryReplayGuard(tolerance_seconds=300)

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app(settings)
    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_email_service] = lambda: email_service
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter
    app.dependency_overrides[get_internal_replay_guard] = lambda: replay_guard
    return engine, app, settings, email_service


async def _register_user(client: AsyncClient, *, email: str, name: str) -> tuple[str, UUID, UUID]:
    reg = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "StrongPassword123!", "display_name": name},
    )
    assert reg.status_code == 201
    data = reg.json()["data"]
    token = str(data["access_token"])
    user_id = UUID(str(data["user"]["id"]))
    ws_res = await client.get("/api/v1/workspaces", headers={"Authorization": f"Bearer {token}"})
    assert ws_res.status_code == 200
    ws_id = UUID(str(ws_res.json()["data"]["items"][0]["id"]))
    return token, user_id, ws_id


async def _invite_and_accept(
    client: AsyncClient,
    *,
    owner_token: str,
    org_ws_id: UUID,
    invitee_token: str,
    invitee_email: str,
    role: str,
    email_service: StubEmailService,
) -> None:
    inv_res = await client.post(
        f"/api/v1/workspaces/{org_ws_id}/invitations",
        json={"email": invitee_email, "role": role},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert inv_res.status_code == 201
    matching = [
        m
        for m in email_service.sent_messages
        if m.template == "workspace_invitation" and m.to_email == invitee_email.lower()
    ]
    assert matching
    accept_res = await client.post(
        "/api/v1/invitations/accept",
        json={"token": matching[-1].token},
        headers={"Authorization": f"Bearer {invitee_token}"},
    )
    assert accept_res.status_code == 200


async def _create_ai_review_draft(
    client: AsyncClient,
    *,
    settings: Settings,
    token: str,
    ws_id: UUID,
    include_conflict: bool = True,
) -> tuple[str, str, dict[str, Any]]:
    auth_h = {"Authorization": f"Bearer {token}"}
    doc_id = (
        await client.post(
            f"/api/v1/workspaces/{ws_id}/documents",
            json={"title": "Notulen Rapat M7"},
            headers=auth_h,
        )
    ).json()["data"]["id"]

    src1 = (
        await client.post(
            f"/api/v1/workspaces/{ws_id}/documents/{doc_id}/sources",
            json={
                "source_type": "form",
                "form_data": {
                    "meeting_title": "Rapat Koordinasi M7",
                    "meeting_date": "2026-11-18",
                    "location": "Ruang Rapat 1",
                    "attendees": ["Sakti", "Budi"],
                    "agenda_items": ["Tinjauan Draf", "Persetujuan"],
                    "notes": (
                        "Opini: Rudi berpendapat alur tinjauan sudah jelas\n"
                        "Budi mengusulkan rilis modul review hari Jumat\n"
                    ),
                    "decisions_raw": [
                        "Gunakan skema tinjauan terpisah",
                        "Terapkan pengecekan hash versi",
                    ],
                    "action_items_raw": ["Uji komponen review | PIC: Sakti | Tenggat: 2026-11-22"],
                },
            },
            headers=auth_h,
        )
    ).json()["data"]["id"]

    if include_conflict:
        await client.post(
            f"/api/v1/workspaces/{ws_id}/documents/{doc_id}/sources",
            json={
                "source_type": "text",
                "source_name": "Catatan Tambahan",
                "raw_text": (
                    "Tanggal: 2026-11-20\n"
                    "Tidak Hadir: Ani\n"
                    "Ringkasan diskusi teknis mengenai validasi provenance.\n"
                ),
            },
            headers=auth_h,
        )

    proc = (await client.post(f"/api/v1/documents/{doc_id}/process", headers=auth_h)).json()["data"]
    ai_job = proc["jobs"][0]
    bundle_id = proc["source_bundle_id"]

    adapter = PythonWorkflowAdapter(
        settings=settings,
        gemini_provider=DeterministicMeetingMinutesProvider(),
        http_client=client,
    )
    dispatch_res = await adapter.dispatch(
        WorkflowDispatchRequest(
            job_id=UUID(ai_job["id"]),
            idempotency_key=f"ai:{ai_job['id']}",
            workflow_key=ai_job["workflow_key"],
            workflow_version=ai_job["workflow_version"],
            document_type="meeting_minutes",
            input_reference=WorkflowInputReference(
                source_bundle_id=UUID(bundle_id),
                retrieval_endpoint=f"/api/v1/internal/source-bundles/{bundle_id}",
            ),
            callback_endpoint="/api/v1/internal/workflows/callback",
            issued_at=datetime.now(UTC),
        )
    )
    assert dispatch_res.accepted is True
    detail = (await client.get(f"/api/v1/documents/{doc_id}", headers=auth_h)).json()["data"]
    assert detail["status"] == "review_required"
    return doc_id, src1, detail


async def test_provenance_integrity_on_edited_deleted_reordered_copied_and_forged_items(
    tmp_path: Path,
) -> None:
    """Reject forged provenance and strip provenance on edited/copied/duplicated items."""
    engine, app, settings, _ = _build_m7_app(tmp_path, db_name="m7_provenance.sqlite")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            token, _, ws_id = await _register_user(c, email="prov@example.com", name="Prov User")
            auth_h = {"Authorization": f"Bearer {token}"}
            doc_id, src1_id, detail = await _create_ai_review_draft(
                c, settings=settings, token=token, ws_id=ws_id, include_conflict=False
            )

            v1 = detail["current_version"]
            base = copy.deepcopy(v1["structured_content"])
            assert len(base["decisions"]) == 2
            assert len(base["proposals"]) == 1

            # 1. Reorder decisions [D2, D1] with NO edits -> both keep grounded provenance
            reordered = copy.deepcopy(base)
            reordered["decisions"] = [base["decisions"][1], base["decisions"][0]]
            r_reord = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": reordered,
                    "expected_version_id": v1["id"],
                    "expected_content_hash": v1["content_hash"],
                },
                headers=auth_h,
            )
            assert r_reord.status_code == 200
            v2 = r_reord.json()["data"]["current_version"]
            assert v2["version_number"] == 2
            v2_content = v2["structured_content"]
            assert [d["origin"] for d in v2_content["decisions"]] == ["grounded", "grounded"]
            assert v2_content["field_statuses"]["decisions"] == "grounded"
            assert v2_content["decisions"][0]["source_id"] == src1_id

            # 2-6. Comprehensive manipulation in one save over v2:
            manipulated = copy.deepcopy(v2_content)
            # (a) Edited AI-grounded decision while deliberately keeping source_id/excerpt/origin
            manipulated["decisions"][0]["decision"] = "Keputusan yang telah diubah peninjau"
            manipulated["decisions"][0]["origin"] = "grounded"
            # (b) Duplicated grounded decision (v2_content["decisions"][1] appears twice)
            dup_grounded = copy.deepcopy(v2_content["decisions"][1])
            dup_grounded["origin"] = "grounded"
            manipulated["decisions"].append(dup_grounded)
            # (c) Cross-section copy: copy a grounded proposal into decisions with its provenance
            copied_prop = {
                "decision": v2_content["proposals"][0]["proposal"],
                "origin": "grounded",
                "source_id": v2_content["proposals"][0]["source_id"],
                "excerpt": v2_content["proposals"][0]["excerpt"],
            }
            manipulated["decisions"].append(copied_prop)
            # (d) Newly created item attempting to forge real DocumentSource.id + excerpt
            forged_new = {
                "decision": "Keputusan baru buatan peninjau",
                "origin": "grounded",
                "source_id": src1_id,
                "excerpt": "Judul Rapat: Rapat Koordinasi M7",
            }
            manipulated["decisions"].append(forged_new)
            # (e) Delete grounded opinion -> provenance entry for opinions must be pruned
            manipulated["opinions"] = []
            # (f) Edit scalar field 'location' while forging field_statuses & top-level provenance
            manipulated["location"] = "Aula Baru"
            manipulated["field_statuses"]["location"] = "grounded"
            manipulated["provenance"].append(
                {
                    "field_path": "location",
                    "source_id": src1_id,
                    "source_name": "Rapat Koordinasi M7",
                    "source_type": "form",
                    "excerpt": "Lokasi palsu",
                }
            )
            # (g) Edit action_items[0].assignee while trying to retain grounded provenance
            manipulated["action_items"][0]["assignee"] = "Andi"
            manipulated["action_items"][0]["origin"] = "grounded"

            r_manip = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": manipulated,
                    "expected_version_id": v2["id"],
                    "expected_content_hash": v2["content_hash"],
                },
                headers=auth_h,
            )
            assert r_manip.status_code == 200
            v3 = r_manip.json()["data"]["current_version"]
            v3c = v3["structured_content"]

            # Decisions: [0] edited->user_provided, [1] untouched->grounded,
            # [2] duplicate->user_provided, [3] cross-section copy->user_provided,
            # [4] forged new->user_provided
            assert [d["origin"] for d in v3c["decisions"]] == [
                "user_provided",
                "grounded",
                "user_provided",
                "user_provided",
                "user_provided",
            ]
            assert v3c["decisions"][0]["source_id"] is None
            assert v3c["decisions"][0]["excerpt"] is None
            assert v3c["decisions"][1]["source_id"] == src1_id
            assert v3c["decisions"][2]["source_id"] is None
            assert v3c["decisions"][3]["source_id"] is None
            assert v3c["decisions"][4]["source_id"] is None
            assert v3c["field_statuses"]["decisions"] == "user_provided"

            # Deleted opinions -> field_status missing and no opinions provenance remains
            assert v3c["field_statuses"]["opinions"] == "missing"
            assert not any(p["field_path"].startswith("opinions") for p in v3c["provenance"])

            # Edited action_items -> user_provided and source_id/excerpt cleared
            assert v3c["action_items"][0]["origin"] == "user_provided"
            assert v3c["action_items"][0]["source_id"] is None
            assert v3c["action_items"][0]["excerpt"] is None
            assert v3c["field_statuses"]["action_items"] == "user_provided"

            # Forged location status & provenance stripped; untouched attendees stays grounded
            assert v3c["field_statuses"]["location"] == "user_provided"
            assert not any(p["field_path"] == "location" for p in v3c["provenance"])
            assert v3c["field_statuses"]["attendees"] == "grounded"
            assert any(p["field_path"] == "attendees" for p in v3c["provenance"])

            # Delete remaining grounded decision too -> decisions provenance completely pruned
            cleared_decs = copy.deepcopy(v3c)
            cleared_decs["decisions"] = []
            r_clear = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": cleared_decs,
                    "expected_version_id": v3["id"],
                    "expected_content_hash": v3["content_hash"],
                },
                headers=auth_h,
            )
            assert r_clear.status_code == 200
            v4c = r_clear.json()["data"]["current_version"]["structured_content"]
            assert v4c["field_statuses"]["decisions"] == "missing"
            assert not any(p["field_path"].startswith("decisions") for p in v4c["provenance"])
    finally:
        await engine.dispose()


async def test_conflict_resolution_rules_and_approval_preconditions(tmp_path: Path) -> None:
    """Enforce explicit conflict resolution, approval preconditions, hash match & idempotency."""
    engine, app, settings, _ = _build_m7_app(tmp_path, db_name="m7_approve.sqlite")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            token, user_id, ws_id = await _register_user(
                c, email="reviewer@example.com", name="Reviewer"
            )
            auth_h = {"Authorization": f"Bearer {token}"}
            doc_id, _, detail = await _create_ai_review_draft(
                c, settings=settings, token=token, ws_id=ws_id, include_conflict=True
            )

            v1 = detail["current_version"]
            v1c = v1["structured_content"]
            assert v1c["field_statuses"]["meeting_date"] == "conflicting"
            assert detail["review_readiness"]["is_ready_for_approval"] is False
            assert "meeting_date" in detail["review_readiness"]["unresolved_conflict_fields"]

            # 1. Approval blocked while conflict remains unresolved
            block_res = await c.post(
                f"/api/v1/documents/{doc_id}/approve",
                json={"version_id": v1["id"], "content_hash": v1["content_hash"]},
                headers=auth_h,
            )
            assert block_res.status_code == 422
            assert (
                "meeting_date" in block_res.json()["error"]["details"]["unresolved_conflict_fields"]
            )

            # 2. Saving unrelated edit (plus forged empty conflicts=[]) MUST NOT silently resolve
            # meeting_date merely because meeting_date holds candidate 0
            edit_other = copy.deepcopy(v1c)
            edit_other["chairperson"] = "Pak Hendra"
            edit_other["conflicts"] = []
            r_other = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": edit_other,
                    "expected_version_id": v1["id"],
                    "expected_content_hash": v1["content_hash"],
                },
                headers=auth_h,
            )
            assert r_other.status_code == 200
            v2 = r_other.json()["data"]["current_version"]
            assert v2["structured_content"]["field_statuses"]["meeting_date"] == "conflicting"
            assert any(
                cf["field_path"] == "meeting_date" for cf in v2["structured_content"]["conflicts"]
            )

            # 3. Attempting to resolve a conflict with a blank value is rejected (422)
            blank_resolve = copy.deepcopy(v2["structured_content"])
            blank_resolve["meeting_date"] = "   "
            r_blank = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": blank_resolve,
                    "expected_version_id": v2["id"],
                    "expected_content_hash": v2["content_hash"],
                    "resolved_conflict_fields": ["meeting_date"],
                },
                headers=auth_h,
            )
            assert r_blank.status_code == 422

            # 4. Explicit confirmation of current value via resolved_conflict_fields resolves
            # conflict and creates v3 with user_provided status
            r_confirm = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": v2["structured_content"],
                    "expected_version_id": v2["id"],
                    "expected_content_hash": v2["content_hash"],
                    "resolved_conflict_fields": ["meeting_date"],
                },
                headers=auth_h,
            )
            assert r_confirm.status_code == 200
            v3 = r_confirm.json()["data"]["current_version"]
            assert v3["version_number"] == 3
            assert v3["structured_content"]["field_statuses"]["meeting_date"] == "user_provided"
            assert v3["structured_content"]["conflicts"] == []
            assert r_confirm.json()["data"]["review_readiness"]["is_ready_for_approval"] is True

            # 5. Test every mandatory approval precondition by clearing required fields
            # (while also testing optional fields: chairperson, organizer, decisions can be empty!)
            for field_to_clear, expected_missing in (
                ("meeting_title", "meeting_title"),
                ("meeting_date", "meeting_date"),
                ("attendees", "attendees"),
                ("agenda_items", "agenda_items"),
            ):
                missing_payload = copy.deepcopy(v3["structured_content"])
                missing_payload[field_to_clear] = (
                    [] if isinstance(missing_payload[field_to_clear], list) else None
                )
                prev_ver = (await c.get(f"/api/v1/documents/{doc_id}", headers=auth_h)).json()[
                    "data"
                ]["current_version"]
                r_miss = await c.patch(
                    f"/api/v1/documents/{doc_id}",
                    json={
                        "structured_content": missing_payload,
                        "expected_version_id": prev_ver["id"],
                        "expected_content_hash": prev_ver["content_hash"],
                    },
                    headers=auth_h,
                )
                assert r_miss.status_code == 200
                cur_v = r_miss.json()["data"]["current_version"]
                app_fail = await c.post(
                    f"/api/v1/documents/{doc_id}/approve",
                    json={"version_id": cur_v["id"], "content_hash": cur_v["content_hash"]},
                    headers=auth_h,
                )
                assert app_fail.status_code == 422
                assert (
                    expected_missing
                    in app_fail.json()["error"]["details"]["missing_required_fields"]
                )

            # Also verify all 3 content sections empty fails, while empty decisions + empty
            # chairperson/organizer succeeds when discussion_summary is present
            latest_v = (await c.get(f"/api/v1/documents/{doc_id}", headers=auth_h)).json()["data"][
                "current_version"
            ]
            no_content = copy.deepcopy(v3["structured_content"])
            no_content["discussion_summary"] = None
            no_content["decisions"] = []
            no_content["action_items"] = []
            r_nocontent = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": no_content,
                    "expected_version_id": latest_v["id"],
                    "expected_content_hash": latest_v["content_hash"],
                },
                headers=auth_h,
            )
            assert r_nocontent.status_code == 200
            v_nocontent = r_nocontent.json()["data"]["current_version"]
            app_nocontent = await c.post(
                f"/api/v1/documents/{doc_id}/approve",
                json={"version_id": v_nocontent["id"], "content_hash": v_nocontent["content_hash"]},
                headers=auth_h,
            )
            assert app_nocontent.status_code == 422
            assert (
                "content_sections"
                in app_nocontent.json()["error"]["details"]["missing_required_fields"]
            )

            # Restore valid minimal payload with empty decisions, null chairperson, null organizer
            ready_optional = copy.deepcopy(v3["structured_content"])
            ready_optional["chairperson"] = None
            ready_optional["organizer"] = None
            ready_optional["decisions"] = []
            ready_optional["discussion_summary"] = "Pembahasan awal disepakati sebagai informasi."
            r_ready = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": ready_optional,
                    "expected_version_id": v_nocontent["id"],
                    "expected_content_hash": v_nocontent["content_hash"],
                },
                headers=auth_h,
            )
            assert r_ready.status_code == 200
            v_ready = r_ready.json()["data"]["current_version"]
            assert r_ready.json()["data"]["review_readiness"]["is_ready_for_approval"] is True

            # 6. Wrong version_id or content_hash rejected with 409 CONFLICT
            bad_hash_app = await c.post(
                f"/api/v1/documents/{doc_id}/approve",
                json={"version_id": v_ready["id"], "content_hash": "0" * 64},
                headers=auth_h,
            )
            assert bad_hash_app.status_code == 409
            bad_ver_app = await c.post(
                f"/api/v1/documents/{doc_id}/approve",
                json={"version_id": v1["id"], "content_hash": v_ready["content_hash"]},
                headers=auth_h,
            )
            assert bad_ver_app.status_code == 409

            # 7. Valid approval transitions document to approved and records approver + timestamp
            ok_app = await c.post(
                f"/api/v1/documents/{doc_id}/approve",
                json={"version_id": v_ready["id"], "content_hash": v_ready["content_hash"]},
                headers=auth_h,
            )
            assert ok_app.status_code == 200
            approved_data = ok_app.json()["data"]
            assert approved_data["status"] == "approved"
            assert approved_data["approved_version_id"] == v_ready["id"]
            assert approved_data["current_version"]["approval_status"] == "approved"
            assert approved_data["current_version"]["approved_by_user_id"] == str(user_id)
            assert approved_data["current_version"]["approved_at"] is not None

            # Idempotent re-approval returns 200 without duplicating audit log
            idem_app = await c.post(
                f"/api/v1/documents/{doc_id}/approve",
                json={"version_id": v_ready["id"], "content_hash": v_ready["content_hash"]},
                headers=auth_h,
            )
            assert idem_app.status_code == 200

        async with session_factory() as session:
            approve_audits = (
                await session.scalars(
                    select(AuditLog).where(
                        AuditLog.workspace_id == ws_id,
                        AuditLog.action == "documents.approved",
                    )
                )
            ).all()
            assert len(approve_audits) == 1
    finally:
        await engine.dispose()


async def test_versioning_noop_historical_loading_post_approval_and_m8_completed_compat(
    tmp_path: Path,
) -> None:
    """Verify no-op protection, optimistic concurrency, historical load, and completed edits."""
    engine, app, settings, _ = _build_m7_app(tmp_path, db_name="m7_versioning.sqlite")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            token, user_id, ws_id = await _register_user(
                c, email="ver@example.com", name="Version User"
            )
            auth_h = {"Authorization": f"Bearer {token}"}
            doc_id, _, detail = await _create_ai_review_draft(
                c, settings=settings, token=token, ws_id=ws_id, include_conflict=True
            )
            v1 = detail["current_version"]

            # 1. No-op PATCH on untouched AI draft creates NO duplicate version
            noop_res = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": v1["structured_content"],
                    "expected_version_id": v1["id"],
                    "expected_content_hash": v1["content_hash"],
                },
                headers=auth_h,
            )
            assert noop_res.status_code == 200
            assert noop_res.json()["data"]["current_version"]["version_number"] == 1

            # 2. Resolve conflict to create v2, then approve v2
            v2_payload = copy.deepcopy(v1["structured_content"])
            v2_payload["meeting_date"] = "2026-11-19"
            r_v2 = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": v2_payload,
                    "expected_version_id": v1["id"],
                    "expected_content_hash": v1["content_hash"],
                },
                headers=auth_h,
            )
            assert r_v2.status_code == 200
            v2 = r_v2.json()["data"]["current_version"]
            assert v2["version_number"] == 2

            # Stale expected_version_id rejected (409)
            stale_patch = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": v2["structured_content"],
                    "expected_version_id": v1["id"],
                },
                headers=auth_h,
            )
            assert stale_patch.status_code == 409

            app_v2 = await c.post(
                f"/api/v1/documents/{doc_id}/approve",
                json={"version_id": v2["id"], "content_hash": v2["content_hash"]},
                headers=auth_h,
            )
            assert app_v2.status_code == 200
            assert app_v2.json()["data"]["status"] == "approved"

            # No-op PATCH on approved v2 MUST NOT invalidate approval
            noop_approved = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": v2["structured_content"],
                    "expected_version_id": v2["id"],
                    "expected_content_hash": v2["content_hash"],
                },
                headers=auth_h,
            )
            assert noop_approved.status_code == 200
            assert noop_approved.json()["data"]["status"] == "approved"
            assert noop_approved.json()["data"]["approved_version_id"] == v2["id"]
            assert noop_approved.json()["data"]["current_version"]["version_number"] == 2

            # 3. Historical version loading ("Muat ke Editor"): load v1 while v2 is current
            # (using expected_version_id=v2.id), keeping v1's unresolved conflict unless resolved
            r_v3 = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": v1["structured_content"],
                    "expected_version_id": v2["id"],
                    "expected_content_hash": v2["content_hash"],
                },
                headers=auth_h,
            )
            assert r_v3.status_code == 200
            d_v3 = r_v3.json()["data"]
            assert d_v3["status"] == "review_required"
            assert d_v3["approved_version_id"] is None
            assert d_v3["current_version"]["version_number"] == 3
            assert (
                d_v3["current_version"]["structured_content"]["field_statuses"]["meeting_date"]
                == "conflicting"
            )

            # Check v2 remained immutable & retained its historical approved_by_user_id/approved_at
            vers = (await c.get(f"/api/v1/documents/{doc_id}/versions", headers=auth_h)).json()[
                "data"
            ]["items"]
            v2_hist = next(v for v in vers if v["version_number"] == 2)
            assert v2_hist["approval_status"] == "superseded"
            assert v2_hist["approved_by_user_id"] == str(user_id)
            assert v2_hist["approved_at"] is not None
            assert v2_hist["content_hash"] == v2["content_hash"]

            # 4. Resolve conflict in v4, approve v4, then simulate M8 'completed' status
            r_v4 = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": d_v3["current_version"]["structured_content"],
                    "expected_version_id": d_v3["current_version"]["id"],
                    "expected_content_hash": d_v3["current_version"]["content_hash"],
                    "resolved_conflict_fields": ["meeting_date"],
                },
                headers=auth_h,
            )
            assert r_v4.status_code == 200
            v4 = r_v4.json()["data"]["current_version"]
            app_v4 = await c.post(
                f"/api/v1/documents/{doc_id}/approve",
                json={"version_id": v4["id"], "content_hash": v4["content_hash"]},
                headers=auth_h,
            )
            assert app_v4.status_code == 200

        # Simulate M8 setting documents.status = 'completed'
        async with session_factory() as session:
            doc_row = await session.scalar(select(Document).where(Document.id == UUID(doc_id)))
            assert doc_row is not None
            doc_row.status = "completed"
            await session.commit()

        # Re-approve directly on 'completed' document fails (409)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            reapp_completed = await c.post(
                f"/api/v1/documents/{doc_id}/approve",
                json={"version_id": v4["id"], "content_hash": v4["content_hash"]},
                headers=auth_h,
            )
            assert reapp_completed.status_code == 409

            # Editing 'completed' document invalidates approval -> review_required, clears
            # approved_version_id, and marks v4 superseded while keeping its approval audit fields
            edit_completed = copy.deepcopy(v4["structured_content"])
            edit_completed["location"] = "Gedung Serbaguna"
            r_v5 = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": edit_completed,
                    "expected_version_id": v4["id"],
                    "expected_content_hash": v4["content_hash"],
                },
                headers=auth_h,
            )
            assert r_v5.status_code == 200
            d_v5 = r_v5.json()["data"]
            assert d_v5["status"] == "review_required"
            assert d_v5["approved_version_id"] is None
            assert d_v5["current_version"]["version_number"] == 5

            vers_after = (
                await c.get(f"/api/v1/documents/{doc_id}/versions", headers=auth_h)
            ).json()["data"]["items"]
            v4_hist = next(v for v in vers_after if v["version_number"] == 4)
            assert v4_hist["approval_status"] == "superseded"
            assert v4_hist["approved_by_user_id"] == str(user_id)
            assert v4_hist["approved_at"] is not None
    finally:
        await engine.dispose()


async def test_review_and_approval_rbac_and_cross_workspace_idor(tmp_path: Path) -> None:
    """Enforce Owner/Admin/Editor/Member permissions on M7 review and approval endpoints."""
    engine, app, settings, email_service = _build_m7_app(tmp_path, db_name="m7_rbac.sqlite")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            owner_tok, _, _ = await _register_user(c, email="own@org.com", name="Owner")
            admin_tok, _, _ = await _register_user(c, email="adm@org.com", name="Admin")
            ed_tok, _, _ = await _register_user(c, email="ed@org.com", name="Editor")
            mem1_tok, _, _ = await _register_user(c, email="m1@org.com", name="Member 1")
            mem2_tok, _, _ = await _register_user(c, email="m2@org.com", name="Member 2")
            outsider_tok, _, _ = await _register_user(c, email="out@other.com", name="Outsider")

            org_id = UUID(
                (
                    await c.post(
                        "/api/v1/workspaces",
                        json={"name": "OSIS Nusantara"},
                        headers={"Authorization": f"Bearer {owner_tok}"},
                    )
                ).json()["data"]["id"]
            )
            for inv_tok, inv_email, role in (
                (admin_tok, "adm@org.com", "admin"),
                (ed_tok, "ed@org.com", "editor"),
                (mem1_tok, "m1@org.com", "member"),
                (mem2_tok, "m2@org.com", "member"),
            ):
                await _invite_and_accept(
                    c,
                    owner_token=owner_tok,
                    org_ws_id=org_id,
                    invitee_token=inv_tok,
                    invitee_email=inv_email,
                    role=role,
                    email_service=email_service,
                )

            doc_id, _, detail = await _create_ai_review_draft(
                c, settings=settings, token=mem1_tok, ws_id=org_id, include_conflict=False
            )
            v1 = detail["current_version"]

            # Editor viewing Member 1's doc: GET allowed (200), PATCH and POST /approve denied (403)
            assert (
                await c.get(
                    f"/api/v1/documents/{doc_id}", headers={"Authorization": f"Bearer {ed_tok}"}
                )
            ).status_code == 200
            assert (
                await c.patch(
                    f"/api/v1/documents/{doc_id}",
                    json={"title": "Edited by Editor"},
                    headers={"Authorization": f"Bearer {ed_tok}"},
                )
            ).status_code == 403
            assert (
                await c.post(
                    f"/api/v1/documents/{doc_id}/approve",
                    json={"version_id": v1["id"], "content_hash": v1["content_hash"]},
                    headers={"Authorization": f"Bearer {ed_tok}"},
                )
            ).status_code == 403

            # Member 2 viewing/editing/approving Member 1's doc: all denied (403)
            assert (
                await c.get(
                    f"/api/v1/documents/{doc_id}", headers={"Authorization": f"Bearer {mem2_tok}"}
                )
            ).status_code == 403
            assert (
                await c.patch(
                    f"/api/v1/documents/{doc_id}",
                    json={"title": "Edited by Member 2"},
                    headers={"Authorization": f"Bearer {mem2_tok}"},
                )
            ).status_code == 403
            assert (
                await c.post(
                    f"/api/v1/documents/{doc_id}/approve",
                    json={"version_id": v1["id"], "content_hash": v1["content_hash"]},
                    headers={"Authorization": f"Bearer {mem2_tok}"},
                )
            ).status_code == 403

            # Outsider (cross-workspace): denied (403)
            assert (
                await c.post(
                    f"/api/v1/documents/{doc_id}/approve",
                    json={"version_id": v1["id"], "content_hash": v1["content_hash"]},
                    headers={"Authorization": f"Bearer {outsider_tok}"},
                )
            ).status_code == 403

            # Admin can edit and Owner can approve Member 1's document
            edited_content = copy.deepcopy(v1["structured_content"])
            edited_content["location"] = "Aula Sekolah"
            admin_edit = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "structured_content": edited_content,
                    "expected_version_id": v1["id"],
                    "expected_content_hash": v1["content_hash"],
                },
                headers={"Authorization": f"Bearer {admin_tok}"},
            )
            assert admin_edit.status_code == 200
            v2 = admin_edit.json()["data"]["current_version"]

            owner_app = await c.post(
                f"/api/v1/documents/{doc_id}/approve",
                json={"version_id": v2["id"], "content_hash": v2["content_hash"]},
                headers={"Authorization": f"Bearer {owner_tok}"},
            )
            assert owner_app.status_code == 200
            assert owner_app.json()["data"]["status"] == "approved"
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
def pg_review_db_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Start an ephemeral PostgreSQL 16 cluster, apply Alembic head, and yield its async URL."""
    pg_bin = Path("/usr/lib/postgresql/16/bin")
    initdb_bin = pg_bin / "initdb"
    pg_ctl_bin = pg_bin / "pg_ctl"
    if not initdb_bin.exists() or not pg_ctl_bin.exists():
        pytest.skip("PostgreSQL 16 binaries not installed locally")

    pg_root = tmp_path_factory.mktemp("pg16_m7_review")
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
        test_db_url = f"postgresql+asyncpg://postgres@127.0.0.1:{port}/doc_saas_m7_test"
        asyncio.run(_create_pg_database(admin_url, "doc_saas_m7_test"))

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


async def test_concurrent_edits_and_approval_race_on_pg(pg_review_db_url: str) -> None:
    """Enforce row-locked optimistic concurrency and edit-vs-approve race safety on PG 16."""
    settings = Settings(
        environment="test",
        database_url=pg_review_db_url,
        internal_hmac_secret="pg-m7-concurrency-hmac-secret-minimum-32-chars",
        jwt_secret_key="pg-m7-concurrency-jwt-secret-minimum-32-chars",
        rate_limit_register_ip_max=100,
    )
    engine = create_engine(pg_review_db_url)
    session_factory = create_session_factory(engine)
    email_service = StubEmailService()
    storage = InMemoryObjectStorage()
    rate_limiter = SlidingWindowRateLimiter()
    replay_guard = InMemoryReplayGuard(tolerance_seconds=300)

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app(settings)
    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_email_service] = lambda: email_service
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter
    app.dependency_overrides[get_internal_replay_guard] = lambda: replay_guard

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            token, _, ws_id = await _register_user(
                c, email="pg-m7-race@example.com", name="PG Race"
            )
            auth_h = {"Authorization": f"Bearer {token}"}
            doc_id, _, detail = await _create_ai_review_draft(
                c, settings=settings, token=token, ws_id=ws_id, include_conflict=False
            )
            v1 = detail["current_version"]

            # 1. Two concurrent edits targeting v1 -> exactly one 200 and one 409
            edit_a = copy.deepcopy(v1["structured_content"])
            edit_a["location"] = "Ruang A"
            edit_b = copy.deepcopy(v1["structured_content"])
            edit_b["location"] = "Ruang B"

            async def send_patch(content: dict[str, Any]) -> int:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://testserver"
                ) as caller:
                    res = await caller.patch(
                        f"/api/v1/documents/{doc_id}",
                        json={
                            "structured_content": content,
                            "expected_version_id": v1["id"],
                            "expected_content_hash": v1["content_hash"],
                        },
                        headers=auth_h,
                    )
                    return res.status_code

            s1, s2 = await asyncio.gather(send_patch(edit_a), send_patch(edit_b))
            assert sorted([s1, s2]) == [200, 409]

            # 2. Concurrent edit vs approve targeting v2 -> invariant: approved_version_id is either
            # None (if edit won or followed approval) or matches the approved v2, never stale.
            v2 = (await c.get(f"/api/v1/documents/{doc_id}", headers=auth_h)).json()["data"][
                "current_version"
            ]
            edit_c = copy.deepcopy(v2["structured_content"])
            edit_c["organizer"] = "Panitia M7"

            async def call_edit() -> int:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://testserver"
                ) as caller:
                    res = await caller.patch(
                        f"/api/v1/documents/{doc_id}",
                        json={
                            "structured_content": edit_c,
                            "expected_version_id": v2["id"],
                            "expected_content_hash": v2["content_hash"],
                        },
                        headers=auth_h,
                    )
                    return res.status_code

            async def call_approve() -> int:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://testserver"
                ) as caller:
                    res = await caller.post(
                        f"/api/v1/documents/{doc_id}/approve",
                        json={"version_id": v2["id"], "content_hash": v2["content_hash"]},
                        headers=auth_h,
                    )
                    return res.status_code

            edit_status, app_status = await asyncio.gather(call_edit(), call_approve())
            assert 200 in (edit_status, app_status)
            assert {edit_status, app_status}.issubset({200, 409})

        async with session_factory() as session:
            doc_row = await session.scalar(select(Document).where(Document.id == UUID(doc_id)))
            assert doc_row is not None
            ver_count = await session.scalar(
                select(func.count(DocumentVersion.id)).where(
                    DocumentVersion.document_id == UUID(doc_id)
                )
            )
            if doc_row.status == "approved":
                assert doc_row.approved_version_id == doc_row.current_version_id
            else:
                assert doc_row.status == "review_required"
                assert doc_row.approved_version_id is None
                assert ver_count == 3
    finally:
        await engine.dispose()
