"""Document foundation, source validation, RBAC, IDOR, and job tests (Milestone M5)."""

import asyncio
import io
import socket
import subprocess
import zipfile
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
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

from app.api.v1.deps import get_email_service, get_object_storage, get_rate_limiter
from app.core.config import Settings
from app.core.rate_limit import SlidingWindowRateLimiter
from app.db.base import Base
from app.db.session import create_engine, create_session_factory, get_db_session
from app.main import create_app
from app.models.credit import CreditReservation, CreditWallet
from app.models.document import (
    Document,
    DocumentSource,
    Job,
    Template,
    TemplateVersion,
)
from app.services.credit import CreditService
from app.services.document import DatabaseJobLivenessChecker
from app.services.email import StubEmailService
from app.services.storage import InMemoryObjectStorage

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PRINTER_SETTINGS_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.printerSettings"
)
HYPERLINK_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
)


def _make_docx_bytes(
    *,
    document_xml: bytes | None = None,
    content_types_xml: bytes | None = None,
    extra_entries: dict[str, bytes] | None = None,
    omit_content_types: bool = False,
) -> bytes:
    """Create a valid (or deliberately crafted) DOCX zip archive in memory."""
    buf = io.BytesIO()
    default_ct = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" '
        b'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Override PartName="/word/document.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml'
        b'.document.main+xml"/>'
        b"</Types>"
    )
    default_doc = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body>"
        b"<w:p><w:r><w:t>Notulen {{jud</w:t></w:r><w:r><w:t>ul}}</w:t></w:r></w:p>"
        b"<w:p><w:r><w:t>Peserta: Sakti, Budi</w:t></w:r></w:p>"
        b"</w:body>"
        b"</w:document>"
    )
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        if not omit_content_types:
            zf.writestr("[Content_Types].xml", content_types_xml or default_ct)
        if document_xml is not None:
            zf.writestr("word/document.xml", document_xml)
        elif not (extra_entries and "__omit_document_xml__" in extra_entries):
            zf.writestr("word/document.xml", default_doc)
        if extra_entries:
            for name, content in extra_entries.items():
                if name == "__omit_document_xml__":
                    continue
                zf.writestr(name, content)
    return buf.getvalue()


def _make_pdf_bytes(extra_body: bytes = b"") -> bytes:
    """Build a minimal structurally valid PDF byte stream."""
    return b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n" + extra_body + b"\n%%EOF\n"


def _build_document_test_app(
    tmp_path: Path,
    *,
    db_name: str,
    beta_complimentary_credits: int | None = None,
    ai_generation_credit_cost: int | None = None,
    max_upload_file_size_bytes: int = 10 * 1024 * 1024,
    max_archive_expansion_ratio: int = 10,
    max_zip_entries: int = 1000,
) -> tuple[AsyncEngine, FastAPI, Settings, InMemoryObjectStorage, StubEmailService]:
    db_file = tmp_path / db_name
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{db_file}",
        internal_hmac_secret="test-secret-for-unit-tests-only-minimum-32-chars",
        jwt_secret_key="test-jwt-secret-for-unit-tests-only-minimum-32-chars",
        beta_complimentary_credits=beta_complimentary_credits,
        ai_generation_credit_cost=ai_generation_credit_cost,
        max_upload_file_size_bytes=max_upload_file_size_bytes,
        max_archive_expansion_ratio=max_archive_expansion_ratio,
        max_zip_entries=max_zip_entries,
        rate_limit_register_ip_max=100,
        rate_limit_login_ip_max=100,
    )
    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    email_service = StubEmailService()
    storage = InMemoryObjectStorage()
    rate_limiter = SlidingWindowRateLimiter()

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app(settings)
    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_email_service] = lambda: email_service
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter
    return engine, app, settings, storage, email_service


async def _register_user(
    client: AsyncClient,
    *,
    email: str,
    display_name: str,
) -> tuple[str, UUID, UUID]:
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
    token = str(data["access_token"])
    user_id = UUID(str(data["user"]["id"]))

    ws_res = await client.get(
        "/api/v1/workspaces",
        headers={"Authorization": f"Bearer {token}"},
    )
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
    raw_token = matching[-1].token
    accept_res = await client.post(
        "/api/v1/invitations/accept",
        json={"token": raw_token},
        headers={"Authorization": f"Bearer {invitee_token}"},
    )
    assert accept_res.status_code == 200


async def test_document_hybrid_sources_versioning_and_soft_delete(tmp_path: Path) -> None:
    """Create a meeting_minutes document, attach form/text/DOCX/PDF/TXT, version, and delete."""
    engine, app, _, storage, _ = _build_document_test_app(tmp_path, db_name="m5_hybrid.sqlite")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            token, _, ws_id = await _register_user(
                c, email="owner@example.com", display_name="Owner"
            )
            headers = {"Authorization": f"Bearer {token}"}

            create_res = await c.post(
                f"/api/v1/workspaces/{ws_id}/documents",
                json={
                    "title": "Notulen Rapat Mingguan",
                    "document_type": "meeting_minutes",
                    "initial_content": {"judul": "Draf Awal"},
                },
                headers=headers,
            )
            assert create_res.status_code == 201
            doc = create_res.json()["data"]
            doc_id = doc["id"]
            assert doc["status"] == "draft"
            assert doc["current_version"]["version_number"] == 1

            # 1. Structured form source
            form_res = await c.post(
                f"/api/v1/workspaces/{ws_id}/documents/{doc_id}/sources",
                json={
                    "source_type": "form",
                    "form_data": {
                        "meeting_title": "Rapat Koordinasi Q4",
                        "meeting_date": "2026-11-18",
                        "attendees": ["Sakti", "Budi"],
                        "agenda_items": ["Evaluasi M4", "Persiapan M5"],
                        "notes": "Diskusi berjalan lancar.",
                    },
                },
                headers=headers,
            )
            assert form_res.status_code == 201
            assert form_res.json()["data"]["source_type"] == "form"
            assert "Rapat Koordinasi Q4" in form_res.json()["data"]["extracted_text_preview"]

            # 2. Raw text source
            text_res = await c.post(
                f"/api/v1/workspaces/{ws_id}/documents/{doc_id}/sources",
                json={
                    "source_type": "text",
                    "source_name": "Catatan Cepat",
                    "raw_text": "Keputusan: lanjut ke implementasi M5.",
                },
                headers=headers,
            )
            assert text_res.status_code == 201
            assert text_res.json()["data"]["source_type"] == "text"

            # 3-5. Uploaded DOCX (with split Word XML runs), PDF, TXT
            uploads = [
                ("meeting.docx", DOCX_MIME, _make_docx_bytes()),
                ("lampiran.pdf", "application/pdf", _make_pdf_bytes()),
                ("transkrip.txt", "text/plain", b"Budi mengusulkan jadwal rilis Jumat."),
            ]
            for filename, mime, content in uploads:
                intent_res = await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents/{doc_id}/sources/upload-intents",
                    json={"filename": filename, "content_type": mime, "size_bytes": len(content)},
                    headers=headers,
                )
                assert intent_res.status_code == 201
                object_key = intent_res.json()["data"]["object_key"]
                assert object_key.startswith(f"workspaces/{ws_id}/documents/{doc_id}/sources/")
                storage.put_object(object_key=object_key, content=content, content_type=mime)

                confirm_res = await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents/{doc_id}/sources",
                    json={
                        "source_type": "upload",
                        "object_key": object_key,
                        "filename": filename,
                        "content_type": mime,
                    },
                    headers=headers,
                )
                assert confirm_res.status_code == 201
                confirmed = confirm_res.json()["data"]
                assert confirmed["has_stored_object"] is True
                assert confirmed["validation_status"] == "valid"
                if filename == "meeting.docx":
                    # Verified Word run-split joining: "{{jud" + "ul}}" -> "{{judul}}"
                    assert "Notulen {{judul}}" in confirmed["extracted_text_preview"]

            # Verify source listing has all 5 sources (2 text/form + 3 uploads)
            sources_res = await c.get(f"/api/v1/documents/{doc_id}/sources", headers=headers)
            assert sources_res.status_code == 200
            assert sources_res.json()["data"]["total"] == 5

            # Edit structured content -> creates v2 without mutating v1
            patch_res = await c.patch(
                f"/api/v1/documents/{doc_id}",
                json={
                    "title": "Notulen Rapat Mingguan (Revisi)",
                    "structured_content": {"judul": "Draf Revisi 2"},
                },
                headers=headers,
            )
            assert patch_res.status_code == 200
            assert patch_res.json()["data"]["current_version"]["version_number"] == 2

            vers_res = await c.get(f"/api/v1/documents/{doc_id}/versions", headers=headers)
            assert vers_res.status_code == 200
            versions = vers_res.json()["data"]["items"]
            assert [v["version_number"] for v in versions] == [2, 1]
            assert versions[1]["structured_content"] == {"judul": "Draf Awal"}

            # Soft-delete archives the document and hides it from subsequent reads
            del_res = await c.delete(f"/api/v1/documents/{doc_id}", headers=headers)
            assert del_res.status_code == 200
            assert del_res.json()["data"]["status"] == "archived"

            get_deleted = await c.get(f"/api/v1/documents/{doc_id}", headers=headers)
            assert get_deleted.status_code == 404
            list_after = await c.get(f"/api/v1/workspaces/{ws_id}/documents", headers=headers)
            assert list_after.status_code == 200
            assert list_after.json()["data"]["total"] == 0
    finally:
        await engine.dispose()


async def test_upload_security_validation_bombs_xxe_macros_external_refs_and_caps(
    tmp_path: Path,
) -> None:
    """Enforce upload caps, MIME/magic alignment, zip bomb, XXE, macro, and key isolation."""
    engine, app, _, storage, _ = _build_document_test_app(
        tmp_path,
        db_name="m5_upload_sec.sqlite",
        max_upload_file_size_bytes=4096,
        max_archive_expansion_ratio=5,
        max_zip_entries=8,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            token, _, ws_id = await _register_user(c, email="sec@example.com", display_name="Sec")
            headers = {"Authorization": f"Bearer {token}"}

            d1 = (
                await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents",
                    json={"title": "Dokumen 1"},
                    headers=headers,
                )
            ).json()["data"]["id"]
            d2 = (
                await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents",
                    json={"title": "Dokumen 2"},
                    headers=headers,
                )
            ).json()["data"]["id"]

            # 1. Wrong extension / legacy .doc / MIME mismatch / oversized declaration
            for bad_intent in (
                {"filename": "legacy.doc", "content_type": "application/msword", "size_bytes": 100},
                {"filename": "archive.zip", "content_type": "application/zip", "size_bytes": 100},
                {"filename": "fake.docx", "content_type": "text/plain", "size_bytes": 100},
                {"filename": "big.txt", "content_type": "text/plain", "size_bytes": 999999},
            ):
                r = await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents/{d1}/sources/upload-intents",
                    json=bad_intent,
                    headers=headers,
                )
                assert r.status_code == 422
                assert r.json()["error"]["code"] == "UPLOAD_REJECTED"

            async def attempt_upload(filename: str, mime: str, data: bytes) -> tuple[int, str]:
                ir = await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents/{d1}/sources/upload-intents",
                    json={"filename": filename, "content_type": mime, "size_bytes": len(data)},
                    headers=headers,
                )
                assert ir.status_code == 201
                key = ir.json()["data"]["object_key"]
                storage.put_object(object_key=key, content=data, content_type=mime)
                sr = await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents/{d1}/sources",
                    json={
                        "source_type": "upload",
                        "object_key": key,
                        "filename": filename,
                        "content_type": mime,
                    },
                    headers=headers,
                )
                code = sr.json()["error"]["code"] if not sr.json()["success"] else "OK"
                return sr.status_code, code

            # 2. Bad magic bytes / corrupt ZIP / non-DOCX ZIP
            assert await attempt_upload("bad_magic.docx", DOCX_MIME, b"NOTAZIP") == (
                422,
                "UPLOAD_REJECTED",
            )
            assert await attempt_upload("bad.pdf", "application/pdf", b"plain text not pdf") == (
                422,
                "UPLOAD_REJECTED",
            )
            assert await attempt_upload("bin.txt", "text/plain", b"hello\x00world") == (
                422,
                "UPLOAD_REJECTED",
            )
            assert await attempt_upload(
                "notdocx.docx",
                DOCX_MIME,
                _make_docx_bytes(omit_content_types=True),
            ) == (422, "UPLOAD_REJECTED")

            # 3. ZIP bomb (expansion ratio > 5) & excessive entry count (> 8)
            bomb_docx = _make_docx_bytes(extra_entries={"word/pad.txt": b"A" * 20_000})
            assert await attempt_upload("bomb.docx", DOCX_MIME, bomb_docx) == (
                422,
                "UPLOAD_REJECTED",
            )
            many_entries = {f"word/extra_{i}.txt": b"hi" for i in range(9)}
            many_docx = _make_docx_bytes(extra_entries=many_entries)
            assert await attempt_upload("many.docx", DOCX_MIME, many_docx) == (
                422,
                "UPLOAD_REJECTED",
            )

            # 4. XML DTD / XXE payload rejected
            xxe_xml = (
                b'<?xml version="1.0"?>'
                b'<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                b"<w:document/>"
            )
            assert await attempt_upload(
                "xxe.docx", DOCX_MIME, _make_docx_bytes(document_xml=xxe_xml)
            ) == (422, "UPLOAD_REJECTED")

            # 5. Embedded VBA macro / OLE binary / PDF JavaScript rejected
            assert await attempt_upload(
                "macro.docx",
                DOCX_MIME,
                _make_docx_bytes(extra_entries={"word/vbaProject.bin": b"\x00\x01"}),
            ) == (422, "UPLOAD_REJECTED")
            assert await attempt_upload(
                "js.pdf",
                "application/pdf",
                _make_pdf_bytes(b"2 0 obj << /JavaScript (app.alert(1)) >> endobj"),
            ) == (422, "UPLOAD_REJECTED")

            # 6. External .rels relationship rejected (never fetched)
            external_rels = (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<Relationships xmlns="http://schemas.openxmlformats.org/'
                b'package/2006/relationships">'
                b'<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                b'officeDocument/2006/relationships/attachedTemplate" '
                b'Target="http://evil.invalid/template.dotm" TargetMode="External"/>'
                b"</Relationships>"
            )
            assert await attempt_upload(
                "ext.docx",
                DOCX_MIME,
                _make_docx_bytes(extra_entries={"word/_rels/settings.xml.rels": external_rels}),
            ) == (422, "UPLOAD_REJECTED")

            # 7. Foreign object key belonging to another document / workspace rejected
            ir2 = await c.post(
                f"/api/v1/workspaces/{ws_id}/documents/{d2}/sources/upload-intents",
                json={"filename": "ok.txt", "content_type": "text/plain", "size_bytes": 5},
                headers=headers,
            )
            foreign_key = ir2.json()["data"]["object_key"]
            storage.put_object(object_key=foreign_key, content=b"hello", content_type="text/plain")
            cross_confirm = await c.post(
                f"/api/v1/workspaces/{ws_id}/documents/{d1}/sources",
                json={
                    "source_type": "upload",
                    "object_key": foreign_key,
                    "filename": "ok.txt",
                    "content_type": "text/plain",
                },
                headers=headers,
            )
            assert cross_confirm.status_code == 422
            assert cross_confirm.json()["error"]["code"] == "UPLOAD_REJECTED"

            # 8. Max 5 uploaded files per document cap enforced cleanly
            for i in range(5):
                status, code = await attempt_upload(f"ok_{i}.txt", "text/plain", b"valid text")
                assert (status, code) == (201, "OK")
            sixth_intent = await c.post(
                f"/api/v1/workspaces/{ws_id}/documents/{d1}/sources/upload-intents",
                json={"filename": "sixth.txt", "content_type": "text/plain", "size_bytes": 10},
                headers=headers,
            )
            assert sixth_intent.status_code == 422
            assert sixth_intent.json()["error"]["code"] == "UPLOAD_REJECTED"
    finally:
        await engine.dispose()


async def test_process_document_jobs_credits_and_database_job_liveness(tmp_path: Path) -> None:
    """Verify separate doc vs job states, bundle ID, atomic credit reservation, and liveness."""
    engine, app, settings, storage, _ = _build_document_test_app(
        tmp_path,
        db_name="m5_process.sqlite",
        beta_complimentary_credits=2,
        ai_generation_credit_cost=2,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            token, _, ws_id = await _register_user(
                c, email="proc@example.com", display_name="Proc"
            )
            headers = {"Authorization": f"Bearer {token}"}

            d1 = (
                await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents",
                    json={"title": "Dokumen Siap Proses"},
                    headers=headers,
                )
            ).json()["data"]["id"]
            d2 = (
                await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents",
                    json={"title": "Dokumen Kedua"},
                    headers=headers,
                )
            ).json()["data"]["id"]

            # Process with zero sources fails validation cleanly
            empty_proc = await c.post(f"/api/v1/documents/{d1}/process", headers=headers)
            assert empty_proc.status_code == 422

            # Attach TXT upload source + text source to d1
            ir = await c.post(
                f"/api/v1/workspaces/{ws_id}/documents/{d1}/sources/upload-intents",
                json={"filename": "notes.txt", "content_type": "text/plain", "size_bytes": 12},
                headers=headers,
            )
            key = ir.json()["data"]["object_key"]
            storage.put_object(object_key=key, content=b"Catatan rapat", content_type="text/plain")
            assert (
                await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents/{d1}/sources",
                    json={
                        "source_type": "upload",
                        "object_key": key,
                        "filename": "notes.txt",
                        "content_type": "text/plain",
                    },
                    headers=headers,
                )
            ).status_code == 201

            # Attach text source to d2 so only credits block it
            assert (
                await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents/{d2}/sources",
                    json={"source_type": "text", "raw_text": "Isi dokumen dua"},
                    headers=headers,
                )
            ).status_code == 201

            # Start processing d1 with idempotency key
            proc_res = await c.post(
                f"/api/v1/documents/{d1}/process",
                json={"idempotency_key": "proc-key-1"},
                headers=headers,
            )
            assert proc_res.status_code == 202
            proc_data = proc_res.json()["data"]
            assert proc_data["document"]["status"] == "processing"
            assert proc_data["reserved_credits"] == 2
            res_id = UUID(proc_data["reservation_id"])
            bundle_id = proc_data["source_bundle_id"]
            job_types = {j["job_type"]: j for j in proc_data["jobs"]}
            assert set(job_types.keys()) == {"normalize", "ai_generation"}
            assert all(j["status"] == "queued" for j in proc_data["jobs"])
            assert all(j["source_bundle_id"] == bundle_id for j in proc_data["jobs"])

            # Idempotent retry returns identical jobs/bundle without double reserving
            retry_res = await c.post(
                f"/api/v1/documents/{d1}/process",
                json={"idempotency_key": "proc-key-1"},
                headers=headers,
            )
            assert retry_res.status_code == 202
            assert retry_res.json()["data"]["reservation_id"] == str(res_id)

            # Query GET /jobs/{job_id} shows separate async job state
            ai_job_id = job_types["ai_generation"]["id"]
            job_res = await c.get(f"/api/v1/jobs/{ai_job_id}", headers=headers)
            assert job_res.status_code == 200
            assert job_res.json()["data"]["status"] == "queued"

            # Second document fails atomically with INSUFFICIENT_CREDITS and no orphan job
            no_cred = await c.post(
                f"/api/v1/documents/{d2}/process",
                json={"idempotency_key": "proc-key-2"},
                headers=headers,
            )
            assert no_cred.status_code == 402
            assert no_cred.json()["error"]["code"] == "INSUFFICIENT_CREDITS"
            d2_after = await c.get(f"/api/v1/documents/{d2}", headers=headers)
            assert d2_after.json()["data"]["status"] == "draft"
            assert d2_after.json()["data"]["jobs"] == []

        # DatabaseJobLivenessChecker protects active jobs from stale reconciliation
        async with session_factory() as session:
            reservation = await session.scalar(
                select(CreditReservation).where(CreditReservation.id == res_id)
            )
            assert reservation is not None
            reservation.expires_at = datetime.now(UTC) - timedelta(seconds=60)
            await session.commit()

            checker = DatabaseJobLivenessChecker()
            credit_svc = CreditService(session=session, settings=settings)
            expired_while_active = await credit_svc.reconcile_stale_reservations(
                workspace_id=ws_id,
                job_liveness_checker=checker,
            )
            assert expired_while_active == []

            ai_job = await session.scalar(select(Job).where(Job.id == UUID(ai_job_id)))
            assert ai_job is not None
            ai_job.status = "failed"
            await session.commit()

            expired_after_fail = await credit_svc.reconcile_stale_reservations(
                workspace_id=ws_id,
                job_liveness_checker=checker,
            )
            assert expired_after_fail == [res_id]

            wallet = await session.scalar(
                select(CreditWallet).where(CreditWallet.workspace_id == ws_id)
            )
            assert wallet is not None
            assert wallet.available_balance == 2
            assert wallet.reserved_balance == 0

            # Verify DocumentSource rows are linked to source_bundle_id
            src = await session.scalar(
                select(DocumentSource).where(DocumentSource.document_id == UUID(d1))
            )
            assert src is not None
            assert str(src.source_bundle_id) == bundle_id
    finally:
        await engine.dispose()


async def test_org_document_permission_policy_and_member_removal_retention(
    tmp_path: Path,
) -> None:
    """Enforce Owner/Admin/Editor/Member permissions and retain removed members' documents."""
    engine, app, _, storage, email_service = _build_document_test_app(
        tmp_path, db_name="m5_rbac.sqlite"
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            owner_tok, _, _ = await _register_user(
                c, email="owner@org.com", display_name="Owner"
            )
            admin_tok, _, _ = await _register_user(
                c, email="admin@org.com", display_name="Admin"
            )
            ed1_tok, _, _ = await _register_user(
                c, email="ed1@org.com", display_name="Editor 1"
            )
            ed2_tok, _, _ = await _register_user(
                c, email="ed2@org.com", display_name="Editor 2"
            )
            mem1_tok, mem1_id, _ = await _register_user(
                c, email="mem1@org.com", display_name="Member 1"
            )
            mem2_tok, _, _ = await _register_user(
                c, email="mem2@org.com", display_name="Member 2"
            )

            org_res = await c.post(
                "/api/v1/workspaces",
                json={"name": "Org Dokumentasi"},
                headers={"Authorization": f"Bearer {owner_tok}"},
            )
            assert org_res.status_code == 201
            org_id = UUID(org_res.json()["data"]["id"])

            await _invite_and_accept(
                c,
                owner_token=owner_tok,
                org_ws_id=org_id,
                invitee_token=admin_tok,
                invitee_email="admin@org.com",
                role="admin",
                email_service=email_service,
            )
            await _invite_and_accept(
                c,
                owner_token=owner_tok,
                org_ws_id=org_id,
                invitee_token=ed1_tok,
                invitee_email="ed1@org.com",
                role="editor",
                email_service=email_service,
            )
            await _invite_and_accept(
                c,
                owner_token=owner_tok,
                org_ws_id=org_id,
                invitee_token=ed2_tok,
                invitee_email="ed2@org.com",
                role="editor",
                email_service=email_service,
            )
            await _invite_and_accept(
                c,
                owner_token=owner_tok,
                org_ws_id=org_id,
                invitee_token=mem1_tok,
                invitee_email="mem1@org.com",
                role="member",
                email_service=email_service,
            )
            await _invite_and_accept(
                c,
                owner_token=owner_tok,
                org_ws_id=org_id,
                invitee_token=mem2_tok,
                invitee_email="mem2@org.com",
                role="member",
                email_service=email_service,
            )

            # Member 1 creates doc_m1 with a text source and starts processing
            doc_m1 = (
                await c.post(
                    f"/api/v1/workspaces/{org_id}/documents",
                    json={"title": "Notulen Member 1"},
                    headers={"Authorization": f"Bearer {mem1_tok}"},
                )
            ).json()["data"]["id"]
            assert (
                await c.post(
                    f"/api/v1/workspaces/{org_id}/documents/{doc_m1}/sources",
                    json={"source_type": "text", "raw_text": "Isi catatan Member 1"},
                    headers={"Authorization": f"Bearer {mem1_tok}"},
                )
            ).status_code == 201
            proc_m1 = await c.post(
                f"/api/v1/documents/{doc_m1}/process",
                headers={"Authorization": f"Bearer {mem1_tok}"},
            )
            assert proc_m1.status_code == 202
            job_m1_id = proc_m1.json()["data"]["jobs"][0]["id"]

            # Member 2 creates own doc_m2; list returns ONLY doc_m2; doc_m1 is completely forbidden
            doc_m2 = (
                await c.post(
                    f"/api/v1/workspaces/{org_id}/documents",
                    json={"title": "Notulen Member 2"},
                    headers={"Authorization": f"Bearer {mem2_tok}"},
                )
            ).json()["data"]["id"]
            mem2_list = await c.get(
                f"/api/v1/workspaces/{org_id}/documents",
                headers={"Authorization": f"Bearer {mem2_tok}"},
            )
            assert [item["id"] for item in mem2_list.json()["data"]["items"]] == [doc_m2]
            assert (
                await c.get(
                    f"/api/v1/documents/{doc_m1}",
                    headers={"Authorization": f"Bearer {mem2_tok}"},
                )
            ).status_code == 403
            assert (
                await c.get(
                    f"/api/v1/jobs/{job_m1_id}",
                    headers={"Authorization": f"Bearer {mem2_tok}"},
                )
            ).status_code == 403

            # Editor 2 has read-only access to doc_m1 and its job, but zero mutation access
            assert (
                await c.get(
                    f"/api/v1/documents/{doc_m1}",
                    headers={"Authorization": f"Bearer {ed2_tok}"},
                )
            ).status_code == 200
            assert (
                await c.get(
                    f"/api/v1/documents/{doc_m1}/sources",
                    headers={"Authorization": f"Bearer {ed2_tok}"},
                )
            ).status_code == 200
            assert (
                await c.get(
                    f"/api/v1/documents/{doc_m1}/versions",
                    headers={"Authorization": f"Bearer {ed2_tok}"},
                )
            ).status_code == 200
            assert (
                await c.get(
                    f"/api/v1/jobs/{job_m1_id}",
                    headers={"Authorization": f"Bearer {ed2_tok}"},
                )
            ).status_code == 200

            # Editor 2 cannot edit, create upload intent, add sources, process, or delete doc_m1
            assert (
                await c.patch(
                    f"/api/v1/documents/{doc_m1}",
                    json={"title": "Hijacked"},
                    headers={"Authorization": f"Bearer {ed2_tok}"},
                )
            ).status_code == 403
            assert (
                await c.post(
                    f"/api/v1/workspaces/{org_id}/documents/{doc_m1}/sources/upload-intents",
                    json={"filename": "a.txt", "content_type": "text/plain", "size_bytes": 10},
                    headers={"Authorization": f"Bearer {ed2_tok}"},
                )
            ).status_code == 403
            assert (
                await c.post(
                    f"/api/v1/workspaces/{org_id}/documents/{doc_m1}/sources",
                    json={"source_type": "text", "raw_text": "forbidden"},
                    headers={"Authorization": f"Bearer {ed2_tok}"},
                )
            ).status_code == 403
            assert (
                await c.post(
                    f"/api/v1/documents/{doc_m1}/process",
                    headers={"Authorization": f"Bearer {ed2_tok}"},
                )
            ).status_code == 403
            assert (
                await c.delete(
                    f"/api/v1/documents/{doc_m1}",
                    headers={"Authorization": f"Bearer {ed2_tok}"},
                )
            ).status_code == 403

            # Admin and Owner have full access to doc_m1 (after job moves to review_required)
            async with session_factory() as session:
                doc_row = await session.scalar(select(Document).where(Document.id == UUID(doc_m1)))
                job_row = await session.scalar(select(Job).where(Job.id == UUID(job_m1_id)))
                assert doc_row is not None and job_row is not None
                job_row.status = "completed"
                doc_row.status = "review_required"
                await session.commit()

            assert (
                await c.patch(
                    f"/api/v1/documents/{doc_m1}",
                    json={"title": "Disunting Admin"},
                    headers={"Authorization": f"Bearer {admin_tok}"},
                )
            ).status_code == 200

            # Removing member1 revokes member1's access WITHOUT deleting doc_m1
            rem_res = await c.delete(
                f"/api/v1/workspaces/{org_id}/members/{mem1_id}",
                headers={"Authorization": f"Bearer {owner_tok}"},
            )
            assert rem_res.status_code == 200
            assert (
                await c.get(
                    f"/api/v1/documents/{doc_m1}",
                    headers={"Authorization": f"Bearer {mem1_tok}"},
                )
            ).status_code == 403
            owner_doc = await c.get(
                f"/api/v1/documents/{doc_m1}",
                headers={"Authorization": f"Bearer {owner_tok}"},
            )
            assert owner_doc.status_code == 200
            assert owner_doc.json()["data"]["title"] == "Disunting Admin"
    finally:
        storage.clear()
        await engine.dispose()


async def test_cross_tenant_idor_and_personal_template_restriction(tmp_path: Path) -> None:
    """Deny cross-workspace access to documents/jobs and reject org templates in personal ws."""
    engine, app, _, _, _ = _build_document_test_app(tmp_path, db_name="m5_idor.sqlite")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            tok_a, _, ws_a = await _register_user(c, email="a@example.com", display_name="User A")
            tok_b, _, ws_b = await _register_user(c, email="b@example.com", display_name="User B")

            doc_a = (
                await c.post(
                    f"/api/v1/workspaces/{ws_a}/documents",
                    json={"title": "Rahasia Workspace A"},
                    headers={"Authorization": f"Bearer {tok_a}"},
                )
            ).json()["data"]["id"]
            assert (
                await c.post(
                    f"/api/v1/workspaces/{ws_a}/documents/{doc_a}/sources",
                    json={"source_type": "text", "raw_text": "Data sensitif A"},
                    headers={"Authorization": f"Bearer {tok_a}"},
                )
            ).status_code == 201
            job_a = (
                await c.post(
                    f"/api/v1/documents/{doc_a}/process",
                    headers={"Authorization": f"Bearer {tok_a}"},
                )
            ).json()["data"]["jobs"][0]["id"]

            # User B attacks across workspace boundary
            for method, path, body in (
                ("GET", f"/api/v1/workspaces/{ws_a}/documents", None),
                ("GET", f"/api/v1/documents/{doc_a}", None),
                ("PATCH", f"/api/v1/documents/{doc_a}", {"title": "Pwned"}),
                ("DELETE", f"/api/v1/documents/{doc_a}", None),
                ("GET", f"/api/v1/documents/{doc_a}/sources", None),
                ("GET", f"/api/v1/documents/{doc_a}/versions", None),
                ("GET", f"/api/v1/jobs/{job_a}", None),
                (
                    "POST",
                    f"/api/v1/workspaces/{ws_a}/documents/{doc_a}/sources",
                    {"source_type": "text", "raw_text": "hijack"},
                ),
                # Manipulate path workspace_id to attacker's own ws_b with victim doc_a
                (
                    "POST",
                    f"/api/v1/workspaces/{ws_b}/documents/{doc_a}/sources",
                    {"source_type": "text", "raw_text": "hijack"},
                ),
            ):
                r = await c.request(
                    method,
                    path,
                    json=body,
                    headers={"Authorization": f"Bearer {tok_b}"},
                )
                assert r.status_code in (403, 404)
                assert "Rahasia Workspace A" not in r.text

            # Org-scoped template version cannot be selected in a Personal Workspace or other ws
            org_res = await c.post(
                "/api/v1/workspaces",
                json={"name": "Org Template Owner"},
                headers={"Authorization": f"Bearer {tok_a}"},
            )
            org_ws_id = UUID(org_res.json()["data"]["id"])
            async with session_factory() as session:
                tpl = Template(
                    workspace_id=org_ws_id,
                    name="Org Minutes",
                    document_type="meeting_minutes",
                    is_builtin=False,
                    status="active",
                )
                session.add(tpl)
                await session.flush()
                tpl_ver = TemplateVersion(
                    template_id=tpl.id,
                    version_number=1,
                    storage_object_key=f"workspaces/{org_ws_id}/templates/{tpl.id}/v1.docx",
                    content_hash="a" * 64,
                    validation_status="valid",
                )
                session.add(tpl_ver)
                await session.commit()
                org_tpl_ver_id = str(tpl_ver.id)

            bad_personal_tpl = await c.post(
                f"/api/v1/workspaces/{ws_a}/documents",
                json={"title": "Pakai Templat Org", "selected_template_version_id": org_tpl_ver_id},
                headers={"Authorization": f"Bearer {tok_a}"},
            )
            assert bad_personal_tpl.status_code in (403, 404)
    finally:
        await engine.dispose()


async def test_docx_benign_real_world_features_and_strict_external_boundary(
    tmp_path: Path,
) -> None:
    """Accept safe hyperlinks, printerSettings.bin, image.json.png, and small high-ratio DOCX."""
    engine, app, _, storage, _ = _build_document_test_app(
        tmp_path,
        db_name="m5_benign_docx.sqlite",
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    def build_rels_xml(entries: list[tuple[str, str, str]]) -> bytes:
        rels = "".join(
            f'<Relationship Id="rId{i}" Type="{rel_type}" '
            f'Target="{target}" TargetMode="{mode}"/>'
            for i, (rel_type, target, mode) in enumerate(entries, start=1)
        )
        return (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<Relationships xmlns="http://schemas.openxmlformats.org/'
            b'package/2006/relationships">'
            + rels.encode("utf-8")
            + b"</Relationships>"
        )

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            token, _, ws_id = await _register_user(
                c, email="benign@example.com", display_name="Benign"
            )
            headers = {"Authorization": f"Bearer {token}"}

            doc_id = (
                await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents",
                    json={"title": "Dokumen Word Nyata"},
                    headers=headers,
                )
            ).json()["data"]["id"]

            async def upload_docx(filename: str, data: bytes) -> tuple[int, str]:
                ir = await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents/{doc_id}/sources/upload-intents",
                    json={"filename": filename, "content_type": DOCX_MIME, "size_bytes": len(data)},
                    headers=headers,
                )
                assert ir.status_code == 201
                key = ir.json()["data"]["object_key"]
                storage.put_object(object_key=key, content=data, content_type=DOCX_MIME)
                sr = await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents/{doc_id}/sources",
                    json={
                        "source_type": "upload",
                        "object_key": key,
                        "filename": filename,
                        "content_type": DOCX_MIME,
                    },
                    headers=headers,
                )
                code = sr.json()["error"]["code"] if not sr.json()["success"] else "OK"
                return sr.status_code, code

            # 1. HTTPS hyperlink in document.xml.rels (never fetched, accepted)
            https_docx = _make_docx_bytes(
                extra_entries={
                    "word/_rels/document.xml.rels": build_rels_xml(
                        [(HYPERLINK_REL, "https://example.invalid/notulen?id=42", "External")]
                    )
                }
            )
            assert await upload_docx("https_link.docx", https_docx) == (201, "OK")

            # 2. mailto hyperlink in document.xml.rels (never fetched, accepted)
            mailto_docx = _make_docx_bytes(
                extra_entries={
                    "word/_rels/document.xml.rels": build_rels_xml(
                        [
                            (
                                HYPERLINK_REL,
                                "mailto:sekretariat@example.invalid?subject=Notulen",
                                "External",
                            )
                        ]
                    )
                }
            )
            assert await upload_docx("mailto_link.docx", mailto_docx) == (201, "OK")

            # 3. Legitimate word/printerSettings/printerSettings1.bin
            printer_ct = (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                b'<Default Extension="rels" '
                b'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                b'<Default Extension="xml" ContentType="application/xml"/>'
                + f'<Default Extension="bin" ContentType="{PRINTER_SETTINGS_MIME}"/>'.encode()
                + b"</Types>"
            )
            valid_printer_docx = _make_docx_bytes(
                content_types_xml=printer_ct,
                extra_entries={"word/printerSettings/printerSettings1.bin": b"\x01\x00DEVMODE\x00"},
            )
            assert await upload_docx("printer.docx", valid_printer_docx) == (201, "OK")

            # 4. Legitimate media asset whose filename contains ".js" substring (`image.json.png`)
            media_json_png = _make_docx_bytes(
                extra_entries={"word/media/image.json.png": b"\x89PNG\r\n\x1a\n"}
            )
            assert await upload_docx("media_name.docx", media_json_png) == (201, "OK")

            # 5. Small legitimate high-ratio DOCX (>15:1 ratio, ~28 KB decompressed <= 64 KB floor)
            repeating_para = ("<w:p><w:r><w:t>" + ("Notulen " * 20) + "</w:t></w:r></w:p>") * 140
            small_high_ratio_xml = (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                b"<w:body>"
                + repeating_para.encode("utf-8")
                + b"</w:body></w:document>"
            )
            small_high_ratio_docx = _make_docx_bytes(document_xml=small_high_ratio_xml)
            assert len(small_high_ratio_docx) < 2500
            assert await upload_docx("small_clean.docx", small_high_ratio_docx) == (201, "OK")

            # Upload cap hit at 5 -> create a second doc for negative regression assertions
            doc2_id = (
                await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents",
                    json={"title": "Dokumen Negatif"},
                    headers=headers,
                )
            ).json()["data"]["id"]

            async def upload_neg(filename: str, data: bytes) -> tuple[int, str]:
                ir = await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents/{doc2_id}/sources/upload-intents",
                    json={"filename": filename, "content_type": DOCX_MIME, "size_bytes": len(data)},
                    headers=headers,
                )
                assert ir.status_code == 201
                key = ir.json()["data"]["object_key"]
                storage.put_object(object_key=key, content=data, content_type=DOCX_MIME)
                sr = await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents/{doc2_id}/sources",
                    json={
                        "source_type": "upload",
                        "object_key": key,
                        "filename": filename,
                        "content_type": DOCX_MIME,
                    },
                    headers=headers,
                )
                return sr.status_code, sr.json()["error"]["code"]

            # 6. Non-hyperlink external relationships and dangerous protocols stay rejected
            attached_tpl_type = (
                "http://schemas.openxmlformats.org/officeDocument/2006/"
                "relationships/attachedTemplate"
            )
            for bad_rel in (
                (attached_tpl_type, "https://evil.invalid/template.dotm", "External"),
                (HYPERLINK_REL, "file:///etc/passwd", "External"),
                (HYPERLINK_REL, "javascript:alert(1)", "External"),
                (HYPERLINK_REL, "\\\\evil.invalid\\share\\doc", "External"),
            ):
                bad_ext_docx = _make_docx_bytes(
                    extra_entries={"word/_rels/document.xml.rels": build_rels_xml([bad_rel])}
                )
                assert await upload_neg("bad_ext.docx", bad_ext_docx) == (422, "UPLOAD_REJECTED")

            # Spoofed OLE2 payload inside printerSettings1.bin or unauthorized .bin/.js paths
            spoofed_printer = _make_docx_bytes(
                content_types_xml=printer_ct,
                extra_entries={
                    "word/printerSettings/printerSettings1.bin": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
                },
            )
            assert await upload_neg("spoof_printer.docx", spoofed_printer) == (
                422,
                "UPLOAD_REJECTED",
            )
            assert await upload_neg(
                "script.docx",
                _make_docx_bytes(extra_entries={"word/evil.js": b"alert(1)"}),
            ) == (422, "UPLOAD_REJECTED")
            assert await upload_neg(
                "vba.docx",
                _make_docx_bytes(extra_entries={"word/vbaProject.bin": b"\x00\x01"}),
            ) == (422, "UPLOAD_REJECTED")

            # 7. Small ZIP bomb exceeding 64 KB floor remains rejected under default limits
            small_bomb_docx = _make_docx_bytes(
                extra_entries={"word/bomb.txt": b"A" * 120_000}
            )
            assert len(small_bomb_docx) < 2500
            assert await upload_neg("small_bomb.docx", small_bomb_docx) == (
                422,
                "UPLOAD_REJECTED",
            )
    finally:
        storage.clear()
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
def pg_document_db_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Start an ephemeral PostgreSQL 16 cluster, run migrations, and yield its async URL."""
    pg_bin = Path("/usr/lib/postgresql/16/bin")
    initdb_bin = pg_bin / "initdb"
    pg_ctl_bin = pg_bin / "pg_ctl"
    if not initdb_bin.exists() or not pg_ctl_bin.exists():
        pytest.skip("PostgreSQL 16 binaries not installed locally")

    pg_root = tmp_path_factory.mktemp("pg16_document_concurrency")
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
        test_db_url = f"postgresql+asyncpg://postgres@127.0.0.1:{port}/doc_saas_m5_test"
        asyncio.run(_create_pg_database(admin_url, "doc_saas_m5_test"))

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


async def test_concurrent_process_same_idempotency_key_across_two_documents_on_pg(
    pg_document_db_url: str,
) -> None:
    """Concurrent /process with shared idempotency key on 2 docs returns one 202 and one 409."""
    settings = Settings(
        environment="test",
        database_url=pg_document_db_url,
        internal_hmac_secret="pg-m5-concurrency-hmac-secret-minimum-32-chars",
        jwt_secret_key="pg-m5-concurrency-jwt-secret-minimum-32-chars",
        beta_complimentary_credits=10,
        ai_generation_credit_cost=2,
        rate_limit_register_ip_max=100,
    )
    engine = create_engine(pg_document_db_url)
    session_factory = create_session_factory(engine)
    email_service = StubEmailService()
    storage = InMemoryObjectStorage()
    rate_limiter = SlidingWindowRateLimiter()

    async def override_get_db_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    app = create_app(settings)
    app.dependency_overrides[get_db_session] = override_get_db_session
    app.dependency_overrides[get_email_service] = lambda: email_service
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_rate_limiter] = lambda: rate_limiter

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            token, _, ws_id = await _register_user(
                c, email="pg-m5-idem@example.com", display_name="PG Idem"
            )
            headers = {"Authorization": f"Bearer {token}"}

            d1 = (
                await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents",
                    json={"title": "Dokumen PG 1"},
                    headers=headers,
                )
            ).json()["data"]["id"]
            d2 = (
                await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents",
                    json={"title": "Dokumen PG 2"},
                    headers=headers,
                )
            ).json()["data"]["id"]

            for did in (d1, d2):
                src_res = await c.post(
                    f"/api/v1/workspaces/{ws_id}/documents/{did}/sources",
                    json={"source_type": "text", "raw_text": f"Sumber teks untuk {did}"},
                    headers=headers,
                )
                assert src_res.status_code == 201

            shared_key = "shared-pg-idem-key"

            async def call_process(target_doc_id: str) -> tuple[str, int, dict[str, Any]]:
                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://testserver"
                ) as caller:
                    res = await caller.post(
                        f"/api/v1/documents/{target_doc_id}/process",
                        json={"idempotency_key": shared_key},
                        headers=headers,
                    )
                    return target_doc_id, res.status_code, res.json()

            res_1, res_2 = await asyncio.gather(call_process(d1), call_process(d2))
            results = [res_1, res_2]
            winners = [r for r in results if r[1] == 202]
            conflicts = [r for r in results if r[1] == 409]

            assert len(winners) == 1
            assert len(conflicts) == 1
            winner_id, _, winner_body = winners[0]
            loser_id, _, loser_body = conflicts[0]
            assert loser_body["error"]["code"] == "IDEMPOTENCY_CONFLICT"

            # Replaying the exact same key on the winning document remains idempotent (202).
            replay = await c.post(
                f"/api/v1/documents/{winner_id}/process",
                json={"idempotency_key": shared_key},
                headers=headers,
            )
            assert replay.status_code == 202
            assert replay.json()["data"]["source_bundle_id"] == (
                winner_body["data"]["source_bundle_id"]
            )
            assert replay.json()["data"]["reservation_id"] == (
                winner_body["data"]["reservation_id"]
            )

            # Losing document remains draft with zero jobs.
            loser_detail = await c.get(f"/api/v1/documents/{loser_id}", headers=headers)
            assert loser_detail.status_code == 200
            assert loser_detail.json()["data"]["status"] == "draft"
            assert loser_detail.json()["data"]["jobs"] == []

        # Verify DB consistency in PostgreSQL 16: single job, single reservation, 8 available/2 res.
        async with session_factory() as session:
            job_count = await session.scalar(
                select(func.count(Job.id)).where(Job.workspace_id == ws_id)
            )
            res_count = await session.scalar(
                select(func.count(CreditReservation.id)).where(
                    CreditReservation.workspace_id == ws_id
                )
            )
            wallet = await session.scalar(
                select(CreditWallet).where(CreditWallet.workspace_id == ws_id)
            )
            assert job_count == 1
            assert res_count == 1
            assert wallet is not None
            assert wallet.available_balance == 8
            assert wallet.reserved_balance == 2
    finally:
        await engine.dispose()


