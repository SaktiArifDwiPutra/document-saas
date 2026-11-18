"""Object storage abstraction and workspace-bound presigned URL generation (M5)."""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import quote
from uuid import UUID, uuid4

from app.core.errors import AppError
from app.core.file_validation import sanitize_filename


@dataclass(frozen=True, slots=True)
class PresignedUploadIntent:
    """Short-lived direct upload instructions bound to a workspace-scoped object key."""

    object_key: str
    upload_url: str
    method: str
    required_headers: dict[str, str]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Stored private object payload and declared content type."""

    object_key: str
    content: bytes
    content_type: str
    size_bytes: int
    created_at: datetime


class ObjectStorageService(Protocol):
    """Protocol abstracting private R2/S3-compatible object storage operations."""

    def build_document_source_key(
        self,
        *,
        workspace_id: UUID,
        document_id: UUID,
        filename: str,
    ) -> str:
        """Construct a random workspace- and document-bound private object key."""
        ...

    def verify_document_source_key(
        self,
        *,
        object_key: str,
        workspace_id: UUID,
        document_id: UUID,
    ) -> None:
        """Raise 422/403-safe AppError if object_key does not belong to (workspace, document)."""
        ...

    def create_upload_intent(
        self,
        *,
        object_key: str,
        content_type: str,
        size_bytes: int,
        signing_secret: str,
        endpoint_base_url: str,
        ttl_seconds: int = 300,
    ) -> PresignedUploadIntent:
        """Issue a short-lived presigned upload URL and required upload headers."""
        ...

    def put_object(self, *, object_key: str, content: bytes, content_type: str) -> StoredObject:
        """Store object bytes at object_key (used by tests and local dev simulation)."""
        ...

    def get_object(self, *, object_key: str) -> StoredObject | None:
        """Fetch stored object bytes or return None when absent."""
        ...


class InMemoryObjectStorage:
    """Local/test private object store simulating workspace-scoped R2 storage."""

    def __init__(self) -> None:
        self._objects: dict[str, StoredObject] = {}

    def clear(self) -> None:
        """Reset all stored objects between isolated unit tests."""
        self._objects.clear()

    def build_document_source_key(
        self,
        *,
        workspace_id: UUID,
        document_id: UUID,
        filename: str,
    ) -> str:
        clean = sanitize_filename(filename)
        return f"workspaces/{workspace_id}/documents/{document_id}/sources/{uuid4()}/{clean}"

    def verify_document_source_key(
        self,
        *,
        object_key: str,
        workspace_id: UUID,
        document_id: UUID,
    ) -> None:
        expected_prefix = f"workspaces/{workspace_id}/documents/{document_id}/sources/"
        if (
            not object_key.startswith(expected_prefix)
            or ".." in object_key
            or "\\" in object_key
            or "\x00" in object_key
            or len(object_key) <= len(expected_prefix)
        ):
            raise AppError(
                code="UPLOAD_REJECTED",
                message="Object key sumber dokumen tidak valid untuk workspace atau dokumen ini.",
                status_code=422,
            )

    def create_upload_intent(
        self,
        *,
        object_key: str,
        content_type: str,
        size_bytes: int,
        signing_secret: str,
        endpoint_base_url: str,
        ttl_seconds: int = 300,
    ) -> PresignedUploadIntent:
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        expires_ts = int(expires_at.timestamp())
        payload = f"PUT:{object_key}:{content_type}:{size_bytes}:{expires_ts}".encode()
        sig = hmac.new(signing_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        base = endpoint_base_url.rstrip("/")
        upload_url = (
            f"{base}/storage/presigned-upload"
            f"?key={quote(object_key, safe='')}"
            f"&expires={expires_ts}"
            f"&sig={sig}"
        )
        return PresignedUploadIntent(
            object_key=object_key,
            upload_url=upload_url,
            method="PUT",
            required_headers={"Content-Type": content_type},
            expires_at=expires_at,
        )

    def put_object(self, *, object_key: str, content: bytes, content_type: str) -> StoredObject:
        obj = StoredObject(
            object_key=object_key,
            content=content,
            content_type=content_type,
            size_bytes=len(content),
            created_at=datetime.now(UTC),
        )
        self._objects[object_key] = obj
        return obj

    def get_object(self, *, object_key: str) -> StoredObject | None:
        return self._objects.get(object_key)

