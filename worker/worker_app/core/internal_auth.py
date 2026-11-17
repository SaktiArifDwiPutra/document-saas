"""Internal HMAC-SHA256 callback signing utilities for the Python Worker."""

import hashlib
import hmac
import time


class WorkerAuthError(ValueError):
    """Raised when worker callback signing arguments are invalid."""


def build_canonical_message(
    *,
    timestamp: int,
    event_id: str,
    method: str,
    canonical_path: str,
    raw_body: bytes | str = b"",
) -> bytes:
    """Build canonical HMAC message matching FastAPI backend verification.

    Format: "{timestamp}.{event_id}.{METHOD}.{canonical_path}.{raw_body}"
    """
    normalized_event_id = event_id.strip()
    if not normalized_event_id:
        raise WorkerAuthError("event_id must not be empty")

    normalized_method = method.strip().upper()
    if not normalized_method:
        raise WorkerAuthError("HTTP method must not be empty")

    normalized_path = canonical_path.strip()
    if not normalized_path.startswith("/"):
        raise WorkerAuthError("canonical_path must start with '/'")

    body_bytes = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
    prefix = f"{timestamp}.{normalized_event_id}.{normalized_method}.{normalized_path}.".encode()
    return prefix + body_bytes


def sign_internal_request(
    *,
    secret: str,
    timestamp: int,
    event_id: str,
    method: str,
    canonical_path: str,
    raw_body: bytes | str = b"",
) -> str:
    """Compute hex-encoded HMAC-SHA256 signature for a worker-to-backend request."""
    if not secret:
        raise WorkerAuthError("HMAC secret must not be empty")

    message = build_canonical_message(
        timestamp=timestamp,
        event_id=event_id,
        method=method,
        canonical_path=canonical_path,
        raw_body=raw_body,
    )
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def build_worker_callback_headers(
    *,
    secret: str,
    event_id: str,
    canonical_path: str,
    raw_body: bytes | str,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Build authenticated headers for POST /api/v1/internal/workers/callback."""
    resolved_ts = int(time.time()) if timestamp is None else timestamp
    signature = sign_internal_request(
        secret=secret,
        timestamp=resolved_ts,
        event_id=event_id,
        method="POST",
        canonical_path=canonical_path,
        raw_body=raw_body,
    )
    return {
        "Content-Type": "application/json",
        "X-Internal-Timestamp": str(resolved_ts),
        "X-Internal-Event-Id": event_id.strip(),
        "X-Internal-Signature": signature,
    }
