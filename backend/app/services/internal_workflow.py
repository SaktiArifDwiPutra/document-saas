"""Internal signed source-bundle retrieval, worker/workflow callbacks, and usage service (M6)."""

import base64
import hashlib
import json
import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, NoReturn, cast
from uuid import UUID, uuid4

import httpx
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.core.file_validation import validate_uploaded_content
from app.core.internal_auth import (
    InMemoryReplayGuard,
    InternalAuthError,
    compute_internal_hmac,
    verify_internal_hmac,
)
from app.core.permissions import WorkspaceRole, WorkspaceType, require_workspace_action
from app.core.security import ensure_utc
from app.models.auth import User
from app.models.credit import CreditReservation
from app.models.document import (
    Document,
    DocumentSource,
    DocumentVersion,
    Job,
    JobAttempt,
    UsageEvent,
    WorkflowCallbackEvent,
)
from app.orchestration.base import (
    WorkflowDispatchRequest,
    WorkflowInputReference,
    WorkflowOrchestrator,
)
from app.orchestration.n8n_adapter import N8NWorkflowAdapter
from app.orchestration.python_adapter import PythonWorkflowAdapter
from app.schemas.ai import (
    InternalCallbackAckResponse,
    MeetingMinutesDraftContent,
    NormalizedSourceItem,
    ProviderUsageMetadata,
    SourceBundleItem,
    SourceBundleResponse,
    UsageEventResponse,
    UsageListResponse,
    UsageSummaryResponse,
    WorkerCallbackRequest,
    WorkflowCallbackRequest,
)
from app.schemas.document import DocumentStatus, DocumentType, JobStatus, SourceType
from app.services.credit import CreditService
from app.services.document import ACTIVE_JOB_STATUSES
from app.services.gemini import GeminiProvider
from app.services.storage import ObjectStorageService
from app.services.workspace_access import (
    record_workspace_audit,
    require_active_workspace_membership,
)

_SUPPORTED_AI_WORKFLOW_KEYS = frozenset({"document_generation_v1", "meeting_minutes_v1"})
_WORD_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")
_PROVENANCE_STOPWORDS = frozenset(
    {
        "dan",
        "atau",
        "yang",
        "dari",
        "untuk",
        "pada",
        "oleh",
        "dengan",
        "bahwa",
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "are",
        "was",
    }
)


def _hash_canonical_json(payload: dict[str, object]) -> str:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _normalize_grounding_text(value: str) -> str:
    return " ".join(value.lower().split())


def _build_source_corpus(source: DocumentSource) -> str:
    parts: list[str] = []
    if source.extracted_text_reference:
        parts.append(source.extracted_text_reference)
    form_data = source.metadata_json.get("form_data")
    if isinstance(form_data, dict):
        for key, val in form_data.items():
            if isinstance(val, str) and val.strip():
                parts.append(f"{key}: {val.strip()}")
            elif isinstance(val, list):
                cleaned = [item.strip() for item in val if isinstance(item, str) and item.strip()]
                if cleaned:
                    parts.append(f"{key}: {', '.join(cleaned)}")
                    parts.append(f"{key}: {'; '.join(cleaned)}")
    return _normalize_grounding_text(" \n ".join(parts))


def _is_excerpt_grounded(excerpt: str, source_corpus: str) -> bool:
    norm_excerpt = _normalize_grounding_text(excerpt)
    if len(norm_excerpt) < 2 or not source_corpus:
        return False
    return norm_excerpt in source_corpus


def _is_claim_grounded(claim: str, source_corpus: str, excerpt: str) -> bool:
    norm_claim = _normalize_grounding_text(claim)
    norm_excerpt = _normalize_grounding_text(excerpt)
    if not norm_claim or not source_corpus:
        return False
    if norm_claim in source_corpus or norm_claim in norm_excerpt:
        return True
    claim_tokens = [
        tok for tok in _WORD_TOKEN_RE.findall(norm_claim) if tok not in _PROVENANCE_STOPWORDS
    ]
    if not claim_tokens:
        return norm_claim in source_corpus
    corpus_tokens = set(_WORD_TOKEN_RE.findall(source_corpus))
    matched = sum(1 for tok in claim_tokens if tok in corpus_tokens)
    return matched >= max(1, (len(claim_tokens) + 1) // 2)


def _reject_provenance(message: str) -> NoReturn:
    raise AppError(
        code="OUTPUT_SCHEMA_INVALID",
        message=message,
        status_code=422,
    )


def _validate_draft_provenance(
    draft: MeetingMinutesDraftContent,
    valid_sources: Sequence[DocumentSource],
) -> None:
    """Verify source IDs, excerpt grounding, and required provenance entries against bundle."""
    source_corpus_by_id: dict[UUID, str] = {
        src.id: _build_source_corpus(src) for src in valid_sources
    }

    for entry in draft.provenance:
        corpus = source_corpus_by_id.get(entry.source_id)
        if corpus is None:
            _reject_provenance(
                "Referensi provenance AI menunjuk sumber yang tidak valid untuk dokumen ini."
            )
        if not _is_excerpt_grounded(entry.excerpt, corpus):
            _reject_provenance(
                "Kutipan provenance AI tidak ditemukan pada teks sumber yang direferensikan."
            )

    entity_checks: list[tuple[str, UUID, str, str, str]] = [
        *[
            (
                item.statement,
                item.source_id,
                item.excerpt,
                "Referensi sumber opini AI tidak valid untuk dokumen ini.",
                "Opini AI tidak memiliki kutipan yang terverifikasi pada sumber.",
            )
            for item in draft.opinions
        ],
        *[
            (
                item.proposal,
                item.source_id,
                item.excerpt,
                "Referensi sumber usulan AI tidak valid untuk dokumen ini.",
                "Usulan AI tidak memiliki kutipan yang terverifikasi pada sumber.",
            )
            for item in draft.proposals
        ],
        *[
            (
                item.decision,
                item.source_id,
                item.excerpt,
                "Referensi sumber keputusan AI tidak valid untuk dokumen ini.",
                "Keputusan AI tidak memiliki kutipan yang terverifikasi pada sumber.",
            )
            for item in draft.decisions
        ],
        *[
            (
                item.task,
                item.source_id,
                item.excerpt,
                "Referensi sumber tindak lanjut AI tidak valid untuk dokumen ini.",
                "Tindak lanjut AI tidak memiliki kutipan yang terverifikasi pada sumber.",
            )
            for item in draft.action_items
        ],
    ]
    for claim_text, source_id, excerpt, invalid_ref_msg, ungrounded_msg in entity_checks:
        corpus = source_corpus_by_id.get(source_id)
        if corpus is None:
            _reject_provenance(invalid_ref_msg)
        if not _is_excerpt_grounded(excerpt, corpus) or not _is_claim_grounded(
            claim_text, corpus, excerpt
        ):
            _reject_provenance(ungrounded_msg)

    for conflict in draft.conflicts:
        if any(sid not in source_corpus_by_id for sid in conflict.source_ids):
            _reject_provenance("Referensi sumber konflik AI tidak valid untuk dokumen ini.")

    has_critical_items = any(
        (
            bool(draft.decisions),
            bool(draft.action_items),
            bool(draft.opinions),
            bool(draft.proposals),
            bool(draft.attendees),
            bool(draft.agenda_items),
            draft.meeting_title is not None,
            draft.meeting_date is not None,
        )
    )
    if has_critical_items and not draft.provenance:
        _reject_provenance(
            "Hasil ekstraksi AI wajib menyertakan daftar provenance yang terverifikasi."
        )

    prov_fields = {entry.field_path for entry in draft.provenance}
    if draft.decisions and not any(
        fp == "decisions" or fp.startswith("decisions") for fp in prov_fields
    ):
        _reject_provenance("Keputusan hasil AI wajib memiliki entri provenance untuk 'decisions'.")
    if draft.action_items and not any(
        fp == "action_items" or fp.startswith("action_items") for fp in prov_fields
    ):
        _reject_provenance(
            "Tindak lanjut hasil AI wajib memiliki entri provenance untuk 'action_items'."
        )
    if draft.opinions and not any(
        fp == "opinions" or fp.startswith("opinions") for fp in prov_fields
    ):
        _reject_provenance("Opini hasil AI wajib memiliki entri provenance untuk 'opinions'.")
    if draft.proposals and not any(
        fp == "proposals" or fp.startswith("proposals") for fp in prov_fields
    ):
        _reject_provenance("Usulan hasil AI wajib memiliki entri provenance untuk 'proposals'.")


def _to_usage_response(event: UsageEvent) -> UsageEventResponse:
    return UsageEventResponse(
        id=event.id,
        workspace_id=event.workspace_id,
        user_id=event.user_id,
        document_id=event.document_id,
        job_id=event.job_id,
        provider=event.provider,
        model=event.model,
        workflow_key=event.workflow_key,
        input_tokens=event.input_tokens,
        output_tokens=event.output_tokens,
        estimated_cost_minor_units=event.estimated_cost_minor_units,
        duration_ms=event.duration_ms,
        status=cast(Literal["completed", "failed"], event.status),
        created_at=ensure_utc(event.created_at),
    )


class InternalWorkflowService:
    """FastAPI authoritative handler for signed internal endpoints and telemetry."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        replay_guard: InMemoryReplayGuard,
    ) -> None:
        self._session = session
        self._settings = settings
        self._replay_guard = replay_guard

    def _verify_signed_headers(
        self,
        *,
        method: str,
        canonical_path: str,
        raw_body: bytes,
        timestamp_header: str | None,
        event_id_header: str | None,
        signature_header: str | None,
        use_replay_guard: bool,
    ) -> tuple[int, str]:
        if len(raw_body) > self._settings.max_callback_payload_bytes:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Ukuran payload internal melebihi batas maksimum.",
                status_code=413,
            )
        if not timestamp_header or not event_id_header or not signature_header:
            raise AppError(
                code="CALLBACK_INVALID",
                message="Header autentikasi internal tidak lengkap.",
                status_code=401,
            )
        try:
            ts = int(timestamp_header.strip())
        except ValueError as exc:
            raise AppError(
                code="CALLBACK_INVALID",
                message="Timestamp internal tidak valid.",
                status_code=401,
            ) from exc

        cleaned_event_id = event_id_header.strip()
        try:
            verify_internal_hmac(
                secret=self._settings.internal_hmac_secret,
                provided_signature=signature_header,
                timestamp=ts,
                event_id=cleaned_event_id,
                method=method,
                canonical_path=canonical_path,
                raw_body=raw_body,
                tolerance_seconds=self._settings.internal_hmac_timestamp_tolerance_seconds,
                replay_guard=self._replay_guard if use_replay_guard else None,
            )
        except InternalAuthError as exc:
            raise AppError(
                code="CALLBACK_INVALID",
                message="Autentikasi HMAC internal gagal atau kedaluwarsa.",
                status_code=401,
            ) from exc

        return ts, cleaned_event_id

    async def get_source_bundle(
        self,
        *,
        source_bundle_id: UUID,
        canonical_path: str,
        timestamp_header: str | None,
        event_id_header: str | None,
        signature_header: str | None,
        job_id_header: str | None = None,
    ) -> SourceBundleResponse:
        """Serve workspace-bound normalized sources to orchestrators after signed verification."""
        self._verify_signed_headers(
            method="GET",
            canonical_path=canonical_path,
            raw_body=b"",
            timestamp_header=timestamp_header,
            event_id_header=event_id_header,
            signature_header=signature_header,
            use_replay_guard=True,
        )

        ai_job = await self._session.scalar(
            select(Job).where(
                Job.source_bundle_id == source_bundle_id,
                Job.job_type == "ai_generation",
            )
        )
        if ai_job is None:
            raise AppError(
                code="NOT_FOUND",
                message="Bundel sumber dokumen tidak ditemukan.",
                status_code=404,
            )

        if job_id_header is not None:
            try:
                expected_job_id = UUID(job_id_header.strip())
            except ValueError as exc:
                raise AppError(
                    code="CALLBACK_INVALID",
                    message="Identitas job internal tidak valid.",
                    status_code=403,
                ) from exc
            if expected_job_id != ai_job.id:
                raise AppError(
                    code="CALLBACK_INVALID",
                    message="Bundel sumber tidak sesuai dengan identitas job.",
                    status_code=403,
                )

        if ai_job.status not in ACTIVE_JOB_STATUSES:
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Job AI untuk bundel sumber ini tidak lagi aktif.",
                status_code=409,
            )

        expires_at = ensure_utc(ai_job.created_at) + timedelta(
            seconds=self._settings.source_bundle_ttl_seconds
        )
        if datetime.now(UTC) > expires_at:
            raise AppError(
                code="CALLBACK_INVALID",
                message="Tautan pengambilan bundel sumber telah kedaluwarsa.",
                status_code=410,
            )

        norm_job = await self._session.scalar(
            select(Job).where(
                Job.source_bundle_id == source_bundle_id,
                Job.job_type == "normalize",
            )
        )
        if norm_job is not None and norm_job.status != "completed":
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Normalisasi file sumber untuk bundel ini belum selesai.",
                status_code=409,
            )

        document = await self._session.scalar(
            select(Document).where(
                Document.id == ai_job.document_id,
                Document.workspace_id == ai_job.workspace_id,
                Document.deleted_at.is_(None),
            )
        )
        if document is None:
            raise AppError(
                code="NOT_FOUND",
                message="Dokumen terkait bundel sumber tidak ditemukan.",
                status_code=404,
            )

        sources = (
            await self._session.scalars(
                select(DocumentSource)
                .where(
                    DocumentSource.workspace_id == ai_job.workspace_id,
                    DocumentSource.document_id == ai_job.document_id,
                    DocumentSource.source_bundle_id == source_bundle_id,
                    DocumentSource.validation_status == "valid",
                )
                .order_by(DocumentSource.created_at.asc(), DocumentSource.id.asc())
            )
        ).all()
        if not sources:
            raise AppError(
                code="NOT_FOUND",
                message="Tidak ada sumber valid pada bundel dokumen ini.",
                status_code=404,
            )

        return SourceBundleResponse(
            source_bundle_id=source_bundle_id,
            workspace_id=ai_job.workspace_id,
            document_id=document.id,
            job_id=ai_job.id,
            document_type=cast(DocumentType, document.document_type),
            title=document.title,
            expires_at=expires_at,
            sources=[
                SourceBundleItem(
                    source_id=src.id,
                    source_type=cast(SourceType, src.source_type),
                    source_name=src.source_name,
                    content_hash=src.content_hash,
                    normalized_text=src.extracted_text_reference or "",
                    metadata=dict(src.metadata_json),
                )
                for src in sources
            ],
        )

    async def _next_attempt_number(self, job_id: UUID) -> int:
        max_num = await self._session.scalar(
            select(func.max(JobAttempt.attempt_number)).where(JobAttempt.job_id == job_id)
        )
        return int(max_num or 0) + 1

    async def handle_worker_callback(
        self,
        *,
        raw_body: bytes,
        canonical_path: str,
        timestamp_header: str | None,
        event_id_header: str | None,
        signature_header: str | None,
    ) -> InternalCallbackAckResponse:
        """Process signed Python Worker normalize or docx_render callbacks atomically."""
        _, event_id = self._verify_signed_headers(
            method="POST",
            canonical_path=canonical_path,
            raw_body=raw_body,
            timestamp_header=timestamp_header,
            event_id_header=event_id_header,
            signature_header=signature_header,
            use_replay_guard=False,
        )
        try:
            payload = WorkerCallbackRequest.model_validate_json(raw_body)
        except (ValidationError, ValueError) as exc:
            raise AppError(
                code="CALLBACK_INVALID",
                message="Payload callback worker tidak valid.",
                status_code=422,
            ) from exc

        payload_hash = hashlib.sha256(raw_body).hexdigest()
        existing_evt = await self._session.scalar(
            select(WorkflowCallbackEvent).where(WorkflowCallbackEvent.event_id == event_id)
        )
        if existing_evt is not None:
            raise AppError(
                code="CALLBACK_INVALID",
                message="Replay event_id callback worker ditolak.",
                status_code=409,
            )

        job = await self._session.scalar(
            select(Job)
            .where(Job.id == payload.job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None:
            raise AppError(code="NOT_FOUND", message="Job worker tidak ditemukan.", status_code=404)
        if job.job_type != payload.job_type:
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Tipe job callback worker tidak sesuai.",
                status_code=409,
            )

        document = await self._session.scalar(
            select(Document)
            .where(Document.id == job.document_id, Document.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if document is None:
            raise AppError(code="NOT_FOUND", message="Dokumen tidak ditemukan.", status_code=404)

        now = datetime.now(UTC)
        if job.status in ("completed", "failed", "cancelled"):
            if job.status == payload.event_type:
                dup_evt = WorkflowCallbackEvent(
                    job_id=job.id,
                    event_id=event_id,
                    source_service="worker",
                    event_type=payload.event_type,
                    received_at=now,
                    signature_valid=True,
                    payload_hash=payload_hash,
                    processed_at=now,
                    result_status="ignored_duplicate",
                )
                self._session.add(dup_evt)
                try:
                    await self._session.commit()
                except IntegrityError as exc:
                    await self._session.rollback()
                    raise AppError(
                        code="CALLBACK_INVALID",
                        message="Replay event_id callback worker ditolak.",
                        status_code=409,
                    ) from exc
                return InternalCallbackAckResponse(
                    event_id=event_id,
                    job_id=job.id,
                    job_status=cast(JobStatus, job.status),
                    document_status=cast(DocumentStatus, document.status),
                    result_status="ignored_duplicate",
                )
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Job worker sudah berada pada status terminal berbeda.",
                status_code=409,
            )

        doc_sources = (
            await self._session.scalars(
                select(DocumentSource).where(
                    DocumentSource.workspace_id == job.workspace_id,
                    DocumentSource.document_id == document.id,
                    DocumentSource.validation_status == "valid",
                )
            )
        ).all()
        sources_by_id = {src.id: src for src in doc_sources}

        attempt_num = await self._next_attempt_number(job.id)
        if payload.event_type == "completed":
            if job.job_type == "normalize":
                for item in payload.normalized_sources:
                    target_src = sources_by_id.get(item.source_id)
                    if target_src is None:
                        raise AppError(
                            code="CALLBACK_INVALID",
                            message="source_id pada callback worker tidak cocok dengan dokumen.",
                            status_code=422,
                        )
                    target_src.extracted_text_reference = item.normalized_text[:200_000]
                    target_src.metadata_json = {**target_src.metadata_json, **item.metadata}
            else:
                document.status = "completed"
                document.updated_at = now

            job.status = "completed"
            job.started_at = job.started_at or now
            job.completed_at = now
            job.attempt_count += 1
            job.external_execution_id = payload.external_execution_id or job.external_execution_id

            self._session.add(
                JobAttempt(
                    job_id=job.id,
                    attempt_number=attempt_num,
                    provider_execution_id=payload.external_execution_id,
                    status="completed",
                    started_at=now,
                    completed_at=now,
                    safe_metadata={"job_type": job.job_type},
                )
            )
            self._session.add(
                WorkflowCallbackEvent(
                    job_id=job.id,
                    event_id=event_id,
                    source_service="worker",
                    event_type="completed",
                    received_at=now,
                    signature_valid=True,
                    payload_hash=payload_hash,
                    processed_at=now,
                    result_status="processed",
                )
            )
            try:
                await self._session.commit()
            except IntegrityError as exc:
                await self._session.rollback()
                raise AppError(
                    code="CALLBACK_INVALID",
                    message="Replay event_id callback worker ditolak.",
                    status_code=409,
                ) from exc
            return InternalCallbackAckResponse(
                event_id=event_id,
                job_id=job.id,
                job_status="completed",
                document_status=cast(DocumentStatus, document.status),
                result_status="processed",
            )

        # Worker failed path
        job.status = "failed"
        job.started_at = job.started_at or now
        job.completed_at = now
        job.attempt_count += 1
        job.error_code = payload.error_code or "WORKER_FAILED"
        job.safe_error_message = payload.safe_error_message or "Pemrosesan worker gagal."
        document.status = "failed"
        document.updated_at = now

        self._session.add(
            JobAttempt(
                job_id=job.id,
                attempt_number=attempt_num,
                provider_execution_id=payload.external_execution_id,
                status="failed",
                started_at=now,
                completed_at=now,
                error_code=job.error_code,
                safe_metadata={"job_type": job.job_type},
            )
        )
        self._session.add(
            WorkflowCallbackEvent(
                job_id=job.id,
                event_id=event_id,
                source_service="worker",
                event_type="failed",
                received_at=now,
                signature_valid=True,
                payload_hash=payload_hash,
                processed_at=now,
                result_status="processed",
            )
        )

        # If normalization failed, cancel paired queued ai_generation job and release AI credits.
        reservation_to_release: CreditReservation | None = None
        if job.job_type == "normalize" and job.source_bundle_id is not None:
            paired_ai_job = await self._session.scalar(
                select(Job)
                .where(
                    Job.document_id == document.id,
                    Job.source_bundle_id == job.source_bundle_id,
                    Job.job_type == "ai_generation",
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if paired_ai_job is not None:
                if paired_ai_job.status in ACTIVE_JOB_STATUSES:
                    paired_ai_job.status = "cancelled"
                    paired_ai_job.completed_at = now
                reservation_to_release = await self._session.scalar(
                    select(CreditReservation).where(
                        CreditReservation.workspace_id == document.workspace_id,
                        CreditReservation.job_id == paired_ai_job.id,
                        CreditReservation.status == "reserved",
                    )
                )

        job_id_val = job.id
        doc_status_val = cast(DocumentStatus, document.status)
        ws_id_val = document.workspace_id
        try:
            await self._session.flush()
            if reservation_to_release is not None:
                credit_svc = CreditService(session=self._session, settings=self._settings)
                await credit_svc.release_reservation(
                    workspace_id=ws_id_val,
                    reservation_id=reservation_to_release.id,
                    reason="Pelepasan reservasi karena normalisasi file gagal.",
                )
            else:
                await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise AppError(
                code="CALLBACK_INVALID",
                message="Replay event_id callback worker ditolak.",
                status_code=409,
            ) from exc

        return InternalCallbackAckResponse(
            event_id=event_id,
            job_id=job_id_val,
            job_status="failed",
            document_status=doc_status_val,
            result_status="processed",
        )

    async def handle_workflow_callback(
        self,
        *,
        raw_body: bytes,
        canonical_path: str,
        timestamp_header: str | None,
        event_id_header: str | None,
        signature_header: str | None,
    ) -> InternalCallbackAckResponse:
        """Validate AI workflow callback, persist draft version, commit credits, record usage."""
        _, event_id = self._verify_signed_headers(
            method="POST",
            canonical_path=canonical_path,
            raw_body=raw_body,
            timestamp_header=timestamp_header,
            event_id_header=event_id_header,
            signature_header=signature_header,
            use_replay_guard=False,
        )
        try:
            payload = WorkflowCallbackRequest.model_validate_json(raw_body)
        except (ValidationError, ValueError) as exc:
            raise AppError(
                code="OUTPUT_SCHEMA_INVALID",
                message="Skema hasil atau payload callback workflow AI tidak valid.",
                status_code=422,
            ) from exc

        payload_hash = hashlib.sha256(raw_body).hexdigest()
        existing_evt = await self._session.scalar(
            select(WorkflowCallbackEvent).where(WorkflowCallbackEvent.event_id == event_id)
        )
        if existing_evt is not None:
            raise AppError(
                code="CALLBACK_INVALID",
                message="Replay event_id callback workflow ditolak.",
                status_code=409,
            )

        job = await self._session.scalar(
            select(Job)
            .where(Job.id == payload.job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None:
            raise AppError(
                code="NOT_FOUND",
                message="Job workflow AI tidak ditemukan.",
                status_code=404,
            )
        if job.job_type != "ai_generation":
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Callback workflow hanya berlaku untuk job ai_generation.",
                status_code=409,
            )

        document = await self._session.scalar(
            select(Document)
            .where(Document.id == job.document_id, Document.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if document is None:
            raise AppError(code="NOT_FOUND", message="Dokumen tidak ditemukan.", status_code=404)

        now = datetime.now(UTC)
        if job.status in ("completed", "failed", "cancelled"):
            if job.status == "completed" and payload.event_type == "completed":
                self._session.add(
                    WorkflowCallbackEvent(
                        job_id=job.id,
                        event_id=event_id,
                        source_service="workflow",
                        event_type="completed",
                        received_at=now,
                        signature_valid=True,
                        payload_hash=payload_hash,
                        processed_at=now,
                        result_status="ignored_duplicate",
                    )
                )
                try:
                    await self._session.commit()
                except IntegrityError as exc:
                    await self._session.rollback()
                    raise AppError(
                        code="CALLBACK_INVALID",
                        message="Replay event_id callback workflow ditolak.",
                        status_code=409,
                    ) from exc
                return InternalCallbackAckResponse(
                    event_id=event_id,
                    job_id=job.id,
                    job_status="completed",
                    document_status=cast(DocumentStatus, document.status),
                    result_status="ignored_duplicate",
                )
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Job AI sudah berada pada status terminal.",
                status_code=409,
            )

        if job.status not in ACTIVE_JOB_STATUSES:
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Status job AI tidak valid untuk menerima callback.",
                status_code=409,
            )

        attempt_num = await self._next_attempt_number(job.id)
        usage_meta = payload.provider_metadata or ProviderUsageMetadata(
            provider=job.workflow_provider,
            model=self._settings.gemini_model,
        )

        if payload.event_type == "completed":
            if (
                payload.document_type != "meeting_minutes"
                or payload.document_type != document.document_type
                or payload.workflow_key not in _SUPPORTED_AI_WORKFLOW_KEYS
                or payload.result is None
            ):
                raise AppError(
                    code="OUTPUT_SCHEMA_INVALID",
                    message="Tipe dokumen atau workflow callback AI belum didukung.",
                    status_code=422,
                )

            if job.source_bundle_id is not None:
                pending_norm = await self._session.scalar(
                    select(Job).where(
                        Job.source_bundle_id == job.source_bundle_id,
                        Job.job_type == "normalize",
                    )
                )
                if pending_norm is not None and pending_norm.status != "completed":
                    raise AppError(
                        code="INVALID_JOB_STATE",
                        message="Callback AI diterima sebelum normalisasi selesai.",
                        status_code=409,
                    )

            valid_sources = (
                await self._session.scalars(
                    select(DocumentSource).where(
                        DocumentSource.workspace_id == document.workspace_id,
                        DocumentSource.document_id == document.id,
                        DocumentSource.source_bundle_id == job.source_bundle_id,
                        DocumentSource.validation_status == "valid",
                    )
                )
            ).all()
            _validate_draft_provenance(payload.result, valid_sources)

            max_ver = await self._session.scalar(
                select(func.max(DocumentVersion.version_number)).where(
                    DocumentVersion.document_id == document.id
                )
            )
            next_ver = int(max_ver or 0) + 1
            structured_dict = cast(dict[str, object], payload.result.model_dump(mode="json"))

            new_version = DocumentVersion(
                document_id=document.id,
                version_number=next_ver,
                structured_content=structured_dict,
                content_hash=_hash_canonical_json(structured_dict),
                created_by_user_id=None,
                created_at=now,
                approval_status="pending",
            )
            self._session.add(new_version)
            await self._session.flush()

            document.current_version_id = new_version.id
            document.approved_version_id = None
            document.status = "review_required"
            document.updated_at = now

            job.status = "completed"
            job.started_at = job.started_at or now
            job.completed_at = now
            job.attempt_count += 1
            job.external_execution_id = payload.external_execution_id or job.external_execution_id

            self._session.add(
                JobAttempt(
                    job_id=job.id,
                    attempt_number=attempt_num,
                    provider_execution_id=payload.external_execution_id,
                    status="completed",
                    started_at=now,
                    completed_at=now,
                    safe_metadata={
                        "workflow_key": payload.workflow_key,
                        "version_number": next_ver,
                    },
                )
            )
            self._session.add(
                WorkflowCallbackEvent(
                    job_id=job.id,
                    event_id=event_id,
                    source_service="workflow",
                    event_type="completed",
                    received_at=now,
                    signature_valid=True,
                    payload_hash=payload_hash,
                    processed_at=now,
                    result_status="processed",
                )
            )
            self._session.add(
                UsageEvent(
                    workspace_id=document.workspace_id,
                    user_id=document.created_by_user_id,
                    document_id=document.id,
                    job_id=job.id,
                    provider=usage_meta.provider,
                    model=usage_meta.model,
                    workflow_key=payload.workflow_key,
                    input_tokens=usage_meta.input_tokens,
                    output_tokens=usage_meta.output_tokens,
                    estimated_cost_minor_units=usage_meta.estimated_cost_minor_units,
                    duration_ms=usage_meta.duration_ms,
                    status="completed",
                    created_at=now,
                )
            )
            record_workspace_audit(
                self._session,
                workspace_id=document.workspace_id,
                actor_user_id=None,
                action="documents.ai_draft_ready",
                resource_type="document",
                resource_id=str(document.id),
                metadata={"job_id": str(job.id), "version_number": next_ver},
            )

            reservation = await self._session.scalar(
                select(CreditReservation).where(
                    CreditReservation.workspace_id == document.workspace_id,
                    CreditReservation.job_id == job.id,
                    CreditReservation.status == "reserved",
                )
            )
            job_id_val = job.id
            ws_id_val = document.workspace_id
            try:
                await self._session.flush()
                if reservation is not None:
                    credit_svc = CreditService(session=self._session, settings=self._settings)
                    await credit_svc.commit_reservation(
                        workspace_id=ws_id_val,
                        reservation_id=reservation.id,
                        document_status="review_required",
                        actor_user_id=None,
                    )
                else:
                    await self._session.commit()
            except IntegrityError as exc:
                await self._session.rollback()
                raise AppError(
                    code="CALLBACK_INVALID",
                    message="Replay event_id callback workflow ditolak.",
                    status_code=409,
                ) from exc

            return InternalCallbackAckResponse(
                event_id=event_id,
                job_id=job_id_val,
                job_status="completed",
                document_status="review_required",
                result_status="processed",
            )

        # Failed AI workflow callback path
        job.status = "failed"
        job.started_at = job.started_at or now
        job.completed_at = now
        job.attempt_count += 1
        job.error_code = payload.error_code or "AI_PROVIDER_ERROR"
        job.safe_error_message = payload.safe_error_message or "Pemrosesan AI gagal."
        job.external_execution_id = payload.external_execution_id or job.external_execution_id
        document.status = "failed"
        document.updated_at = now

        self._session.add(
            JobAttempt(
                job_id=job.id,
                attempt_number=attempt_num,
                provider_execution_id=payload.external_execution_id,
                status="failed",
                started_at=now,
                completed_at=now,
                error_code=job.error_code,
                safe_metadata={"workflow_key": payload.workflow_key},
            )
        )
        self._session.add(
            WorkflowCallbackEvent(
                job_id=job.id,
                event_id=event_id,
                source_service="workflow",
                event_type="failed",
                received_at=now,
                signature_valid=True,
                payload_hash=payload_hash,
                processed_at=now,
                result_status="processed",
            )
        )
        self._session.add(
            UsageEvent(
                workspace_id=document.workspace_id,
                user_id=document.created_by_user_id,
                document_id=document.id,
                job_id=job.id,
                provider=usage_meta.provider,
                model=usage_meta.model,
                workflow_key=payload.workflow_key,
                input_tokens=usage_meta.input_tokens,
                output_tokens=usage_meta.output_tokens,
                estimated_cost_minor_units=usage_meta.estimated_cost_minor_units,
                duration_ms=usage_meta.duration_ms,
                status="failed",
                created_at=now,
            )
        )

        reservation = await self._session.scalar(
            select(CreditReservation).where(
                CreditReservation.workspace_id == document.workspace_id,
                CreditReservation.job_id == job.id,
                CreditReservation.status == "reserved",
            )
        )
        job_id_val = job.id
        ws_id_val = document.workspace_id
        try:
            await self._session.flush()
            if reservation is not None:
                credit_svc = CreditService(session=self._session, settings=self._settings)
                await credit_svc.release_reservation(
                    workspace_id=ws_id_val,
                    reservation_id=reservation.id,
                    reason=f"Pelepasan reservasi akibat kegagalan AI ({job.error_code}).",
                )
            else:
                await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            raise AppError(
                code="CALLBACK_INVALID",
                message="Replay event_id callback workflow ditolak.",
                status_code=409,
            ) from exc

        return InternalCallbackAckResponse(
            event_id=event_id,
            job_id=job_id_val,
            job_status="failed",
            document_status="failed",
            result_status="processed",
        )

    async def list_workspace_usage(
        self,
        actor: User,
        *,
        workspace_id: UUID,
        page: int = 1,
        page_size: int = 20,
    ) -> UsageListResponse:
        """List non-sensitive AI usage events for an authorized workspace member."""
        workspace, member = await require_active_workspace_membership(
            self._session,
            workspace_id=workspace_id,
            user_id=actor.id,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="view",
        )

        total = int(
            await self._session.scalar(
                select(func.count(UsageEvent.id)).where(UsageEvent.workspace_id == workspace_id)
            )
            or 0
        )
        offset = (page - 1) * page_size
        rows = (
            await self._session.scalars(
                select(UsageEvent)
                .where(UsageEvent.workspace_id == workspace_id)
                .order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc())
                .offset(offset)
                .limit(page_size)
            )
        ).all()
        return UsageListResponse(
            items=[_to_usage_response(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def get_workspace_usage_summary(
        self,
        actor: User,
        *,
        workspace_id: UUID,
    ) -> UsageSummaryResponse:
        """Aggregate non-sensitive AI usage counters for a workspace."""
        workspace, member = await require_active_workspace_membership(
            self._session,
            workspace_id=workspace_id,
            user_id=actor.id,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="view",
        )

        rows = (
            await self._session.scalars(
                select(UsageEvent).where(UsageEvent.workspace_id == workspace_id)
            )
        ).all()
        completed_count = sum(1 for r in rows if r.status == "completed")
        failed_count = sum(1 for r in rows if r.status == "failed")
        return UsageSummaryResponse(
            workspace_id=workspace_id,
            total_events=len(rows),
            completed_events=completed_count,
            failed_events=failed_count,
            total_input_tokens=sum(r.input_tokens for r in rows),
            total_output_tokens=sum(r.output_tokens for r in rows),
            total_estimated_cost_minor_units=sum(r.estimated_cost_minor_units for r in rows),
            total_duration_ms=sum(r.duration_ms for r in rows),
        )

    def _signed_post_headers(
        self,
        *,
        canonical_path: str,
        raw_body: bytes,
        event_id: str,
    ) -> dict[str, str]:
        ts = int(time.time())
        sig = compute_internal_hmac(
            secret=self._settings.internal_hmac_secret,
            timestamp=ts,
            event_id=event_id,
            method="POST",
            canonical_path=canonical_path,
            raw_body=raw_body,
        )
        return {
            "Content-Type": "application/json",
            "X-Internal-Timestamp": str(ts),
            "X-Internal-Event-Id": event_id,
            "X-Internal-Signature": sig,
        }

    async def _fail_running_job_and_release_credits(
        self,
        *,
        job_id: UUID,
        error_code: str,
        safe_message: str,
    ) -> None:
        job = await self._session.scalar(
            select(Job)
            .where(Job.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if job is None or job.status in ("completed", "failed", "cancelled"):
            await self._session.commit()
            return

        document = await self._session.scalar(
            select(Document)
            .where(Document.id == job.document_id, Document.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        now = datetime.now(UTC)
        attempt_num = await self._next_attempt_number(job.id)
        job.status = "failed"
        job.started_at = job.started_at or now
        job.completed_at = now
        job.attempt_count += 1
        job.error_code = error_code[:64]
        job.safe_error_message = safe_message[:255]
        job.updated_at = now

        if document is not None:
            document.status = "failed"
            document.updated_at = now

        self._session.add(
            JobAttempt(
                job_id=job.id,
                attempt_number=attempt_num,
                provider_execution_id=job.external_execution_id,
                status="failed",
                started_at=now,
                completed_at=now,
                error_code=job.error_code,
                safe_metadata={"job_type": job.job_type, "stage": "dispatch"},
            )
        )

        reservation_job_id = job.id
        if job.job_type == "normalize" and job.source_bundle_id is not None:
            paired_ai_job = await self._session.scalar(
                select(Job)
                .where(
                    Job.document_id == job.document_id,
                    Job.source_bundle_id == job.source_bundle_id,
                    Job.job_type == "ai_generation",
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if paired_ai_job is not None:
                reservation_job_id = paired_ai_job.id
                if paired_ai_job.status in ACTIVE_JOB_STATUSES:
                    paired_ai_job.status = "cancelled"
                    paired_ai_job.completed_at = now
        elif job.job_type == "ai_generation" and document is not None:
            self._session.add(
                UsageEvent(
                    workspace_id=document.workspace_id,
                    user_id=document.created_by_user_id,
                    document_id=document.id,
                    job_id=job.id,
                    provider=job.workflow_provider,
                    model=self._settings.gemini_model,
                    workflow_key=job.workflow_key,
                    input_tokens=0,
                    output_tokens=0,
                    estimated_cost_minor_units=0,
                    duration_ms=0,
                    status="failed",
                    created_at=now,
                )
            )

        reservation = await self._session.scalar(
            select(CreditReservation).where(
                CreditReservation.workspace_id == job.workspace_id,
                CreditReservation.job_id == reservation_job_id,
                CreditReservation.status == "reserved",
            )
        )
        ws_id = job.workspace_id
        await self._session.flush()
        if reservation is not None:
            credit_svc = CreditService(session=self._session, settings=self._settings)
            await credit_svc.release_reservation(
                workspace_id=ws_id,
                reservation_id=reservation.id,
                reason=f"Pelepasan reservasi karena dispatch gagal ({job.error_code}).",
            )
        else:
            await self._session.commit()
        self._session.expunge_all()

    async def dispatch_normalize_job(
        self,
        *,
        job_id: UUID,
        loopback_client: httpx.AsyncClient,
        storage: ObjectStorageService,
        worker_http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Atomically transition a queued normalize job to running and invoke Worker/fallback."""
        norm_job = await self._session.scalar(
            select(Job)
            .where(Job.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if norm_job is None or norm_job.job_type != "normalize" or norm_job.status != "queued":
            await self._session.commit()
            return

        bundle_id = norm_job.source_bundle_id
        if bundle_id is None:
            await self._session.commit()
            return

        now = datetime.now(UTC)
        norm_job.status = "running"
        norm_job.started_at = norm_job.started_at or now
        norm_job.updated_at = now

        upload_sources = (
            await self._session.scalars(
                select(DocumentSource)
                .where(
                    DocumentSource.workspace_id == norm_job.workspace_id,
                    DocumentSource.document_id == norm_job.document_id,
                    DocumentSource.source_bundle_id == bundle_id,
                    DocumentSource.source_type == "upload",
                    DocumentSource.validation_status == "valid",
                )
                .order_by(DocumentSource.created_at.asc(), DocumentSource.id.asc())
            )
        ).all()
        source_records = [
            (
                src.id,
                src.source_name,
                src.storage_object_key,
                dict(src.metadata_json),
            )
            for src in upload_sources
        ]
        await self._session.commit()
        self._session.expunge_all()

        worker_callback_path = f"{self._settings.api_v1_prefix}/internal/workers/callback"

        try:
            loaded_inputs: list[tuple[UUID, str, str, bytes]] = []
            for src_id, src_name, obj_key, meta in source_records:
                stored = storage.get_object(object_key=obj_key) if obj_key else None
                if stored is None:
                    fail_cb = WorkerCallbackRequest(
                        job_id=job_id,
                        job_type="normalize",
                        event_type="failed",
                        external_execution_id=f"norm-missing-{uuid4()}",
                        error_code="UPLOAD_REJECTED",
                        safe_error_message=(
                            "Objek sumber unggahan tidak ditemukan saat normalisasi."
                        ),
                    )
                    fail_bytes = fail_cb.model_dump_json().encode("utf-8")
                    await loopback_client.post(
                        worker_callback_path,
                        content=fail_bytes,
                        headers=self._signed_post_headers(
                            canonical_path=worker_callback_path,
                            raw_body=fail_bytes,
                            event_id=f"wrkcb-miss-{uuid4()}",
                        ),
                    )
                    return
                filename = str(meta.get("filename") or src_name)
                mime = str(meta.get("content_type") or stored.content_type or "text/plain")
                loaded_inputs.append((src_id, filename, mime, stored.content))

            if worker_http_client is not None or self._settings.worker_provider_mode == "http":
                worker_path = f"{self._settings.api_v1_prefix}/internal/normalize"
                cb_url = (
                    f"{str(loopback_client.base_url).rstrip('/')}{worker_callback_path}"
                    if worker_http_client is not None
                    else None
                )
                worker_req = {
                    "job_id": str(job_id),
                    "sources": [
                        {
                            "source_id": str(src_id),
                            "filename": fname,
                            "content_type": mime,
                            "content_base64": base64.b64encode(raw_bytes).decode("ascii"),
                        }
                        for src_id, fname, mime, raw_bytes in loaded_inputs
                    ],
                    "deliver_callback": True,
                    "callback_url": cb_url,
                }
                raw_req = json.dumps(worker_req, separators=(",", ":")).encode("utf-8")
                req_headers = self._signed_post_headers(
                    canonical_path=worker_path,
                    raw_body=raw_req,
                    event_id=f"wrkdisp-{uuid4()}",
                )
                if worker_http_client is not None:
                    wrk_res = await worker_http_client.post(
                        worker_path,
                        content=raw_req,
                        headers=req_headers,
                    )
                else:
                    async with httpx.AsyncClient(
                        base_url=self._settings.worker_base_url,
                        timeout=self._settings.worker_timeout_seconds,
                    ) as client:
                        wrk_res = await client.post(
                            worker_path,
                            content=raw_req,
                            headers=req_headers,
                        )
                if wrk_res.status_code >= 400:
                    await self._fail_running_job_and_release_credits(
                        job_id=job_id,
                        error_code="AI_PROVIDER_ERROR",
                        safe_message="Layanan normalisasi worker menolak permintaan.",
                    )
                return

            normalized_items: list[NormalizedSourceItem] = []
            for src_id, fname, mime, raw_bytes in loaded_inputs:
                validated = validate_uploaded_content(
                    filename=fname,
                    content_type=mime,
                    data=raw_bytes,
                    max_file_size_bytes=self._settings.max_upload_file_size_bytes,
                    max_archive_expansion_ratio=self._settings.max_archive_expansion_ratio,
                    max_zip_entries=self._settings.max_zip_entries,
                )
                normalized_items.append(
                    NormalizedSourceItem(
                        source_id=src_id,
                        normalized_text=validated.extracted_text[:200_000],
                        metadata={"normalized_by": "local_fallback", **validated.metadata},
                    )
                )

            cb_payload = WorkerCallbackRequest(
                job_id=job_id,
                job_type="normalize",
                event_type="completed",
                external_execution_id=f"local-wrk-{uuid4()}",
                normalized_sources=normalized_items,
            )
            cb_bytes = cb_payload.model_dump_json().encode("utf-8")
            cb_res = await loopback_client.post(
                worker_callback_path,
                content=cb_bytes,
                headers=self._signed_post_headers(
                    canonical_path=worker_callback_path,
                    raw_body=cb_bytes,
                    event_id=f"locwrk-{uuid4()}",
                ),
            )
            cb_res.raise_for_status()
        except AppError as exc:
            await self._fail_running_job_and_release_credits(
                job_id=job_id,
                error_code=exc.code,
                safe_message=exc.message,
            )
        except (httpx.HTTPError, ValueError):
            await self._fail_running_job_and_release_credits(
                job_id=job_id,
                error_code="AI_PROVIDER_ERROR",
                safe_message="Normalisasi file sumber gagal dijalankan.",
            )

    async def dispatch_ai_generation_job(
        self,
        *,
        job_id: UUID,
        loopback_client: httpx.AsyncClient,
        gemini_provider: GeminiProvider,
        n8n_http_client: httpx.AsyncClient | None = None,
    ) -> JobStatus | None:
        """Atomically transition queued ai_generation job to running and dispatch orchestrator."""
        if self._settings.workflow_orchestrator_provider == "none":
            return None

        ai_job = await self._session.scalar(
            select(Job)
            .where(Job.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if ai_job is None or ai_job.job_type != "ai_generation":
            await self._session.commit()
            return None
        if ai_job.status != "queued":
            current_status = cast(JobStatus, ai_job.status)
            await self._session.commit()
            return current_status

        if ai_job.source_bundle_id is not None:
            norm_job = await self._session.scalar(
                select(Job).where(
                    Job.source_bundle_id == ai_job.source_bundle_id,
                    Job.job_type == "normalize",
                )
            )
            if norm_job is not None and norm_job.status != "completed":
                await self._session.commit()
                return "queued"

        document = await self._session.scalar(
            select(Document).where(Document.id == ai_job.document_id, Document.deleted_at.is_(None))
        )
        if document is None or ai_job.source_bundle_id is None:
            await self._session.commit()
            return cast(JobStatus, ai_job.status)

        now = datetime.now(UTC)
        ai_job.status = "running"
        ai_job.started_at = ai_job.started_at or now
        ai_job.updated_at = now
        api_prefix = self._settings.api_v1_prefix
        dispatch_req = WorkflowDispatchRequest(
            job_id=ai_job.id,
            idempotency_key=ai_job.idempotency_key,
            workflow_key=ai_job.workflow_key,
            workflow_version=ai_job.workflow_version,
            document_type=document.document_type,
            input_reference=WorkflowInputReference(
                source_bundle_id=ai_job.source_bundle_id,
                retrieval_endpoint=f"{api_prefix}/internal/source-bundles/{ai_job.source_bundle_id}",
            ),
            callback_endpoint=f"{api_prefix}/internal/workflows/callback",
            issued_at=now,
        )
        await self._session.commit()
        self._session.expunge_all()

        orchestrator: WorkflowOrchestrator
        if self._settings.workflow_orchestrator_provider == "python":
            orchestrator = PythonWorkflowAdapter(
                settings=self._settings,
                gemini_provider=gemini_provider,
                http_client=loopback_client,
            )
        else:
            orchestrator = N8NWorkflowAdapter(
                settings=self._settings,
                http_client=n8n_http_client,
            )

        try:
            dispatch_result = await orchestrator.dispatch(dispatch_req)
            if not dispatch_result.accepted:
                await self._fail_running_job_and_release_credits(
                    job_id=job_id,
                    error_code="AI_PROVIDER_ERROR",
                    safe_message=dispatch_result.message
                    or "Layanan orkestrasi AI menolak permintaan.",
                )
        except httpx.HTTPStatusError as exc:
            err_code = "CALLBACK_DELIVERY_FAILED"
            safe_msg = "Pengiriman hasil orkestrasi AI ditolak oleh backend."
            try:
                err_body = exc.response.json()
                raw_code = err_body.get("error", {}).get("code")
                if isinstance(raw_code, str) and raw_code:
                    err_code = raw_code
            except ValueError:
                pass
            await self._fail_running_job_and_release_credits(
                job_id=job_id,
                error_code=err_code,
                safe_message=safe_msg,
            )
        except (httpx.HTTPError, AppError, ValueError):
            await self._fail_running_job_and_release_credits(
                job_id=job_id,
                error_code="AI_PROVIDER_ERROR",
                safe_message="Eksekusi orkestrasi workflow AI gagal.",
            )

        updated_status = await self._session.scalar(
            select(Job.status).where(Job.id == job_id).execution_options(populate_existing=True)
        )
        return cast(JobStatus, updated_status) if updated_status is not None else None

    async def dispatch_paired_ai_after_normalize(
        self,
        *,
        normalize_job_id: UUID,
        loopback_client: httpx.AsyncClient,
        gemini_provider: GeminiProvider,
        n8n_http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """Dispatch the paired queued ai_generation job once normalize completes."""
        if self._settings.workflow_orchestrator_provider == "none":
            return

        norm_job = await self._session.scalar(
            select(Job)
            .where(Job.id == normalize_job_id)
            .execution_options(populate_existing=True)
        )
        if (
            norm_job is None
            or norm_job.job_type != "normalize"
            or norm_job.status != "completed"
            or norm_job.source_bundle_id is None
        ):
            return

        paired_ai_job = await self._session.scalar(
            select(Job)
            .where(
                Job.document_id == norm_job.document_id,
                Job.source_bundle_id == norm_job.source_bundle_id,
                Job.job_type == "ai_generation",
            )
            .execution_options(populate_existing=True)
        )
        if paired_ai_job is not None and paired_ai_job.status == "queued":
            await self.dispatch_ai_generation_job(
                job_id=paired_ai_job.id,
                loopback_client=loopback_client,
                gemini_provider=gemini_provider,
                n8n_http_client=n8n_http_client,
            )

    async def refresh_callback_ack(
        self,
        *,
        data: InternalCallbackAckResponse,
    ) -> InternalCallbackAckResponse:
        """Reload document_status after downstream paired AI orchestration runs."""
        job = await self._session.scalar(
            select(Job).where(Job.id == data.job_id).execution_options(populate_existing=True)
        )
        if job is None:
            return data
        doc_status = await self._session.scalar(
            select(Document.status)
            .where(Document.id == job.document_id)
            .execution_options(populate_existing=True)
        )
        if doc_status is None:
            return data
        return data.model_copy(update={"document_status": cast(DocumentStatus, doc_status)})
