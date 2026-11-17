"""Tests for internal HMAC-SHA256 canonical signing and verification."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from app.core.internal_auth import (
    InMemoryReplayGuard,
    InternalAuthError,
    build_canonical_message,
    compute_internal_hmac,
    verify_internal_hmac,
)

TEST_SECRET = "unit-test-hmac-secret-at-least-32-characters-long"


def _load_worker_internal_auth() -> ModuleType:
    worker_auth_path = (
        Path(__file__).resolve().parents[2] / "worker" / "worker_app" / "core" / "internal_auth.py"
    )
    spec = importlib.util.spec_from_file_location("worker_internal_auth", worker_auth_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_get_source_bundle_signature_binds_method_and_canonical_path() -> None:
    """GET requests without a body bind METHOD and canonical_path including source_bundle_id."""
    bundle_id = "11111111-2222-3333-4444-555555555555"
    other_bundle_id = "99999999-2222-3333-4444-555555555555"
    canonical_path = f"/api/v1/internal/source-bundles/{bundle_id}"
    now_ts = 1_700_000_000

    sig = compute_internal_hmac(
        secret=TEST_SECRET,
        timestamp=now_ts,
        event_id="evt-get-1",
        method="GET",
        canonical_path=canonical_path,
        raw_body=b"",
    )

    verify_internal_hmac(
        secret=TEST_SECRET,
        provided_signature=sig,
        timestamp=now_ts,
        event_id="evt-get-1",
        method="GET",
        canonical_path=canonical_path,
        raw_body=b"",
        now_timestamp=now_ts + 10,
    )

    # Replaying the same GET signature against a different source_bundle_id must fail
    with pytest.raises(InternalAuthError, match="Invalid internal HMAC signature"):
        verify_internal_hmac(
            secret=TEST_SECRET,
            provided_signature=sig,
            timestamp=now_ts,
            event_id="evt-get-1",
            method="GET",
            canonical_path=f"/api/v1/internal/source-bundles/{other_bundle_id}",
            raw_body=b"",
            now_timestamp=now_ts + 10,
        )

    # Replaying the same signature with a different HTTP method must fail
    with pytest.raises(InternalAuthError, match="Invalid internal HMAC signature"):
        verify_internal_hmac(
            secret=TEST_SECRET,
            provided_signature=sig,
            timestamp=now_ts,
            event_id="evt-get-1",
            method="POST",
            canonical_path=canonical_path,
            raw_body=b"",
            now_timestamp=now_ts + 10,
        )


def test_post_callback_signature_verification_and_tamper_rejection() -> None:
    """POST callbacks bind body, path, method, event_id, and timestamp within tolerance."""
    callback_path = "/api/v1/internal/workflows/callback"
    raw_body = b'{"job_id":"abc","event_type":"completed"}'
    now_ts = 1_700_000_000

    sig = compute_internal_hmac(
        secret=TEST_SECRET,
        timestamp=now_ts,
        event_id="evt-post-1",
        method="POST",
        canonical_path=callback_path,
        raw_body=raw_body,
    )

    verify_internal_hmac(
        secret=TEST_SECRET,
        provided_signature=sig,
        timestamp=now_ts,
        event_id="evt-post-1",
        method="POST",
        canonical_path=callback_path,
        raw_body=raw_body,
        now_timestamp=now_ts + 299,
    )

    # Tampered body fails
    with pytest.raises(InternalAuthError, match="Invalid internal HMAC signature"):
        verify_internal_hmac(
            secret=TEST_SECRET,
            provided_signature=sig,
            timestamp=now_ts,
            event_id="evt-post-1",
            method="POST",
            canonical_path=callback_path,
            raw_body=b'{"job_id":"abc","event_type":"failed"}',
            now_timestamp=now_ts + 10,
        )


def test_rejects_expired_timestamp_outside_five_minute_window() -> None:
    """Timestamps older than 300 seconds must be rejected."""
    now_ts = 1_700_000_000
    path = "/api/v1/internal/workers/callback"
    body = b'{"status":"ok"}'

    sig = compute_internal_hmac(
        secret=TEST_SECRET,
        timestamp=now_ts,
        event_id="evt-stale-1",
        method="POST",
        canonical_path=path,
        raw_body=body,
    )

    with pytest.raises(InternalAuthError, match="tolerance window"):
        verify_internal_hmac(
            secret=TEST_SECRET,
            provided_signature=sig,
            timestamp=now_ts,
            event_id="evt-stale-1",
            method="POST",
            canonical_path=path,
            raw_body=body,
            tolerance_seconds=300,
            now_timestamp=now_ts + 301,
        )


def test_canonical_message_validation() -> None:
    """Canonical message builder rejects empty event_id or non-rooted path."""
    with pytest.raises(InternalAuthError, match="event_id"):
        build_canonical_message(
            timestamp=1_700_000_000,
            event_id="   ",
            method="GET",
            canonical_path="/api/v1/internal/source-bundles/123",
        )

    with pytest.raises(InternalAuthError, match="canonical_path"):
        build_canonical_message(
            timestamp=1_700_000_000,
            event_id="evt-1",
            method="GET",
            canonical_path="api/v1/internal/source-bundles/123",
        )


def test_replay_guard_rejects_duplicate_event_id() -> None:
    """Replay guard rejects duplicate event_id within window and ignores invalid signatures."""
    guard = InMemoryReplayGuard(tolerance_seconds=300)
    now_ts = 1_700_000_000
    path = "/api/v1/internal/workers/callback"
    body = b'{"job_id":"job-1","status":"completed"}'
    sig = compute_internal_hmac(
        secret=TEST_SECRET,
        timestamp=now_ts,
        event_id="evt-replay-1",
        method="POST",
        canonical_path=path,
        raw_body=body,
    )

    # An invalid signature must not poison the replay guard slot for evt-replay-1
    with pytest.raises(InternalAuthError, match="Invalid internal HMAC signature"):
        verify_internal_hmac(
            secret=TEST_SECRET,
            provided_signature="0" * 64,
            timestamp=now_ts,
            event_id="evt-replay-1",
            method="POST",
            canonical_path=path,
            raw_body=body,
            now_timestamp=now_ts + 5,
            replay_guard=guard,
        )

    # First valid delivery succeeds
    verify_internal_hmac(
        secret=TEST_SECRET,
        provided_signature=sig,
        timestamp=now_ts,
        event_id="evt-replay-1",
        method="POST",
        canonical_path=path,
        raw_body=body,
        now_timestamp=now_ts + 5,
        replay_guard=guard,
    )

    # Replayed event_id fails
    with pytest.raises(InternalAuthError, match="Replayed internal event_id"):
        verify_internal_hmac(
            secret=TEST_SECRET,
            provided_signature=sig,
            timestamp=now_ts,
            event_id="evt-replay-1",
            method="POST",
            canonical_path=path,
            raw_body=body,
            now_timestamp=now_ts + 10,
            replay_guard=guard,
        )


def test_worker_and_backend_hmac_canonicalization_compatibility() -> None:
    """Worker callback headers and GET signing match backend verification."""
    worker_auth = _load_worker_internal_auth()
    now_ts = 1_700_000_000

    # POST callback compatibility
    callback_path = "/api/v1/internal/workers/callback"
    callback_body = '{"job_id":"job-123","status":"completed"}'
    headers = worker_auth.build_worker_callback_headers(
        secret=TEST_SECRET,
        event_id="evt-cross-post-1",
        canonical_path=callback_path,
        raw_body=callback_body,
        timestamp=now_ts,
    )
    verify_internal_hmac(
        secret=TEST_SECRET,
        provided_signature=headers["X-Internal-Signature"],
        timestamp=int(headers["X-Internal-Timestamp"]),
        event_id=headers["X-Internal-Event-Id"],
        method="POST",
        canonical_path=callback_path,
        raw_body=callback_body.encode("utf-8"),
        now_timestamp=now_ts + 5,
    )

    # GET canonical compatibility
    bundle_path = "/api/v1/internal/source-bundles/11111111-2222-3333-4444-555555555555"
    get_sig = worker_auth.sign_internal_request(
        secret=TEST_SECRET,
        timestamp=now_ts,
        event_id="evt-cross-get-1",
        method="GET",
        canonical_path=bundle_path,
        raw_body=b"",
    )
    verify_internal_hmac(
        secret=TEST_SECRET,
        provided_signature=get_sig,
        timestamp=now_ts,
        event_id="evt-cross-get-1",
        method="GET",
        canonical_path=bundle_path,
        raw_body=b"",
        now_timestamp=now_ts + 5,
    )
