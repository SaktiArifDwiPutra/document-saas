"""Tests for Python Worker internal callback HMAC signing."""

import hashlib
import hmac

import pytest

from worker_app.core.internal_auth import (
    WorkerAuthError,
    build_worker_callback_headers,
    sign_internal_request,
)

TEST_SECRET = "worker-test-hmac-secret-at-least-32-chars-long"


def test_worker_callback_headers_use_canonical_format() -> None:
    """Worker callback signature matches canonical format."""
    ts = 1_700_000_123
    event_id = "worker-evt-001"
    path = "/api/v1/internal/workers/callback"
    body = '{"job_id":"job-1","status":"completed"}'

    headers = build_worker_callback_headers(
        secret=TEST_SECRET,
        event_id=event_id,
        canonical_path=path,
        raw_body=body,
        timestamp=ts,
    )

    expected_message = f"{ts}.{event_id}.POST.{path}.{body}".encode()
    expected_sig = hmac.new(
        TEST_SECRET.encode("utf-8"), expected_message, hashlib.sha256
    ).hexdigest()

    assert headers["X-Internal-Timestamp"] == str(ts)
    assert headers["X-Internal-Event-Id"] == event_id
    assert headers["X-Internal-Signature"] == expected_sig


def test_worker_signing_rejects_invalid_inputs() -> None:
    """Signer rejects empty secret, empty event_id, or non-rooted path."""
    with pytest.raises(WorkerAuthError):
        sign_internal_request(
            secret="",
            timestamp=1_700_000_000,
            event_id="evt-1",
            method="POST",
            canonical_path="/api/v1/internal/workers/callback",
            raw_body=b"{}",
        )

    with pytest.raises(WorkerAuthError):
        sign_internal_request(
            secret=TEST_SECRET,
            timestamp=1_700_000_000,
            event_id=" ",
            method="POST",
            canonical_path="/api/v1/internal/workers/callback",
            raw_body=b"{}",
        )

    with pytest.raises(WorkerAuthError):
        sign_internal_request(
            secret=TEST_SECRET,
            timestamp=1_700_000_000,
            event_id="evt-1",
            method="POST",
            canonical_path="api/v1/internal/workers/callback",
            raw_body=b"{}",
        )
