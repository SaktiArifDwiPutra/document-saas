"""Internal service-to-service HMAC-SHA256 signing and verification."""

import hashlib
import hmac
import time


class InternalAuthError(ValueError):
    """Raised when internal HMAC signature or timestamp validation fails."""


class InMemoryReplayGuard:
    """In-process replay guard for event_id uniqueness within the tolerance window.

    Persistent PostgreSQL event_id deduplication (`workflow_callback_events`) is added
    alongside database job models in M5/M6.
    """

    def __init__(self, tolerance_seconds: int = 300) -> None:
        self._tolerance_seconds = tolerance_seconds
        self._seen_events: dict[str, int] = {}

    def check_and_record(self, *, event_id: str, timestamp: int, now_timestamp: int) -> None:
        """Reject previously seen event_id values and prune expired entries."""
        cutoff = now_timestamp - self._tolerance_seconds
        expired = [eid for eid, ts in self._seen_events.items() if ts < cutoff]
        for eid in expired:
            del self._seen_events[eid]

        normalized_event_id = event_id.strip()
        if normalized_event_id in self._seen_events:
            raise InternalAuthError("Replayed internal event_id detected")
        self._seen_events[normalized_event_id] = timestamp


def build_canonical_message(
    *,
    timestamp: int,
    event_id: str,
    method: str,
    canonical_path: str,
    raw_body: bytes | str = b"",
) -> bytes:
    """Build canonical HMAC signing payload binding method, path, timestamp, event_id, and body.

    Format: "{timestamp}.{event_id}.{METHOD}.{canonical_path}.{raw_body}"
    """
    normalized_event_id = event_id.strip()
    if not normalized_event_id:
        raise InternalAuthError("event_id must not be empty")

    normalized_method = method.strip().upper()
    if not normalized_method:
        raise InternalAuthError("HTTP method must not be empty")

    normalized_path = canonical_path.strip()
    if not normalized_path.startswith("/"):
        raise InternalAuthError("canonical_path must start with '/'")

    body_bytes = raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
    prefix = f"{timestamp}.{normalized_event_id}.{normalized_method}.{normalized_path}.".encode()
    return prefix + body_bytes


def compute_internal_hmac(
    *,
    secret: str,
    timestamp: int,
    event_id: str,
    method: str,
    canonical_path: str,
    raw_body: bytes | str = b"",
) -> str:
    """Compute hex-encoded HMAC-SHA256 signature over the canonical internal request."""
    if not secret:
        raise InternalAuthError("HMAC secret must not be empty")

    message = build_canonical_message(
        timestamp=timestamp,
        event_id=event_id,
        method=method,
        canonical_path=canonical_path,
        raw_body=raw_body,
    )
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_internal_hmac(
    *,
    secret: str,
    provided_signature: str,
    timestamp: int,
    event_id: str,
    method: str,
    canonical_path: str,
    raw_body: bytes | str = b"",
    tolerance_seconds: int = 300,
    now_timestamp: int | None = None,
    replay_guard: InMemoryReplayGuard | None = None,
) -> None:
    """Verify internal HMAC signature, timestamp freshness, and optional event_id replay."""
    if not provided_signature.strip():
        raise InternalAuthError("Missing HMAC signature")

    current_ts = int(time.time()) if now_timestamp is None else now_timestamp
    if abs(current_ts - timestamp) > tolerance_seconds:
        raise InternalAuthError("Internal request timestamp outside allowed tolerance window")

    expected_signature = compute_internal_hmac(
        secret=secret,
        timestamp=timestamp,
        event_id=event_id,
        method=method,
        canonical_path=canonical_path,
        raw_body=raw_body,
    )
    if not hmac.compare_digest(expected_signature, provided_signature.strip().lower()):
        raise InternalAuthError("Invalid internal HMAC signature")

    if replay_guard is not None:
        replay_guard.check_and_record(
            event_id=event_id,
            timestamp=timestamp,
            now_timestamp=current_ts,
        )
