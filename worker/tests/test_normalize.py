"""Tests for Python Worker isolated normalization and HMAC authentication."""

import base64
import io
import json
import time
import zipfile
import zlib
from uuid import uuid4

import httpx
from httpx import ASGITransport, AsyncClient

from worker_app.core.config import WorkerSettings
from worker_app.core.internal_auth import sign_internal_request, verify_internal_request
from worker_app.main import create_worker_app

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _make_docx(
    *, extra_xml: bytes | None = None, extra_files: dict[str, bytes] | None = None
) -> bytes:
    buf = io.BytesIO()
    ct = (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Default Extension="rels" '
        b'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b"</Types>"
    )
    doc = extra_xml or (
        b'<?xml version="1.0" encoding="UTF-8"?>'
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        b"<w:body>"
        b"<w:p><w:r><w:t>Keputu</w:t></w:r><w:r><w:t>san: Rilis M6</w:t></w:r></w:p>"
        b"</w:body></w:document>"
    )
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", ct)
        zf.writestr("word/document.xml", doc)
        if extra_files:
            for k, v in extra_files.items():
                zf.writestr(k, v)
    return buf.getvalue()


async def test_worker_normalize_docx_pdf_txt_and_rejects_malicious_inputs() -> None:
    """Worker normalizes valid DOCX/PDF/TXT over HMAC-auth HTTP without DB access."""
    secret = "worker-normalize-test-hmac-secret-minimum-32-chars"
    settings = WorkerSettings(environment="test", internal_hmac_secret=secret)
    app = create_worker_app(settings)

    docx_bytes = _make_docx()
    pdf_bytes = b"%PDF-1.4\nBT (Usulan: Tambah QA) Tj ET\n%%EOF\n"
    txt_bytes = b"Peserta: Sakti, Budi"

    job_id = str(uuid4())
    src1, src2, src3 = str(uuid4()), str(uuid4()), str(uuid4())
    payload = {
        "job_id": job_id,
        "sources": [
            {
                "source_id": src1,
                "filename": "notes.docx",
                "content_type": DOCX_MIME,
                "content_base64": base64.b64encode(docx_bytes).decode("ascii"),
            },
            {
                "source_id": src2,
                "filename": "lampiran.pdf",
                "content_type": "application/pdf",
                "content_base64": base64.b64encode(pdf_bytes).decode("ascii"),
            },
            {
                "source_id": src3,
                "filename": "chat.txt",
                "content_type": "text/plain",
                "content_base64": base64.b64encode(txt_bytes).decode("ascii"),
            },
        ],
    }
    raw_body = json.dumps(payload).encode("utf-8")
    ts = int(time.time())
    event_id = "wrk-evt-1"
    sig = sign_internal_request(
        secret=secret,
        timestamp=ts,
        event_id=event_id,
        method="POST",
        canonical_path="/api/v1/internal/normalize",
        raw_body=raw_body,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://worker") as c:
        # Unauthenticated rejected
        unauth = await c.post("/api/v1/internal/normalize", content=raw_body)
        assert unauth.status_code == 401

        res = await c.post(
            "/api/v1/internal/normalize",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Internal-Timestamp": str(ts),
                "X-Internal-Event-Id": event_id,
                "X-Internal-Signature": sig,
            },
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["event_type"] == "completed"
        items = {item["source_id"]: item for item in data["normalized_sources"]}
        assert "Keputusan: Rilis M6" in items[src1]["normalized_text"]
        assert "Usulan: Tambah QA" in items[src2]["normalized_text"]
        assert items[src3]["normalized_text"] == "Peserta: Sakti, Budi"

        # XXE payload yields failed callback payload
        xxe_docx = _make_docx(
            extra_xml=b'<?xml version="1.0"?><!DOCTYPE foo><w:document/>',
        )
        bad_body = json.dumps(
            {
                "job_id": job_id,
                "sources": [
                    {
                        "source_id": src1,
                        "filename": "xxe.docx",
                        "content_type": DOCX_MIME,
                        "content_base64": base64.b64encode(xxe_docx).decode("ascii"),
                    }
                ],
            }
        ).encode("utf-8")
        bad_sig = sign_internal_request(
            secret=secret,
            timestamp=ts,
            event_id="wrk-evt-2",
            method="POST",
            canonical_path="/api/v1/internal/normalize",
            raw_body=bad_body,
        )
        bad_res = await c.post(
            "/api/v1/internal/normalize",
            content=bad_body,
            headers={
                "Content-Type": "application/json",
                "X-Internal-Timestamp": str(ts),
                "X-Internal-Event-Id": "wrk-evt-2",
                "X-Internal-Signature": bad_sig,
            },
        )
        assert bad_res.status_code == 200
        assert bad_res.json()["data"]["event_type"] == "failed"
        assert bad_res.json()["data"]["error_code"] == "UPLOAD_REJECTED"


def _make_flate_pdf(streams: list[bytes]) -> bytes:
    """Build a valid multi-stream PDF with FlateDecode compressed content streams."""
    chunks: list[bytes] = [b"%PDF-1.4\n1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"]
    obj_num = 3
    for raw in streams:
        compressed = zlib.compress(raw)
        header = (
            f"{obj_num} 0 obj << /Length {len(compressed)} /Filter /FlateDecode >>\nstream\n"
        ).encode("ascii")
        chunks.append(header + compressed + b"\nendstream\nendobj\n")
        obj_num += 1
    chunks.append(b"%%EOF\n")
    return b"".join(chunks)


async def test_worker_rejects_docx_macro_content_type_and_handles_flate_pdf_and_callback() -> None:
    """Test DOCX macro ContentType check, FlateDecode TJ/Tj PDF, and callback signing."""
    secret = "worker-stage-c-test-hmac-secret-minimum-32-chars"
    settings = WorkerSettings(
        environment="test",
        internal_hmac_secret=secret,
        backend_internal_base_url="http://backend-internal",
    )
    app = create_worker_app(settings)

    delivered_callbacks: list[httpx.Request] = []

    async def callback_handler(request: httpx.Request) -> httpx.Response:
        delivered_callbacks.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "event_id": request.headers["X-Internal-Event-Id"],
                    "job_id": json.loads(request.content.decode("utf-8"))["job_id"],
                    "job_status": "completed",
                    "document_status": "processing",
                    "result_status": "processed",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(callback_handler)) as cb_client:
        app.state.callback_http_client = cb_client

        # 1. Compressed FlateDecode PDF with TJ arrays, octal/paren escapes, and multiple streams
        pdf_stream_1 = (
            b"BT /F1 12 Tf 72 720 Td "
            b"[(Keputu) -10 (san:) -250 (Setujui) -250 (Rilis)] TJ "
            b"T* (Peserta: Sakti, Rina) Tj ET"
        )
        pdf_stream_2 = b"BT 72 700 Td (Tindak lanjut: Kirim notulen \\(Q4\\)) Tj ET"
        flate_pdf = _make_flate_pdf([pdf_stream_1, pdf_stream_2])

        job_id = str(uuid4())
        pdf_src_id = str(uuid4())
        req_body = json.dumps(
            {
                "job_id": job_id,
                "deliver_callback": True,
                "sources": [
                    {
                        "source_id": pdf_src_id,
                        "filename": "compressed_minutes.pdf",
                        "content_type": "application/pdf",
                        "content_base64": base64.b64encode(flate_pdf).decode("ascii"),
                    }
                ],
            }
        ).encode("utf-8")
        ts = int(time.time())
        sig = sign_internal_request(
            secret=secret,
            timestamp=ts,
            event_id="wrk-flate-1",
            method="POST",
            canonical_path="/api/v1/internal/normalize",
            raw_body=req_body,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://worker") as c:
            res = await c.post(
                "/api/v1/internal/normalize",
                content=req_body,
                headers={
                    "Content-Type": "application/json",
                    "X-Internal-Timestamp": str(ts),
                    "X-Internal-Event-Id": "wrk-flate-1",
                    "X-Internal-Signature": sig,
                },
            )
            assert res.status_code == 200
            body = res.json()
            assert body["data"]["event_type"] == "completed"
            assert body["callback_delivered"] is True
            extracted = body["data"]["normalized_sources"][0]["normalized_text"]
            assert "Keputusan: Setujui Rilis" in extracted
            assert "Peserta: Sakti, Rina" in extracted
            assert "Tindak lanjut: Kirim notulen (Q4)" in extracted
            assert body["data"]["normalized_sources"][0]["metadata"]["text_stream_count"] == 2

            # Verify delivered callback HTTP request was signed with build_worker_callback_headers
            assert len(delivered_callbacks) == 1
            cb_req = delivered_callbacks[0]
            assert cb_req.url.path == "/api/v1/internal/workers/callback"
            verify_internal_request(
                secret=secret,
                provided_signature=cb_req.headers["X-Internal-Signature"],
                timestamp=int(cb_req.headers["X-Internal-Timestamp"]),
                event_id=cb_req.headers["X-Internal-Event-Id"],
                method="POST",
                canonical_path="/api/v1/internal/workers/callback",
                raw_body=cb_req.content,
            )

            # 2. DOCX with macroEnabled ContentType in [Content_Types].xml rejected (Stage C1)
            macro_ct_buf = io.BytesIO()
            macro_ct_xml = (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                b'<Override PartName="/word/document.xml" '
                b'ContentType="application/vnd.ms-word.document.macroEnabled.main+xml"/>'
                b"</Types>"
            )
            with zipfile.ZipFile(macro_ct_buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("[Content_Types].xml", macro_ct_xml)
                zf.writestr("word/document.xml", b"<w:document/>")

            # 3. Corrupt FlateDecode PDF rejected safely (Stage C2)
            corrupt_pdf = (
                b"%PDF-1.4\n1 0 obj << /Length 12 /Filter /FlateDecode >>\nstream\n"
                b"not-valid-zlib\nendstream\nendobj\n%%EOF\n"
            )
            # 4. Hidden /JavaScript inside compressed FlateDecode stream rejected
            hidden_js_pdf = _make_flate_pdf([b"1 0 obj << /JavaScript (app.alert(1)) >> endobj"])

            for idx, (fname, mime, bad_bytes) in enumerate(
                [
                    ("macro_ct.docx", DOCX_MIME, macro_ct_buf.getvalue()),
                    ("corrupt.pdf", "application/pdf", corrupt_pdf),
                    ("hidden_js.pdf", "application/pdf", hidden_js_pdf),
                ]
            ):
                bad_payload = json.dumps(
                    {
                        "job_id": job_id,
                        "deliver_callback": True,
                        "sources": [
                            {
                                "source_id": pdf_src_id,
                                "filename": fname,
                                "content_type": mime,
                                "content_base64": base64.b64encode(bad_bytes).decode("ascii"),
                            }
                        ],
                    }
                ).encode("utf-8")
                bad_sig = sign_internal_request(
                    secret=secret,
                    timestamp=ts,
                    event_id=f"wrk-bad-{idx}",
                    method="POST",
                    canonical_path="/api/v1/internal/normalize",
                    raw_body=bad_payload,
                )
                bad_res = await c.post(
                    "/api/v1/internal/normalize",
                    content=bad_payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Internal-Timestamp": str(ts),
                        "X-Internal-Event-Id": f"wrk-bad-{idx}",
                        "X-Internal-Signature": bad_sig,
                    },
                )
                assert bad_res.status_code == 200
                assert bad_res.json()["data"]["event_type"] == "failed"
                assert bad_res.json()["data"]["error_code"] == "UPLOAD_REJECTED"
                assert bad_res.json()["callback_delivered"] is True
