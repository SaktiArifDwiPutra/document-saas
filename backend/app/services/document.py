"""Document, source provenance, validation, and job foundation service (Milestone M5)."""

import hashlib
import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

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
    DeleteDocumentResponse,
    DocumentCreateRequest,
    DocumentDetailResponse,
    DocumentListResponse,
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
    ProcessDocumentRequest,
    ProcessDocumentResponse,
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
        return DocumentDetailResponse(
            **summary.model_dump(),
            current_version=_to_version_response(current_version) if current_version else None,
            sources_count=0,
            jobs=[],
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
        return DocumentDetailResponse(
            **summary.model_dump(),
            current_version=_to_version_response(current_version) if current_version else None,
            sources_count=sources_count,
            jobs=[_to_job_response(job) for job in jobs],
        )

    async def update_document(
        self,
        actor: User,
        *,
        document_id: UUID,
        payload: DocumentUpdateRequest,
    ) -> DocumentDetailResponse:
        """Update document title/template or append a new version on structured edit."""
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

        if payload.selected_template_version_id is not None:
            await self._validate_selected_template_version(
                workspace=workspace,
                template_version_id=payload.selected_template_version_id,
            )
            document.selected_template_version_id = payload.selected_template_version_id

        if payload.title is not None:
            document.title = payload.title

        now = datetime.now(UTC)
        if payload.structured_content is not None:
            max_ver = await self._session.scalar(
                select(func.max(DocumentVersion.version_number)).where(
                    DocumentVersion.document_id == document.id
                )
            )
            next_number = int(max_ver or 0) + 1
            if document.approved_version_id is not None:
                prev_approved = await self._session.scalar(
                    select(DocumentVersion).where(
                        DocumentVersion.id == document.approved_version_id,
                        DocumentVersion.document_id == document.id,
                    )
                )
                if prev_approved is not None:
                    prev_approved.approval_status = "superseded"
                document.approved_version_id = None
                document.status = "review_required"
            elif document.status in ("approved", "completed", "failed"):
                document.status = "review_required"

            new_version = DocumentVersion(
                document_id=document.id,
                version_number=next_number,
                structured_content=dict(payload.structured_content),
                content_hash=_hash_canonical_json(dict(payload.structured_content)),
                created_by_user_id=actor.id,
                created_at=now,
                approval_status="pending",
            )
            self._session.add(new_version)
            await self._session.flush()
            document.current_version_id = new_version.id

        document.updated_at = now
        record_workspace_audit(
            self._session,
            workspace_id=document.workspace_id,
            actor_user_id=actor.id,
            action="documents.updated",
            resource_type="document",
            resource_id=str(document.id),
            metadata={"versioned_edit": payload.structured_content is not None},
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
