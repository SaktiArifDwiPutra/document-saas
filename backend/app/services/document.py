"""Document, source provenance, validation, and job foundation service (Milestone M5)."""

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError
from app.core.file_validation import (
    normalize_content_type,
    sanitize_filename,
    validate_upload_declaration,
    validate_uploaded_content,
)
from app.core.permissions import (
    DocumentAction,
    WorkspaceRole,
    WorkspaceType,
    authorize_document_access,
    require_workspace_action,
)
from app.core.security import ensure_utc
from app.models.auth import User
from app.models.credit import CreditReservation
from app.models.document import (
    Document,
    DocumentSource,
    DocumentVersion,
    Job,
    Template,
    TemplateVersion,
)
from app.models.workspace import Workspace, WorkspaceMember
from app.schemas.document import (
    ApprovalStatus,
    ApproveDocumentRequest,
    DeleteDocumentResponse,
    DocumentCreateRequest,
    DocumentDetailResponse,
    DocumentListResponse,
    DocumentReviewReadiness,
    DocumentSourceListResponse,
    DocumentSourceResponse,
    DocumentSourceSubmitRequest,
    DocumentStatus,
    DocumentSummaryResponse,
    DocumentUpdateRequest,
    DocumentVersionListResponse,
    DocumentVersionResponse,
    JobResponse,
    JobStatus,
    JobType,
    MeetingMinutesFormInput,
    MeetingMinutesReviewContent,
    ProcessDocumentRequest,
    ProcessDocumentResponse,
    ReviewActionItem,
    ReviewConflictItem,
    ReviewDecisionItem,
    ReviewFieldStatus,
    ReviewOpinionItem,
    ReviewProposalItem,
    ReviewProvenanceEntry,
    SourceType,
    SourceValidationStatus,
    UploadIntentRequest,
    UploadIntentResponse,
)
from app.services.credit import CreditService
from app.services.storage import ObjectStorageService
from app.services.workspace_access import (
    record_workspace_audit,
    require_active_workspace_membership,
)

ACTIVE_JOB_STATUSES: frozenset[str] = frozenset({"queued", "running", "validating"})
_PROCESSABLE_DOC_STATUSES: frozenset[str] = frozenset({"draft", "failed", "review_required"})
_IMMUTABLE_WHILE_ACTIVE_STATUSES: frozenset[str] = frozenset(
    {"processing", "rendering", "archived"}
)
_REVIEW_SCALAR_FIELDS: tuple[str, ...] = (
    "meeting_title",
    "meeting_date",
    "meeting_time",
    "location",
    "organizer",
    "chairperson",
    "discussion_summary",
)
_REVIEW_SIMPLE_LIST_FIELDS: tuple[str, ...] = ("attendees", "absentees", "agenda_items")
_REVIEW_COMPLEX_LIST_FIELDS: tuple[str, ...] = (
    "opinions",
    "proposals",
    "decisions",
    "action_items",
)
_ALL_REVIEW_CONTENT_FIELDS: frozenset[str] = frozenset(
    _REVIEW_SCALAR_FIELDS + _REVIEW_SIMPLE_LIST_FIELDS + _REVIEW_COMPLEX_LIST_FIELDS
)
_REVIEW_SCHEMA_KEYS: frozenset[str] = frozenset(MeetingMinutesReviewContent.model_fields.keys())


def _clean_optional_str(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned if cleaned else None


def _clean_string_list(items: Sequence[str]) -> list[str]:
    return [item.strip() for item in items if item.strip()]


def _provenance_matches_field(field_path: str, target_field: str) -> bool:
    return (
        field_path == target_field
        or field_path.startswith(f"{target_field}[")
        or field_path.startswith(f"{target_field}.")
    )


def _content_fields_signature(parsed: MeetingMinutesReviewContent) -> dict[str, object]:
    """Return canonical content-only payload ignoring provenance/status metadata."""
    return {
        "meeting_title": _clean_optional_str(parsed.meeting_title),
        "meeting_date": _clean_optional_str(parsed.meeting_date),
        "meeting_time": _clean_optional_str(parsed.meeting_time),
        "location": _clean_optional_str(parsed.location),
        "organizer": _clean_optional_str(parsed.organizer),
        "chairperson": _clean_optional_str(parsed.chairperson),
        "attendees": _clean_string_list(parsed.attendees),
        "absentees": _clean_string_list(parsed.absentees),
        "agenda_items": _clean_string_list(parsed.agenda_items),
        "discussion_summary": _clean_optional_str(parsed.discussion_summary),
        "opinions": [
            (item.statement.strip(), _clean_optional_str(item.speaker))
            for item in parsed.opinions
        ],
        "proposals": [
            (item.proposal.strip(), _clean_optional_str(item.proposer), item.status)
            for item in parsed.proposals
        ],
        "decisions": [item.decision.strip() for item in parsed.decisions],
        "action_items": [
            (
                item.task.strip(),
                _clean_optional_str(item.assignee),
                _clean_optional_str(item.due_date),
            )
            for item in parsed.action_items
        ],
    }


def evaluate_meeting_minutes_readiness(
    content: dict[str, object] | None,
) -> DocumentReviewReadiness:
    """Evaluate mandatory approval preconditions vs. optional informational warnings."""
    if content is None:
        return DocumentReviewReadiness(
            is_ready_for_approval=False,
            missing_required_fields=[
                "meeting_title",
                "meeting_date",
                "attendees",
                "agenda_items",
                "content_sections",
            ],
            unresolved_conflict_fields=[],
            blocking_issues=["Draf dokumen belum tersedia untuk ditinjau."],
            optional_warnings=[],
        )

    try:
        parsed = MeetingMinutesReviewContent.model_validate(content)
    except ValidationError:
        return DocumentReviewReadiness(
            is_ready_for_approval=False,
            missing_required_fields=[
                "meeting_title",
                "meeting_date",
                "attendees",
                "agenda_items",
                "content_sections",
            ],
            unresolved_conflict_fields=[],
            blocking_issues=["Struktur draf Notulen Rapat belum lengkap."],
            optional_warnings=[],
        )

    missing_required_fields: list[str] = []
    blocking_issues: list[str] = []

    if _clean_optional_str(parsed.meeting_title) is None:
        missing_required_fields.append("meeting_title")
        blocking_issues.append("Judul rapat wajib diisi sebelum persetujuan.")
    if _clean_optional_str(parsed.meeting_date) is None:
        missing_required_fields.append("meeting_date")
        blocking_issues.append("Tanggal rapat wajib diisi sebelum persetujuan.")
    if not _clean_string_list(parsed.attendees):
        missing_required_fields.append("attendees")
        blocking_issues.append("Daftar peserta hadir wajib memiliki minimal satu peserta.")
    if not _clean_string_list(parsed.agenda_items):
        missing_required_fields.append("agenda_items")
        blocking_issues.append("Agenda rapat wajib memiliki minimal satu topik.")

    has_summary = _clean_optional_str(parsed.discussion_summary) is not None
    has_decisions = len(parsed.decisions) > 0
    has_actions = len(parsed.action_items) > 0
    if not (has_summary or has_decisions or has_actions):
        missing_required_fields.append("content_sections")
        blocking_issues.append(
            "Minimal satu bagian isi rapat (ringkasan pembahasan, keputusan, "
            "atau tindak lanjut) wajib diisi."
        )

    unresolved_conflict_fields: list[str] = []
    seen_conflicts: set[str] = set()
    for conflict in parsed.conflicts:
        if conflict.field_path not in seen_conflicts:
            seen_conflicts.add(conflict.field_path)
            unresolved_conflict_fields.append(conflict.field_path)
    for field_name, status in parsed.field_statuses.items():
        if status == "conflicting" and field_name not in seen_conflicts:
            seen_conflicts.add(field_name)
            unresolved_conflict_fields.append(field_name)

    for conflict_field in unresolved_conflict_fields:
        blocking_issues.append(
            f"Konflik pada bidang '{conflict_field}' wajib diselesaikan terlebih dahulu."
        )

    optional_warnings: list[str] = []
    seen_warnings: set[str] = set()
    for warning in parsed.warnings:
        cleaned_warning = warning.strip()
        if cleaned_warning and cleaned_warning not in seen_warnings:
            seen_warnings.add(cleaned_warning)
            optional_warnings.append(cleaned_warning)

    if _clean_optional_str(parsed.meeting_time) is None:
        optional_warnings.append("Waktu rapat belum diisi (opsional).")
    if _clean_optional_str(parsed.location) is None:
        optional_warnings.append("Lokasi rapat belum diisi (opsional).")
    if _clean_optional_str(parsed.organizer) is None:
        optional_warnings.append("Penyelenggara rapat belum diisi (opsional).")
    if _clean_optional_str(parsed.chairperson) is None:
        optional_warnings.append("Pimpinan rapat belum diisi (opsional).")
    if not parsed.decisions:
        optional_warnings.append("Tidak ada keputusan formal tercatat pada rapat ini (opsional).")
    if any(
        _clean_optional_str(item.assignee) is None or _clean_optional_str(item.due_date) is None
        for item in parsed.action_items
    ):
        optional_warnings.append(
            "Beberapa tindak lanjut belum memiliki PIC atau tenggat waktu (opsional)."
        )

    return DocumentReviewReadiness(
        is_ready_for_approval=(
            len(missing_required_fields) == 0 and len(unresolved_conflict_fields) == 0
        ),
        missing_required_fields=missing_required_fields,
        unresolved_conflict_fields=unresolved_conflict_fields,
        blocking_issues=blocking_issues,
        optional_warnings=optional_warnings,
    )


class DatabaseJobLivenessChecker:
    """Authoritative JobLivenessChecker backed by row-locked `jobs` records."""

    async def is_job_active(self, session: AsyncSession, job_id: UUID) -> bool:
        """Return True while the associated job remains queued, running, or validating."""
        status = await session.scalar(
            select(Job.status)
            .where(Job.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return status in ACTIVE_JOB_STATUSES


def _hash_canonical_json(payload: dict[str, object]) -> str:
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _format_form_extracted_text(form_data: MeetingMinutesFormInput) -> str:
    """Create readable normalized text representation from structured meeting minutes input."""
    lines = [f"Judul Rapat: {form_data.meeting_title}"]
    if form_data.meeting_date:
        lines.append(f"Tanggal: {form_data.meeting_date}")
    if form_data.meeting_time:
        lines.append(f"Waktu: {form_data.meeting_time}")
    if form_data.location:
        lines.append(f"Lokasi: {form_data.location}")
    if form_data.organizer:
        lines.append(f"Penyelenggara: {form_data.organizer}")
    if form_data.attendees:
        lines.append("Peserta: " + ", ".join(form_data.attendees))
    if form_data.agenda_items:
        lines.append("Agenda: " + "; ".join(form_data.agenda_items))
    if form_data.notes:
        lines.append(f"Catatan: {form_data.notes.strip()}")
    if form_data.decisions_raw:
        lines.append("Usulan/Keputusan Mentah: " + "; ".join(form_data.decisions_raw))
    if form_data.action_items_raw:
        lines.append("Tindak Lanjut Mentah: " + "; ".join(form_data.action_items_raw))
    return "\n".join(lines)


def _to_version_response(version: DocumentVersion) -> DocumentVersionResponse:
    return DocumentVersionResponse(
        id=version.id,
        document_id=version.document_id,
        version_number=version.version_number,
        structured_content=dict(version.structured_content),
        content_hash=version.content_hash,
        created_by_user_id=version.created_by_user_id,
        created_at=ensure_utc(version.created_at),
        approval_status=cast(ApprovalStatus, version.approval_status),
        approved_by_user_id=version.approved_by_user_id,
        approved_at=ensure_utc(version.approved_at) if version.approved_at else None,
    )


def _to_source_response(source: DocumentSource) -> DocumentSourceResponse:
    preview = (
        source.extracted_text_reference[:500]
        if source.extracted_text_reference
        else None
    )
    return DocumentSourceResponse(
        id=source.id,
        workspace_id=source.workspace_id,
        document_id=source.document_id,
        document_version_id=source.document_version_id,
        source_bundle_id=source.source_bundle_id,
        source_type=cast(SourceType, source.source_type),
        has_stored_object=source.storage_object_key is not None,
        source_name=source.source_name,
        content_hash=source.content_hash,
        validation_status=cast(SourceValidationStatus, source.validation_status),
        metadata=dict(source.metadata_json),
        extracted_text_preview=preview,
        created_by_user_id=source.created_by_user_id,
        created_at=ensure_utc(source.created_at),
    )


def _to_job_response(job: Job) -> JobResponse:
    return JobResponse(
        id=job.id,
        workspace_id=job.workspace_id,
        document_id=job.document_id,
        job_type=cast(JobType, job.job_type),
        status=cast(JobStatus, job.status),
        source_bundle_id=job.source_bundle_id,
        workflow_key=job.workflow_key,
        workflow_version=job.workflow_version,
        workflow_provider=job.workflow_provider,
        attempt_count=job.attempt_count,
        started_at=ensure_utc(job.started_at) if job.started_at else None,
        completed_at=ensure_utc(job.completed_at) if job.completed_at else None,
        error_code=job.error_code,
        safe_error_message=job.safe_error_message,
        created_at=ensure_utc(job.created_at),
        updated_at=ensure_utc(job.updated_at),
    )


def _to_document_summary(document: Document) -> DocumentSummaryResponse:
    return DocumentSummaryResponse(
        id=document.id,
        workspace_id=document.workspace_id,
        created_by_user_id=document.created_by_user_id,
        document_type=document.document_type,
        title=document.title,
        status=cast(DocumentStatus, document.status),
        current_version_id=document.current_version_id,
        selected_template_version_id=document.selected_template_version_id,
        approved_version_id=document.approved_version_id,
        created_at=ensure_utc(document.created_at),
        updated_at=ensure_utc(document.updated_at),
    )


class DocumentService:
    """Owns M5 documents, versions, source validation, and async job creation."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        storage: ObjectStorageService,
    ) -> None:
        self._session = session
        self._settings = settings
        self._storage = storage

    async def _validate_selected_template_version(
        self,
        *,
        workspace: Workspace,
        template_version_id: UUID,
    ) -> TemplateVersion:
        row = (
            await self._session.execute(
                select(TemplateVersion, Template)
                .join(Template, Template.id == TemplateVersion.template_id)
                .where(TemplateVersion.id == template_version_id)
            )
        ).first()
        if row is None:
            raise AppError(
                code="NOT_FOUND",
                message="Versi templat tidak ditemukan.",
                status_code=404,
            )
        version, template = row
        if template.status == "archived" or version.archived_at is not None:
            raise AppError(
                code="CONFLICT",
                message="Versi templat sudah diarsipkan dan tidak dapat dipilih.",
                status_code=409,
            )
        if not template.is_builtin and template.workspace_id != workspace.id:
            raise AppError(
                code="NOT_FOUND",
                message="Versi templat tidak ditemukan di workspace ini.",
                status_code=404,
            )
        if workspace.type == "personal" and not template.is_builtin:
            raise AppError(
                code="FORBIDDEN",
                message="Workspace pribadi hanya dapat menggunakan templat bawaan aplikasi.",
                status_code=403,
            )
        return version

    async def _require_authorized_document(
        self,
        actor: User,
        *,
        document_id: UUID,
        action: DocumentAction,
        expected_workspace_id: UUID | None = None,
        lock_document: bool = False,
    ) -> tuple[Document, Workspace, WorkspaceMember]:
        if expected_workspace_id is not None:
            workspace, member = await require_active_workspace_membership(
                self._session,
                workspace_id=expected_workspace_id,
                user_id=actor.id,
            )

        stmt = select(Document).where(
            Document.id == document_id,
            Document.deleted_at.is_(None),
        )
        if lock_document:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        document = await self._session.scalar(stmt)
        if document is None or (
            expected_workspace_id is not None and document.workspace_id != expected_workspace_id
        ):
            raise AppError(
                code="NOT_FOUND",
                message="Dokumen tidak ditemukan.",
                status_code=404,
            )

        if expected_workspace_id is None:
            workspace, member = await require_active_workspace_membership(
                self._session,
                workspace_id=document.workspace_id,
                user_id=actor.id,
            )

        owner_id = document.created_by_user_id or UUID(int=0)
        authorize_document_access(
            role=cast(WorkspaceRole, member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            actor_user_id=actor.id,
            document_owner_user_id=owner_id,
            action=action,
        )
        return document, workspace, member

    async def _count_upload_sources(self, document_id: UUID) -> int:
        count = await self._session.scalar(
            select(func.count(DocumentSource.id)).where(
                DocumentSource.document_id == document_id,
                DocumentSource.source_type == "upload",
                DocumentSource.validation_status != "rejected",
            )
        )
        return int(count or 0)

    async def list_documents(
        self,
        actor: User,
        *,
        workspace_id: UUID,
        page: int = 1,
        page_size: int = 20,
    ) -> DocumentListResponse:
        """List non-deleted workspace documents filtered by Organization Document Policy."""
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

        filters = [
            Document.workspace_id == workspace_id,
            Document.deleted_at.is_(None),
        ]
        # Member role can only see documents they own; Editor/Admin/Owner can list org docs.
        if workspace.type == "personal" or member.role == "member":
            filters.append(Document.created_by_user_id == actor.id)

        total = int(
            await self._session.scalar(select(func.count(Document.id)).where(*filters)) or 0
        )
        offset = (page - 1) * page_size
        rows = (
            await self._session.scalars(
                select(Document)
                .where(*filters)
                .order_by(Document.created_at.desc(), Document.id.desc())
                .offset(offset)
                .limit(page_size)
            )
        ).all()
        return DocumentListResponse(
            items=[_to_document_summary(doc) for doc in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    async def create_document(
        self,
        actor: User,
        *,
        workspace_id: UUID,
        payload: DocumentCreateRequest,
    ) -> DocumentDetailResponse:
        """Create a new workspace document container (and optional initial v1 content)."""
        workspace, member = await require_active_workspace_membership(
            self._session,
            workspace_id=workspace_id,
            user_id=actor.id,
        )
        require_workspace_action(
            role=cast(WorkspaceRole, member.role),
            workspace_type=cast(WorkspaceType, workspace.type),
            action="create_document",
        )
        if payload.selected_template_version_id is not None:
            await self._validate_selected_template_version(
                workspace=workspace,
                template_version_id=payload.selected_template_version_id,
            )

        now = datetime.now(UTC)
        document = Document(
            workspace_id=workspace_id,
            created_by_user_id=actor.id,
            document_type=payload.document_type,
            title=payload.title,
            status="draft",
            selected_template_version_id=payload.selected_template_version_id,
            created_at=now,
            updated_at=now,
        )
        self._session.add(document)
        await self._session.flush()

        current_version: DocumentVersion | None = None
        if payload.initial_content is not None:
            current_version = DocumentVersion(
                document_id=document.id,
                version_number=1,
                structured_content=dict(payload.initial_content),
                content_hash=_hash_canonical_json(dict(payload.initial_content)),
                created_by_user_id=actor.id,
                created_at=now,
                approval_status="pending",
            )
            self._session.add(current_version)
            await self._session.flush()
            document.current_version_id = current_version.id

        record_workspace_audit(
            self._session,
            workspace_id=workspace_id,
            actor_user_id=actor.id,
            action="documents.created",
            resource_type="document",
            resource_id=str(document.id),
            metadata={"document_type": document.document_type},
        )
        await self._session.commit()
        await self._session.refresh(document)
        if current_version is not None:
            await self._session.refresh(current_version)

        summary = _to_document_summary(document)
        readiness = (
            evaluate_meeting_minutes_readiness(
                current_version.structured_content if current_version else None
            )
            if document.document_type == "meeting_minutes"
            else None
        )
        return DocumentDetailResponse(
            **summary.model_dump(),
            current_version=_to_version_response(current_version) if current_version else None,
            sources_count=0,
            jobs=[],
            review_readiness=readiness,
        )

    async def get_document(self, actor: User, *, document_id: UUID) -> DocumentDetailResponse:
        """Fetch document details, current version snapshot, source count, and jobs."""
        document, _, _ = await self._require_authorized_document(
            actor,
            document_id=document_id,
            action="view",
        )
        current_version: DocumentVersion | None = None
        if document.current_version_id is not None:
            current_version = await self._session.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.id == document.current_version_id,
                    DocumentVersion.document_id == document.id,
                )
            )
        sources_count = int(
            await self._session.scalar(
                select(func.count(DocumentSource.id)).where(
                    DocumentSource.document_id == document.id
                )
            )
            or 0
        )
        jobs = (
            await self._session.scalars(
                select(Job)
                .where(Job.document_id == document.id)
                .order_by(Job.created_at.desc(), Job.id.desc())
            )
        ).all()
        summary = _to_document_summary(document)
        readiness = (
            evaluate_meeting_minutes_readiness(
                current_version.structured_content if current_version else None
            )
            if document.document_type == "meeting_minutes"
            else None
        )
        return DocumentDetailResponse(
            **summary.model_dump(),
            current_version=_to_version_response(current_version) if current_version else None,
            sources_count=sources_count,
            jobs=[_to_job_response(job) for job in jobs],
            review_readiness=readiness,
        )

    async def _normalize_and_validate_review_content(
        self,
        *,
        document: Document,
        current_version: DocumentVersion | None,
        payload: DocumentUpdateRequest,
    ) -> tuple[dict[str, object], bool, bool]:
        """Validate human edits, enforce server-side provenance, and detect no-op saves."""
        assert payload.structured_content is not None
        submitted_keys = set(payload.structured_content.keys())
        current_keys = (
            set(current_version.structured_content.keys())
            if current_version is not None
            else set()
        )
        should_validate_review = (
            document.status in ("review_required", "approved", "completed")
            or bool(payload.resolved_conflict_fields)
            or bool(submitted_keys & _REVIEW_SCHEMA_KEYS)
            or bool(current_keys & _REVIEW_SCHEMA_KEYS)
        )
        if not should_validate_review:
            raw_dict = dict(payload.structured_content)
            is_noop = (
                current_version is not None
                and _hash_canonical_json(raw_dict) == current_version.content_hash
            )
            return raw_dict, False, is_noop

        try:
            parsed = MeetingMinutesReviewContent.model_validate(payload.structured_content)
        except ValidationError as exc:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Struktur konten tinjauan dokumen tidak valid.",
                status_code=422,
            ) from exc

        existing_versions = (
            await self._session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(DocumentVersion.version_number.asc())
            )
        ).all()

        ai_grounded_scalars: dict[str, list[tuple[str, list[ReviewProvenanceEntry]]]] = {
            f: [] for f in _REVIEW_SCALAR_FIELDS
        }
        ai_grounded_lists: dict[str, list[tuple[tuple[str, ...], list[ReviewProvenanceEntry]]]] = {
            f: [] for f in _REVIEW_SIMPLE_LIST_FIELDS
        }
        ai_conflict_provenance: dict[str, list[ReviewProvenanceEntry]] = {}
        max_item_counts: dict[str, Counter[tuple[object, ...]]] = {
            sec: Counter() for sec in _REVIEW_COMPLEX_LIST_FIELDS
        }
        ai_item_provenance: dict[tuple[str, UUID, str], list[ReviewProvenanceEntry]] = {}
        server_conflicts_by_field: dict[str, ReviewConflictItem] = {}
        server_system_warnings: list[str] = []

        for ver in existing_versions:
            try:
                ver_parsed = MeetingMinutesReviewContent.model_validate(ver.structured_content)
            except ValidationError:
                continue

            for conflict in ver_parsed.conflicts:
                server_conflicts_by_field[conflict.field_path] = conflict

            if ver.created_by_user_id is not None:
                continue

            for warning in ver_parsed.warnings:
                cleaned_w = warning.strip()
                if (
                    cleaned_w
                    and not cleaned_w.startswith("Konflik terdeteksi pada bidang ")
                    and cleaned_w not in server_system_warnings
                ):
                    server_system_warnings.append(cleaned_w)

            for field in _REVIEW_SCALAR_FIELDS:
                val = _clean_optional_str(getattr(ver_parsed, field))
                provs = [
                    p
                    for p in ver_parsed.provenance
                    if _provenance_matches_field(p.field_path, field)
                ]
                if provs:
                    ai_conflict_provenance.setdefault(field, []).extend(provs)
                if ver_parsed.field_statuses.get(field) == "grounded" and val is not None:
                    ai_grounded_scalars[field].append((val, provs))

            for field in _REVIEW_SIMPLE_LIST_FIELDS:
                items = _clean_string_list(getattr(ver_parsed, field))
                provs = [
                    p
                    for p in ver_parsed.provenance
                    if _provenance_matches_field(p.field_path, field)
                ]
                if provs:
                    ai_conflict_provenance.setdefault(field, []).extend(provs)
                if ver_parsed.field_statuses.get(field) == "grounded" and items:
                    ai_grounded_lists[field].append((tuple(items), provs))

            ver_counts: dict[str, Counter[tuple[object, ...]]] = {
                sec: Counter() for sec in _REVIEW_COMPLEX_LIST_FIELDS
            }
            for op in ver_parsed.opinions:
                exc_clean = _clean_optional_str(op.excerpt)
                if op.source_id is not None and exc_clean is not None:
                    sig: tuple[object, ...] = (
                        op.statement.strip(),
                        _clean_optional_str(op.speaker),
                        op.source_id,
                        exc_clean,
                    )
                    ver_counts["opinions"][sig] += 1
            for prop in ver_parsed.proposals:
                exc_clean = _clean_optional_str(prop.excerpt)
                if prop.source_id is not None and exc_clean is not None:
                    sig = (
                        prop.proposal.strip(),
                        _clean_optional_str(prop.proposer),
                        prop.status,
                        prop.source_id,
                        exc_clean,
                    )
                    ver_counts["proposals"][sig] += 1
            for dec in ver_parsed.decisions:
                exc_clean = _clean_optional_str(dec.excerpt)
                if dec.source_id is not None and exc_clean is not None:
                    sig = (dec.decision.strip(), dec.source_id, exc_clean)
                    ver_counts["decisions"][sig] += 1
            for act in ver_parsed.action_items:
                exc_clean = _clean_optional_str(act.excerpt)
                if act.source_id is not None and exc_clean is not None:
                    sig = (
                        act.task.strip(),
                        _clean_optional_str(act.assignee),
                        _clean_optional_str(act.due_date),
                        act.source_id,
                        exc_clean,
                    )
                    ver_counts["action_items"][sig] += 1

            for sec in _REVIEW_COMPLEX_LIST_FIELDS:
                for sig, cnt in ver_counts[sec].items():
                    if cnt > max_item_counts[sec][sig]:
                        max_item_counts[sec][sig] = cnt

            for prov in ver_parsed.provenance:
                for sec in _REVIEW_COMPLEX_LIST_FIELDS:
                    if _provenance_matches_field(prov.field_path, sec):
                        key = (sec, prov.source_id, prov.excerpt.strip())
                        ai_item_provenance.setdefault(key, []).append(prov)

        current_parsed: MeetingMinutesReviewContent | None = None
        if current_version is not None:
            try:
                current_parsed = MeetingMinutesReviewContent.model_validate(
                    current_version.structured_content
                )
            except ValidationError:
                current_parsed = None

        if current_parsed is not None and not server_system_warnings:
            for warning in current_parsed.warnings:
                cleaned_w = warning.strip()
                if (
                    cleaned_w
                    and not cleaned_w.startswith("Konflik terdeteksi pada bidang ")
                    and cleaned_w not in server_system_warnings
                ):
                    server_system_warnings.append(cleaned_w)

        current_conflicts_by_field: dict[str, ReviewConflictItem] = (
            {c.field_path: c for c in current_parsed.conflicts}
            if current_parsed is not None
            else {}
        )
        active_conflicts_by_field: dict[str, ReviewConflictItem] = dict(
            current_conflicts_by_field
        )
        for submitted_conflict in parsed.conflicts:
            if (
                submitted_conflict.field_path not in active_conflicts_by_field
                and submitted_conflict.field_path in server_conflicts_by_field
            ):
                active_conflicts_by_field[submitted_conflict.field_path] = (
                    server_conflicts_by_field[submitted_conflict.field_path]
                )

        def normalized_field_value(
            model: MeetingMinutesReviewContent, field_name: str
        ) -> object:
            if field_name in _REVIEW_SCALAR_FIELDS:
                return _clean_optional_str(cast(str | None, getattr(model, field_name)))
            if field_name in _REVIEW_SIMPLE_LIST_FIELDS:
                return _clean_string_list(cast(list[str], getattr(model, field_name)))
            return getattr(model, field_name)

        def is_non_empty_value(val: object) -> bool:
            if val is None:
                return False
            if isinstance(val, str):
                return bool(val.strip())
            if isinstance(val, list):
                return len(val) > 0
            return True

        resolved_set: set[str] = set()
        for raw_field in payload.resolved_conflict_fields:
            field_name = raw_field.strip()
            if field_name not in _ALL_REVIEW_CONTENT_FIELDS:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=f"Bidang konflik '{raw_field}' tidak dikenal.",
                    status_code=422,
                )
            if not is_non_empty_value(normalized_field_value(parsed, field_name)):
                raise AppError(
                    code="VALIDATION_ERROR",
                    message=f"Bidang konflik '{field_name}' tidak boleh kosong saat diselesaikan.",
                    status_code=422,
                )
            resolved_set.add(field_name)

        resolved_by_human: set[str] = set()
        remaining_conflicts: list[ReviewConflictItem] = []
        remaining_conflict_fields: set[str] = set()
        for field_path, conflict_item in active_conflicts_by_field.items():
            sub_val = normalized_field_value(parsed, field_path)
            sub_non_empty = is_non_empty_value(sub_val)
            cur_val = (
                normalized_field_value(current_parsed, field_path)
                if current_parsed is not None
                else None
            )
            if not sub_non_empty:
                is_resolved = False
            elif field_path in resolved_set or (
                field_path in current_conflicts_by_field
                and current_parsed is not None
                and sub_val != cur_val
            ):
                is_resolved = True
            else:
                is_resolved = False

            if is_resolved:
                resolved_by_human.add(field_path)
            else:
                remaining_conflicts.append(conflict_item)
                remaining_conflict_fields.add(field_path)

        new_field_statuses: dict[str, ReviewFieldStatus] = {}
        new_provenance: list[ReviewProvenanceEntry] = []
        seen_prov_keys: set[tuple[str, UUID, str]] = set()

        def append_prov(entries: Sequence[ReviewProvenanceEntry]) -> None:
            for entry in entries:
                key = (entry.field_path, entry.source_id, entry.excerpt.strip())
                if key not in seen_prov_keys and len(new_provenance) < 300:
                    seen_prov_keys.add(key)
                    new_provenance.append(entry)

        normalized_scalars: dict[str, str | None] = {}
        for field in _REVIEW_SCALAR_FIELDS:
            sub_scalar = _clean_optional_str(cast(str | None, getattr(parsed, field)))
            normalized_scalars[field] = sub_scalar
            if field in remaining_conflict_fields:
                new_field_statuses[field] = "conflicting"
                append_prov(ai_conflict_provenance.get(field, []))
            elif field in resolved_by_human:
                new_field_statuses[field] = "user_provided"
            elif sub_scalar is None:
                new_field_statuses[field] = "missing"
            else:
                matching_ai = [
                    provs for val, provs in ai_grounded_scalars[field] if val == sub_scalar
                ]
                cur_was_grounded = (
                    current_parsed is not None
                    and current_parsed.field_statuses.get(field) == "grounded"
                    and _clean_optional_str(cast(str | None, getattr(current_parsed, field)))
                    == sub_scalar
                )
                loaded_hist_grounded = parsed.field_statuses.get(field) == "grounded"
                if matching_ai and (cur_was_grounded or loaded_hist_grounded):
                    new_field_statuses[field] = "grounded"
                    append_prov(matching_ai[-1])
                else:
                    new_field_statuses[field] = "user_provided"

        normalized_lists: dict[str, list[str]] = {}
        for field in _REVIEW_SIMPLE_LIST_FIELDS:
            sub_list = _clean_string_list(cast(list[str], getattr(parsed, field)))
            normalized_lists[field] = sub_list
            if field in remaining_conflict_fields:
                new_field_statuses[field] = "conflicting"
                append_prov(ai_conflict_provenance.get(field, []))
            elif field in resolved_by_human:
                new_field_statuses[field] = "user_provided"
            elif not sub_list:
                new_field_statuses[field] = "missing"
            else:
                sub_tuple = tuple(sub_list)
                matching_ai = [
                    provs for vals, provs in ai_grounded_lists[field] if vals == sub_tuple
                ]
                cur_was_grounded = (
                    current_parsed is not None
                    and current_parsed.field_statuses.get(field) == "grounded"
                    and _clean_string_list(cast(list[str], getattr(current_parsed, field)))
                    == sub_list
                )
                loaded_hist_grounded = parsed.field_statuses.get(field) == "grounded"
                if matching_ai and (cur_was_grounded or loaded_hist_grounded):
                    new_field_statuses[field] = "grounded"
                    append_prov(matching_ai[-1])
                else:
                    new_field_statuses[field] = "user_provided"

        remaining_allowance = {
            sec: Counter(cnts) for sec, cnts in max_item_counts.items()
        }

        norm_opinions: list[ReviewOpinionItem] = []
        for op in parsed.opinions:
            stmt = op.statement.strip()
            spk = _clean_optional_str(op.speaker)
            excerpt_clean = _clean_optional_str(op.excerpt)
            if op.source_id is not None and excerpt_clean is not None:
                sig = (stmt, spk, op.source_id, excerpt_clean)
                if remaining_allowance["opinions"][sig] > 0:
                    remaining_allowance["opinions"][sig] -= 1
                    norm_opinions.append(
                        ReviewOpinionItem(
                            statement=stmt,
                            speaker=spk,
                            origin="grounded",
                            source_id=op.source_id,
                            excerpt=excerpt_clean,
                        )
                    )
                    append_prov(
                        ai_item_provenance.get(("opinions", op.source_id, excerpt_clean), [])
                    )
                    continue
            norm_opinions.append(
                ReviewOpinionItem(
                    statement=stmt,
                    speaker=spk,
                    origin="user_provided",
                    source_id=None,
                    excerpt=None,
                )
            )
        if "opinions" in remaining_conflict_fields:
            new_field_statuses["opinions"] = "conflicting"
        elif not norm_opinions:
            new_field_statuses["opinions"] = "missing"
        elif all(item.origin == "grounded" for item in norm_opinions):
            new_field_statuses["opinions"] = "grounded"
        else:
            new_field_statuses["opinions"] = "user_provided"

        norm_proposals: list[ReviewProposalItem] = []
        for prop in parsed.proposals:
            prop_text = prop.proposal.strip()
            proposer = _clean_optional_str(prop.proposer)
            excerpt_clean = _clean_optional_str(prop.excerpt)
            if prop.source_id is not None and excerpt_clean is not None:
                sig = (prop_text, proposer, prop.status, prop.source_id, excerpt_clean)
                if remaining_allowance["proposals"][sig] > 0:
                    remaining_allowance["proposals"][sig] -= 1
                    norm_proposals.append(
                        ReviewProposalItem(
                            proposal=prop_text,
                            proposer=proposer,
                            status=prop.status,
                            origin="grounded",
                            source_id=prop.source_id,
                            excerpt=excerpt_clean,
                        )
                    )
                    append_prov(
                        ai_item_provenance.get(("proposals", prop.source_id, excerpt_clean), [])
                    )
                    continue
            norm_proposals.append(
                ReviewProposalItem(
                    proposal=prop_text,
                    proposer=proposer,
                    status=prop.status,
                    origin="user_provided",
                    source_id=None,
                    excerpt=None,
                )
            )
        if "proposals" in remaining_conflict_fields:
            new_field_statuses["proposals"] = "conflicting"
        elif not norm_proposals:
            new_field_statuses["proposals"] = "missing"
        elif all(item.origin == "grounded" for item in norm_proposals):
            new_field_statuses["proposals"] = "grounded"
        else:
            new_field_statuses["proposals"] = "user_provided"

        norm_decisions: list[ReviewDecisionItem] = []
        for dec in parsed.decisions:
            dec_text = dec.decision.strip()
            excerpt_clean = _clean_optional_str(dec.excerpt)
            if dec.source_id is not None and excerpt_clean is not None:
                sig = (dec_text, dec.source_id, excerpt_clean)
                if remaining_allowance["decisions"][sig] > 0:
                    remaining_allowance["decisions"][sig] -= 1
                    norm_decisions.append(
                        ReviewDecisionItem(
                            decision=dec_text,
                            origin="grounded",
                            source_id=dec.source_id,
                            excerpt=excerpt_clean,
                        )
                    )
                    append_prov(
                        ai_item_provenance.get(("decisions", dec.source_id, excerpt_clean), [])
                    )
                    continue
            norm_decisions.append(
                ReviewDecisionItem(
                    decision=dec_text,
                    origin="user_provided",
                    source_id=None,
                    excerpt=None,
                )
            )
        if "decisions" in remaining_conflict_fields:
            new_field_statuses["decisions"] = "conflicting"
        elif not norm_decisions:
            new_field_statuses["decisions"] = "missing"
        elif all(item.origin == "grounded" for item in norm_decisions):
            new_field_statuses["decisions"] = "grounded"
        else:
            new_field_statuses["decisions"] = "user_provided"

        norm_actions: list[ReviewActionItem] = []
        for act in parsed.action_items:
            task_text = act.task.strip()
            assignee = _clean_optional_str(act.assignee)
            due_date = _clean_optional_str(act.due_date)
            excerpt_clean = _clean_optional_str(act.excerpt)
            if act.source_id is not None and excerpt_clean is not None:
                sig = (task_text, assignee, due_date, act.source_id, excerpt_clean)
                if remaining_allowance["action_items"][sig] > 0:
                    remaining_allowance["action_items"][sig] -= 1
                    norm_actions.append(
                        ReviewActionItem(
                            task=task_text,
                            assignee=assignee,
                            due_date=due_date,
                            origin="grounded",
                            source_id=act.source_id,
                            excerpt=excerpt_clean,
                        )
                    )
                    append_prov(
                        ai_item_provenance.get(
                            ("action_items", act.source_id, excerpt_clean), []
                        )
                    )
                    continue
            norm_actions.append(
                ReviewActionItem(
                    task=task_text,
                    assignee=assignee,
                    due_date=due_date,
                    origin="user_provided",
                    source_id=None,
                    excerpt=None,
                )
            )
        if "action_items" in remaining_conflict_fields:
            new_field_statuses["action_items"] = "conflicting"
        elif not norm_actions:
            new_field_statuses["action_items"] = "missing"
        elif all(item.origin == "grounded" for item in norm_actions):
            new_field_statuses["action_items"] = "grounded"
        else:
            new_field_statuses["action_items"] = "user_provided"

        final_warnings = list(server_system_warnings)
        for conflict in remaining_conflicts:
            c_warn = f"Konflik terdeteksi pada bidang '{conflict.field_path}'."
            if c_warn not in final_warnings:
                final_warnings.append(c_warn)

        normalized_model = MeetingMinutesReviewContent(
            meeting_title=normalized_scalars["meeting_title"],
            meeting_date=normalized_scalars["meeting_date"],
            meeting_time=normalized_scalars["meeting_time"],
            location=normalized_scalars["location"],
            organizer=normalized_scalars["organizer"],
            chairperson=normalized_scalars["chairperson"],
            attendees=normalized_lists["attendees"],
            absentees=normalized_lists["absentees"],
            agenda_items=normalized_lists["agenda_items"],
            discussion_summary=normalized_scalars["discussion_summary"],
            opinions=norm_opinions,
            proposals=norm_proposals,
            decisions=norm_decisions,
            action_items=norm_actions,
            field_statuses=new_field_statuses,
            conflicts=remaining_conflicts,
            provenance=new_provenance,
            warnings=final_warnings[:50],
        )
        normalized_dict = cast(dict[str, object], normalized_model.model_dump(mode="json"))

        is_noop = False
        if current_version is not None and not resolved_by_human:
            raw_hash = _hash_canonical_json(dict(payload.structured_content))
            new_hash = _hash_canonical_json(normalized_dict)
            if (
                raw_hash == current_version.content_hash
                or new_hash == current_version.content_hash
                or (
                    current_parsed is not None
                    and current_version.created_by_user_id is None
                    and _content_fields_signature(normalized_model)
                    == _content_fields_signature(current_parsed)
                    and {c.field_path for c in remaining_conflicts}
                    == {c.field_path for c in current_parsed.conflicts}
                )
            ):
                is_noop = True

        return normalized_dict, True, is_noop

    async def update_document(
        self,
        actor: User,
        *,
        document_id: UUID,
        payload: DocumentUpdateRequest,
    ) -> DocumentDetailResponse:
        """Update document metadata or create a new version on substantive structured edit."""
        document, workspace, _ = await self._require_authorized_document(
            actor,
            document_id=document_id,
            action="edit",
            lock_document=True,
        )
        if document.status in _IMMUTABLE_WHILE_ACTIVE_STATUSES:
            raise AppError(
                code="CONFLICT",
                message="Dokumen sedang diproses atau diarsipkan dan tidak dapat diubah.",
                status_code=409,
            )

        current_version: DocumentVersion | None = None
        if document.current_version_id is not None:
            current_version = await self._session.scalar(
                select(DocumentVersion)
                .where(
                    DocumentVersion.id == document.current_version_id,
                    DocumentVersion.document_id == document.id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )

        if (
            payload.expected_version_id is not None
            and document.current_version_id != payload.expected_version_id
        ):
            raise AppError(
                code="CONFLICT",
                message="Versi dokumen telah berubah. Muat ulang dokumen sebelum menyimpan.",
                status_code=409,
            )
        if payload.expected_content_hash is not None and (
            current_version is None
            or current_version.content_hash.lower() != payload.expected_content_hash.strip().lower()
        ):
            raise AppError(
                code="CONFLICT",
                message=(
                    "Hash konten versi dokumen tidak cocok. Muat ulang dokumen sebelum menyimpan."
                ),
                status_code=409,
            )

        if payload.selected_template_version_id is not None:
            await self._validate_selected_template_version(
                workspace=workspace,
                template_version_id=payload.selected_template_version_id,
            )
            document.selected_template_version_id = payload.selected_template_version_id

        if payload.title is not None:
            document.title = payload.title

        now = datetime.now(UTC)
        created_version_number: int | None = None
        approval_invalidated = False
        previous_status = document.status
        superseded_version_id: UUID | None = None
        new_version_id: UUID | None = None

        if payload.structured_content is not None:
            (
                normalized_content,
                is_review_schema_content,
                is_noop,
            ) = await self._normalize_and_validate_review_content(
                document=document,
                current_version=current_version,
                payload=payload,
            )
            if not is_noop:
                max_ver = await self._session.scalar(
                    select(func.max(DocumentVersion.version_number)).where(
                        DocumentVersion.document_id == document.id
                    )
                )
                next_number = int(max_ver or 0) + 1
                created_version_number = next_number

                if current_version is not None and current_version.approval_status in (
                    "pending",
                    "approved",
                ):
                    current_version.approval_status = "superseded"
                    if document.approved_version_id == current_version.id:
                        superseded_version_id = current_version.id

                if document.approved_version_id is not None:
                    approval_invalidated = True
                    if (
                        current_version is None
                        or document.approved_version_id != current_version.id
                    ):
                        prev_approved = await self._session.scalar(
                            select(DocumentVersion)
                            .where(
                                DocumentVersion.id == document.approved_version_id,
                                DocumentVersion.document_id == document.id,
                            )
                            .with_for_update()
                            .execution_options(populate_existing=True)
                        )
                        if prev_approved is not None:
                            prev_approved.approval_status = "superseded"
                            superseded_version_id = prev_approved.id
                    document.approved_version_id = None

                if (
                    document.status in ("approved", "completed", "failed", "review_required")
                    or is_review_schema_content
                ):
                    document.status = "review_required"

                new_version = DocumentVersion(
                    document_id=document.id,
                    version_number=next_number,
                    structured_content=normalized_content,
                    content_hash=_hash_canonical_json(normalized_content),
                    created_by_user_id=actor.id,
                    created_at=now,
                    approval_status="pending",
                )
                self._session.add(new_version)
                await self._session.flush()
                document.current_version_id = new_version.id
                new_version_id = new_version.id

        document.updated_at = now
        audit_meta: dict[str, object] = {
            "versioned_edit": created_version_number is not None,
            "previous_status": previous_status,
            "approval_invalidated": approval_invalidated,
        }
        if created_version_number is not None:
            audit_meta["version_number"] = created_version_number
        if superseded_version_id is not None:
            audit_meta["superseded_version_id"] = str(superseded_version_id)
        if new_version_id is not None:
            audit_meta["new_version_id"] = str(new_version_id)
        if payload.resolved_conflict_fields:
            audit_meta["resolved_conflicts"] = list(payload.resolved_conflict_fields)

        record_workspace_audit(
            self._session,
            workspace_id=document.workspace_id,
            actor_user_id=actor.id,
            action="documents.updated",
            resource_type="document",
            resource_id=str(document.id),
            metadata=audit_meta,
        )
        await self._session.commit()
        return await self.get_document(actor, document_id=document.id)

    async def approve_document(
        self,
        actor: User,
        *,
        document_id: UUID,
        payload: ApproveDocumentRequest,
    ) -> DocumentDetailResponse:
        """Explicitly approve the exact current document version after precondition checks."""
        document, _, _ = await self._require_authorized_document(
            actor,
            document_id=document_id,
            action="approve",
            lock_document=True,
        )
        if document.current_version_id is None:
            raise AppError(
                code="CONFLICT",
                message="Dokumen belum memiliki versi draf untuk disetujui.",
                status_code=409,
            )

        current_version = await self._session.scalar(
            select(DocumentVersion)
            .where(
                DocumentVersion.id == document.current_version_id,
                DocumentVersion.document_id == document.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if current_version is None:
            raise AppError(
                code="CONFLICT",
                message="Versi dokumen saat ini tidak ditemukan.",
                status_code=409,
            )

        if (
            payload.version_id != current_version.id
            or payload.content_hash.strip().lower() != current_version.content_hash.lower()
        ):
            raise AppError(
                code="CONFLICT",
                message="Versi atau hash konten dokumen tidak sesuai dengan versi terbaru.",
                status_code=409,
            )

        if (
            document.status == "approved"
            and document.approved_version_id == current_version.id
            and current_version.approval_status == "approved"
        ):
            return await self.get_document(actor, document_id=document.id)

        if document.status != "review_required":
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Hanya dokumen dengan status review_required yang dapat disetujui.",
                status_code=409,
            )

        readiness = evaluate_meeting_minutes_readiness(current_version.structured_content)
        if not readiness.is_ready_for_approval:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Dokumen belum memenuhi prasyarat persetujuan.",
                status_code=422,
                details=readiness.model_dump(),
            )

        now = datetime.now(UTC)
        current_version.approval_status = "approved"
        current_version.approved_by_user_id = actor.id
        current_version.approved_at = now
        document.approved_version_id = current_version.id
        document.status = "approved"
        document.updated_at = now

        record_workspace_audit(
            self._session,
            workspace_id=document.workspace_id,
            actor_user_id=actor.id,
            action="documents.approved",
            resource_type="document",
            resource_id=str(document.id),
            metadata={
                "version_id": str(current_version.id),
                "version_number": current_version.version_number,
                "content_hash": current_version.content_hash,
            },
        )
        await self._session.commit()
        return await self.get_document(actor, document_id=document.id)

    async def delete_document(
        self,
        actor: User,
        *,
        document_id: UUID,
    ) -> DeleteDocumentResponse:
        """Archive/soft-delete a document owned or manageable under workspace RBAC."""
        document, _, _ = await self._require_authorized_document(
            actor,
            document_id=document_id,
            action="delete",
            lock_document=True,
        )
        if document.status in ("processing", "rendering"):
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Dokumen yang sedang diproses tidak dapat dihapus.",
                status_code=409,
            )

        now = datetime.now(UTC)
        document.status = "archived"
        document.deleted_at = now
        document.updated_at = now
        record_workspace_audit(
            self._session,
            workspace_id=document.workspace_id,
            actor_user_id=actor.id,
            action="documents.archived",
            resource_type="document",
            resource_id=str(document.id),
            metadata={},
        )
        await self._session.commit()
        return DeleteDocumentResponse(
            id=document.id,
            status="archived",
            deleted_at=now,
        )

    async def create_upload_intent(
        self,
        actor: User,
        *,
        workspace_id: UUID,
        document_id: UUID,
        payload: UploadIntentRequest,
    ) -> UploadIntentResponse:
        """Authorize document upload and issue workspace-bound presigned upload URL."""
        document, _, _ = await self._require_authorized_document(
            actor,
            document_id=document_id,
            action="submit_source",
            expected_workspace_id=workspace_id,
        )
        if document.status in _IMMUTABLE_WHILE_ACTIVE_STATUSES:
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Sumber tidak dapat ditambahkan saat dokumen sedang diproses.",
                status_code=409,
            )

        current_uploads = await self._count_upload_sources(document.id)
        if current_uploads >= self._settings.max_source_files_per_document:
            raise AppError(
                code="UPLOAD_REJECTED",
                message="Jumlah maksimum file sumber per dokumen telah tercapai.",
                status_code=422,
                details={"max_files": self._settings.max_source_files_per_document},
            )

        _, normalized_mime, clean_name = validate_upload_declaration(
            filename=payload.filename,
            content_type=payload.content_type,
            size_bytes=payload.size_bytes,
            max_file_size_bytes=self._settings.max_upload_file_size_bytes,
        )
        object_key = self._storage.build_document_source_key(
            workspace_id=workspace_id,
            document_id=document_id,
            filename=clean_name,
        )
        endpoint_base = (
            f"{self._settings.object_storage_endpoint.rstrip('/')}/"
            f"{self._settings.object_storage_bucket.strip('/')}"
        )
        intent = self._storage.create_upload_intent(
            object_key=object_key,
            content_type=normalized_mime,
            size_bytes=payload.size_bytes,
            signing_secret=self._settings.internal_hmac_secret,
            endpoint_base_url=endpoint_base,
        )
        return UploadIntentResponse(
            object_key=intent.object_key,
            upload_url=intent.upload_url,
            method=intent.method,
            required_headers=intent.required_headers,
            expires_at=intent.expires_at,
            max_file_size_bytes=self._settings.max_upload_file_size_bytes,
        )

    async def submit_source(
        self,
        actor: User,
        *,
        workspace_id: UUID,
        document_id: UUID,
        payload: DocumentSourceSubmitRequest,
    ) -> DocumentSourceResponse:
        """Confirm uploaded source object (strictly validated) or store form/raw text input."""
        document, _, _ = await self._require_authorized_document(
            actor,
            document_id=document_id,
            action="submit_source",
            expected_workspace_id=workspace_id,
            lock_document=True,
        )
        if document.status in _IMMUTABLE_WHILE_ACTIVE_STATUSES:
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Sumber tidak dapat ditambahkan saat dokumen sedang diproses.",
                status_code=409,
            )

        now = datetime.now(UTC)
        if payload.source_type == "upload":
            assert payload.object_key is not None
            assert payload.filename is not None
            assert payload.content_type is not None

            current_uploads = await self._count_upload_sources(document.id)
            if current_uploads >= self._settings.max_source_files_per_document:
                raise AppError(
                    code="UPLOAD_REJECTED",
                    message="Jumlah maksimum file sumber per dokumen telah tercapai.",
                    status_code=422,
                    details={"max_files": self._settings.max_source_files_per_document},
                )

            self._storage.verify_document_source_key(
                object_key=payload.object_key,
                workspace_id=workspace_id,
                document_id=document_id,
            )
            existing_key = await self._session.scalar(
                select(DocumentSource.id).where(
                    DocumentSource.document_id == document.id,
                    DocumentSource.storage_object_key == payload.object_key,
                )
            )
            if existing_key is not None:
                raise AppError(
                    code="CONFLICT",
                    message="Object key sumber tersebut sudah dikonfirmasi pada dokumen ini.",
                    status_code=409,
                )

            stored = self._storage.get_object(object_key=payload.object_key)
            if stored is None:
                raise AppError(
                    code="UPLOAD_REJECTED",
                    message="Objek unggahan tidak ditemukan di penyimpanan privat.",
                    status_code=422,
                )
            if normalize_content_type(stored.content_type) != normalize_content_type(
                payload.content_type
            ):
                raise AppError(
                    code="UPLOAD_REJECTED",
                    message="Tipe MIME objek tersimpan tidak sesuai dengan konfirmasi unggahan.",
                    status_code=422,
                )

            validated = validate_uploaded_content(
                filename=payload.filename,
                content_type=payload.content_type,
                data=stored.content,
                max_file_size_bytes=self._settings.max_upload_file_size_bytes,
                max_archive_expansion_ratio=self._settings.max_archive_expansion_ratio,
                max_zip_entries=self._settings.max_zip_entries,
            )
            clean_name = sanitize_filename(payload.filename)
            source = DocumentSource(
                workspace_id=workspace_id,
                document_id=document.id,
                document_version_id=document.current_version_id,
                source_type="upload",
                storage_object_key=payload.object_key,
                source_name=payload.source_name or clean_name,
                extracted_text_reference=validated.extracted_text[:8000] or None,
                content_hash=validated.content_hash,
                metadata_json={
                    **validated.metadata,
                    "filename": clean_name,
                    "content_type": validated.content_type,
                    "size_bytes": validated.size_bytes,
                    "file_kind": validated.kind,
                },
                validation_status="valid",
                created_by_user_id=actor.id,
                created_at=now,
            )
        elif payload.source_type == "form":
            assert payload.form_data is not None
            form_dict = cast(dict[str, object], payload.form_data.model_dump(mode="json"))
            normalized_text = _format_form_extracted_text(payload.form_data)
            source = DocumentSource(
                workspace_id=workspace_id,
                document_id=document.id,
                document_version_id=document.current_version_id,
                source_type="form",
                storage_object_key=None,
                source_name=payload.source_name or payload.form_data.meeting_title,
                extracted_text_reference=normalized_text,
                content_hash=_hash_canonical_json(form_dict),
                metadata_json={"form_data": form_dict},
                validation_status="valid",
                created_by_user_id=actor.id,
                created_at=now,
            )
        else:
            assert payload.raw_text is not None
            if "\x00" in payload.raw_text:
                raise AppError(
                    code="VALIDATION_ERROR",
                    message="Teks sumber mengandung karakter biner tidak valid.",
                    status_code=422,
                )
            normalized_raw = payload.raw_text.strip()
            source = DocumentSource(
                workspace_id=workspace_id,
                document_id=document.id,
                document_version_id=document.current_version_id,
                source_type="text",
                storage_object_key=None,
                source_name=payload.source_name or "Catatan Teks",
                extracted_text_reference=normalized_raw,
                content_hash=hashlib.sha256(normalized_raw.encode("utf-8")).hexdigest(),
                metadata_json={"char_count": len(normalized_raw)},
                validation_status="valid",
                created_by_user_id=actor.id,
                created_at=now,
            )

        self._session.add(source)
        document.updated_at = now
        await self._session.flush()
        record_workspace_audit(
            self._session,
            workspace_id=workspace_id,
            actor_user_id=actor.id,
            action="documents.source_added",
            resource_type="document_source",
            resource_id=str(source.id),
            metadata={"document_id": str(document.id), "source_type": source.source_type},
        )
        await self._session.commit()
        await self._session.refresh(source)
        return _to_source_response(source)

    async def list_sources(
        self,
        actor: User,
        *,
        document_id: UUID,
    ) -> DocumentSourceListResponse:
        """List provenance/source records linked to an authorized document."""
        document, _, _ = await self._require_authorized_document(
            actor,
            document_id=document_id,
            action="view",
        )
        rows = (
            await self._session.scalars(
                select(DocumentSource)
                .where(DocumentSource.document_id == document.id)
                .order_by(DocumentSource.created_at.asc(), DocumentSource.id.asc())
            )
        ).all()
        return DocumentSourceListResponse(
            items=[_to_source_response(row) for row in rows],
            total=len(rows),
        )

    async def list_versions(
        self,
        actor: User,
        *,
        document_id: UUID,
    ) -> DocumentVersionListResponse:
        """List immutable version history for an authorized document."""
        document, _, _ = await self._require_authorized_document(
            actor,
            document_id=document_id,
            action="view",
        )
        rows = (
            await self._session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.document_id == document.id)
                .order_by(DocumentVersion.version_number.desc())
            )
        ).all()
        return DocumentVersionListResponse(
            items=[_to_version_response(row) for row in rows],
            total=len(rows),
        )

    async def start_processing(
        self,
        actor: User,
        *,
        document_id: UUID,
        payload: ProcessDocumentRequest,
    ) -> ProcessDocumentResponse:
        """Bind valid sources into a workspace source_bundle_id and queue M5 jobs."""
        document, _, _ = await self._require_authorized_document(
            actor,
            document_id=document_id,
            action="process",
            lock_document=True,
        )

        # Idempotent replay support when caller provides an idempotency_key.
        if payload.idempotency_key is not None:
            ai_idem_key = f"ai:{payload.idempotency_key}"
            existing_ai_job = await self._session.scalar(
                select(Job).where(
                    Job.workspace_id == document.workspace_id,
                    Job.idempotency_key == ai_idem_key,
                )
            )
            if existing_ai_job is not None:
                if existing_ai_job.document_id != document.id:
                    raise AppError(
                        code="IDEMPOTENCY_CONFLICT",
                        message="Idempotency key sudah digunakan pada dokumen lain.",
                        status_code=409,
                    )
                bundle_id = existing_ai_job.source_bundle_id
                assert bundle_id is not None
                bundled_jobs = (
                    await self._session.scalars(
                        select(Job)
                        .where(
                            Job.document_id == document.id,
                            Job.source_bundle_id == bundle_id,
                        )
                        .order_by(Job.created_at.asc(), Job.id.asc())
                    )
                ).all()
                existing_res = await self._session.scalar(
                    select(CreditReservation).where(
                        CreditReservation.workspace_id == document.workspace_id,
                        CreditReservation.job_id == existing_ai_job.id,
                    )
                )
                return ProcessDocumentResponse(
                    document=_to_document_summary(document),
                    source_bundle_id=bundle_id,
                    jobs=[_to_job_response(job) for job in bundled_jobs],
                    reservation_id=existing_res.id if existing_res else None,
                    reserved_credits=existing_res.amount if existing_res else 0,
                )

        if document.status not in _PROCESSABLE_DOC_STATUSES:
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Dokumen tidak berada dalam status yang dapat diproses.",
                status_code=409,
            )

        active_job_id = await self._session.scalar(
            select(Job.id).where(
                Job.document_id == document.id,
                Job.status.in_(ACTIVE_JOB_STATUSES),
            )
        )
        if active_job_id is not None:
            raise AppError(
                code="INVALID_JOB_STATE",
                message="Masih ada job aktif yang berjalan untuk dokumen ini.",
                status_code=409,
            )

        valid_sources = (
            await self._session.scalars(
                select(DocumentSource)
                .where(
                    DocumentSource.document_id == document.id,
                    DocumentSource.validation_status == "valid",
                )
                .order_by(DocumentSource.created_at.asc(), DocumentSource.id.asc())
            )
        ).all()
        if not valid_sources:
            raise AppError(
                code="VALIDATION_ERROR",
                message="Tambahkan minimal satu sumber valid sebelum memproses dokumen.",
                status_code=422,
            )

        now = datetime.now(UTC)
        source_bundle_id = uuid4()
        run_key = payload.idempotency_key or str(uuid4())
        has_uploads = False
        for src in valid_sources:
            src.source_bundle_id = source_bundle_id
            if src.source_type == "upload":
                has_uploads = True

        jobs_to_create: list[Job] = []
        if has_uploads:
            normalize_job = Job(
                workspace_id=document.workspace_id,
                document_id=document.id,
                job_type="normalize",
                status="queued",
                source_bundle_id=source_bundle_id,
                workflow_key="source_normalize_v1",
                workflow_version="v1",
                workflow_provider="python_worker",
                idempotency_key=f"norm:{run_key}",
                created_at=now,
                updated_at=now,
            )
            self._session.add(normalize_job)
            jobs_to_create.append(normalize_job)

        ai_provider_label = (
            "python" if self._settings.workflow_orchestrator_provider == "python" else "n8n"
        )
        ai_job = Job(
            workspace_id=document.workspace_id,
            document_id=document.id,
            job_type="ai_generation",
            status="queued",
            source_bundle_id=source_bundle_id,
            workflow_key=f"{document.document_type}_v1",
            workflow_version="v1",
            workflow_provider=ai_provider_label,
            idempotency_key=f"ai:{run_key}",
            created_at=now,
            updated_at=now,
        )
        self._session.add(ai_job)
        jobs_to_create.append(ai_job)

        document_id_val = document.id
        workspace_id_val = document.workspace_id

        document.status = "processing"
        document.updated_at = now
        try:
            await self._session.flush()
            ai_job_id_val = ai_job.id
            job_ids = [job.id for job in jobs_to_create]

            record_workspace_audit(
                self._session,
                workspace_id=workspace_id_val,
                actor_user_id=actor.id,
                action="documents.processing_queued",
                resource_type="document",
                resource_id=str(document_id_val),
                metadata={
                    "source_bundle_id": str(source_bundle_id),
                    "job_types": [job.job_type for job in jobs_to_create],
                },
            )

            reservation: CreditReservation | None = None
            if self._settings.ai_generation_credit_cost is not None:
                credit_service = CreditService(
                    session=self._session,
                    settings=self._settings,
                )
                reservation = await credit_service.reserve_credits(
                    workspace_id=workspace_id_val,
                    amount=self._settings.ai_generation_credit_cost,
                    idempotency_key=f"doc-process:{run_key}",
                    job_id=ai_job_id_val,
                    created_by_user_id=actor.id,
                )
            else:
                await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            if payload.idempotency_key is not None:
                existing_ai_job = await self._session.scalar(
                    select(Job).where(
                        Job.workspace_id == workspace_id_val,
                        Job.idempotency_key == f"ai:{payload.idempotency_key}",
                    )
                )
                if existing_ai_job is not None and existing_ai_job.document_id == document_id_val:
                    bundle_id = existing_ai_job.source_bundle_id
                    assert bundle_id is not None
                    reloaded_doc = await self._session.scalar(
                        select(Document).where(Document.id == document_id_val)
                    )
                    assert reloaded_doc is not None
                    bundled_jobs = (
                        await self._session.scalars(
                            select(Job)
                            .where(
                                Job.document_id == document_id_val,
                                Job.source_bundle_id == bundle_id,
                            )
                            .order_by(Job.created_at.asc(), Job.id.asc())
                        )
                    ).all()
                    existing_res = await self._session.scalar(
                        select(CreditReservation).where(
                            CreditReservation.workspace_id == workspace_id_val,
                            CreditReservation.job_id == existing_ai_job.id,
                        )
                    )
                    return ProcessDocumentResponse(
                        document=_to_document_summary(reloaded_doc),
                        source_bundle_id=bundle_id,
                        jobs=[_to_job_response(job) for job in bundled_jobs],
                        reservation_id=existing_res.id if existing_res else None,
                        reserved_credits=existing_res.amount if existing_res else 0,
                    )
            raise AppError(
                code="IDEMPOTENCY_CONFLICT",
                message="Idempotency key sudah digunakan pada dokumen lain.",
                status_code=409,
            ) from exc

        refreshed_doc = await self._session.scalar(
            select(Document).where(Document.id == document_id_val)
        )
        assert refreshed_doc is not None
        refreshed_jobs = (
            await self._session.scalars(
                select(Job)
                .where(Job.id.in_(job_ids))
                .order_by(Job.created_at.asc(), Job.id.asc())
            )
        ).all()
        return ProcessDocumentResponse(
            document=_to_document_summary(refreshed_doc),
            source_bundle_id=source_bundle_id,
            jobs=[_to_job_response(job) for job in refreshed_jobs],
            reservation_id=reservation.id if reservation else None,
            reserved_credits=reservation.amount if reservation else 0,
        )

    async def get_processing_snapshot(
        self,
        *,
        document_id: UUID,
        source_bundle_id: UUID,
    ) -> ProcessDocumentResponse:
        """Reload freshly committed document, jobs, and credit reservation for a bundle."""
        document = await self._session.scalar(
            select(Document)
            .where(Document.id == document_id)
            .execution_options(populate_existing=True)
        )
        assert document is not None
        bundled_jobs = (
            await self._session.scalars(
                select(Job)
                .where(
                    Job.document_id == document_id,
                    Job.source_bundle_id == source_bundle_id,
                )
                .order_by(Job.created_at.asc(), Job.id.asc())
                .execution_options(populate_existing=True)
            )
        ).all()
        ai_job = next((job for job in bundled_jobs if job.job_type == "ai_generation"), None)
        reservation: CreditReservation | None = None
        if ai_job is not None:
            reservation = await self._session.scalar(
                select(CreditReservation)
                .where(
                    CreditReservation.workspace_id == document.workspace_id,
                    CreditReservation.job_id == ai_job.id,
                )
                .execution_options(populate_existing=True)
            )
        return ProcessDocumentResponse(
            document=_to_document_summary(document),
            source_bundle_id=source_bundle_id,
            jobs=[_to_job_response(job) for job in bundled_jobs],
            reservation_id=reservation.id if reservation else None,
            reserved_credits=reservation.amount if reservation else 0,
        )

    async def get_job(self, actor: User, *, job_id: UUID) -> JobResponse:
        """Fetch an individual asynchronous job after verifying workspace & document policy."""
        job = await self._session.scalar(select(Job).where(Job.id == job_id))
        if job is None:
            raise AppError(
                code="NOT_FOUND",
                message="Job tidak ditemukan.",
                status_code=404,
            )

        await self._require_authorized_document(
            actor,
            document_id=job.document_id,
            action="view",
            expected_workspace_id=job.workspace_id,
        )
        return _to_job_response(job)
