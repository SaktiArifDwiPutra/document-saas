"""In-memory per-scope sliding-window rate limiter for authentication endpoints."""

import hashlib
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Mapping
from threading import Lock

from app.core.errors import AppError
from app.core.security import normalize_email

logger = logging.getLogger(__name__)


def account_rate_limit_key(scope: str, email: str) -> str:
    """Build a non-PII SHA-256 account bucket key from a normalized email address."""
    normalized = normalize_email(email)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"{scope}:acct:{digest}"


def extract_rate_limit_scope(key: str) -> str:
    """Derive a logical rate-limit pool scope from a bucket key prefix."""
    parts = key.split(":", 2)
    if len(parts) >= 3 and parts[0] and parts[1]:
        return f"{parts[0]}:{parts[1]}"
    if len(parts) == 2 and parts[0]:
        return parts[0]
    return "default"


def _rate_limited_error() -> AppError:
    return AppError(
        code="RATE_LIMITED",
        message="Terlalu banyak permintaan. Silakan coba lagi beberapa saat.",
        status_code=429,
    )


class SlidingWindowRateLimiter:
    """Per-scope bounded in-memory sliding-window rate limiter for single-instance dev/beta use.

    Each scope maintains an independent bucket pool with a bounded active-key capacity.
    Expired buckets are purged before rejecting unseen keys; active buckets are never evicted.
    When a scope pool is saturated with active keys, new unseen keys fail closed with HTTP 429.
    """

    def __init__(
        self,
        max_keys: int = 10_000,
        scope_max_keys: Mapping[str, int] | None = None,
    ) -> None:
        if max_keys < 1:
            raise ValueError("max_keys must be at least 1")
        normalized_scopes: dict[str, int] = {}
        if scope_max_keys is not None:
            for raw_scope, capacity in scope_max_keys.items():
                cleaned_scope = raw_scope.strip()
                if not cleaned_scope:
                    raise ValueError("scope_max_keys keys must not be empty")
                if capacity < 1:
                    raise ValueError("scope_max_keys capacities must be at least 1")
                normalized_scopes[cleaned_scope] = int(capacity)

        self._max_keys = max_keys
        self._scope_max_keys = normalized_scopes
        self._pools: dict[str, OrderedDict[str, tuple[deque[float], int]]] = {}
        self._saturation_rejections: dict[str, int] = {}
        self._lock = Lock()
        self._checks_since_purge = 0

    @property
    def max_keys(self) -> int:
        """Return default maximum tracked keys per scope."""
        return self._max_keys

    @property
    def scope_max_keys(self) -> dict[str, int]:
        """Return configured per-scope maximum tracked key capacities."""
        return dict(self._scope_max_keys)

    def max_keys_for_scope(self, scope: str) -> int:
        """Return effective maximum tracked key capacity for a given scope."""
        return self._scope_max_keys.get(scope, self._max_keys)

    @property
    def key_count(self) -> int:
        """Return total number of tracked rate-limit keys across all scopes."""
        with self._lock:
            return sum(len(pool) for pool in self._pools.values())

    def scope_key_count(self, scope: str) -> int:
        """Return number of currently tracked rate-limit keys in a specific scope."""
        with self._lock:
            pool = self._pools.get(scope)
            return len(pool) if pool is not None else 0

    def saturation_rejections(self, scope: str | None = None) -> int:
        """Return count of fail-closed saturation rejections globally or for a single scope."""
        with self._lock:
            if scope is None:
                return sum(self._saturation_rejections.values())
            return self._saturation_rejections.get(scope, 0)

    def saturation_metrics(self) -> dict[str, int]:
        """Return snapshot copy of per-scope fail-closed saturation rejection counts."""
        with self._lock:
            return dict(self._saturation_rejections)

    @property
    def _buckets(self) -> dict[str, tuple[deque[float], int]]:
        """Return combined snapshot of active buckets across all scopes."""
        with self._lock:
            combined: dict[str, tuple[deque[float], int]] = {}
            for pool in self._pools.values():
                combined.update(pool)
            return combined

    def _purge_scope_expired_locked(self, scope: str, current: float) -> int:
        pool = self._pools.get(scope)
        if pool is None:
            return 0
        removed = 0
        for bucket_key in list(pool.keys()):
            bucket, window_seconds = pool[bucket_key]
            cutoff = current - window_seconds
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if not bucket:
                del pool[bucket_key]
                removed += 1
        if not pool:
            del self._pools[scope]
        return removed

    def _purge_all_expired_locked(self, current: float) -> int:
        removed = 0
        for scope in list(self._pools.keys()):
            removed += self._purge_scope_expired_locked(scope, current)
        return removed

    def prune_expired(
        self,
        now_monotonic: float | None = None,
        *,
        scope: str | None = None,
    ) -> int:
        """Remove expired timestamps and empty buckets, returning removed bucket count."""
        current = time.monotonic() if now_monotonic is None else now_monotonic
        with self._lock:
            if scope is not None:
                return self._purge_scope_expired_locked(scope.strip(), current)
            return self._purge_all_expired_locked(current)

    def check(
        self,
        key: str,
        *,
        max_requests: int,
        window_seconds: int,
        now_monotonic: float | None = None,
        scope: str | None = None,
    ) -> None:
        """Record an attempt and raise 429 RATE_LIMITED on quota breach or scope saturation."""
        current = time.monotonic() if now_monotonic is None else now_monotonic
        cutoff = current - window_seconds
        resolved_scope = (
            scope.strip() if scope and scope.strip() else extract_rate_limit_scope(key)
        )

        with self._lock:
            self._checks_since_purge += 1
            if self._checks_since_purge >= 128:
                self._purge_all_expired_locked(current)
                self._checks_since_purge = 0

            pool = self._pools.get(resolved_scope)
            if pool is not None and key in pool:
                bucket, _ = pool[key]
                while bucket and bucket[0] <= cutoff:
                    bucket.popleft()
                if not bucket and max_requests <= 0:
                    del pool[key]
                    if not pool:
                        del self._pools[resolved_scope]
                    raise _rate_limited_error()
                pool[key] = (bucket, window_seconds)
                pool.move_to_end(key)
                if len(bucket) >= max_requests:
                    raise _rate_limited_error()
                bucket.append(current)
                return

            if max_requests <= 0:
                raise _rate_limited_error()

            scope_capacity = self._scope_max_keys.get(resolved_scope, self._max_keys)
            pool = self._pools.setdefault(resolved_scope, OrderedDict())
            if len(pool) >= scope_capacity:
                self._purge_scope_expired_locked(resolved_scope, current)
                pool = self._pools.setdefault(resolved_scope, OrderedDict())
                if len(pool) >= scope_capacity:
                    rejection_count = self._saturation_rejections.get(resolved_scope, 0) + 1
                    self._saturation_rejections[resolved_scope] = rejection_count
                    logger.warning(
                        "rate_limit_scope_saturated scope=%s active_keys=%d "
                        "max_keys=%d saturation_rejections=%d",
                        resolved_scope,
                        len(pool),
                        scope_capacity,
                        rejection_count,
                    )
                    raise _rate_limited_error()

            pool[key] = (deque([current]), window_seconds)

    def reset(self) -> None:
        """Clear all recorded rate-limit buckets and saturation metrics."""
        with self._lock:
            self._pools.clear()
            self._saturation_rejections.clear()
            self._checks_since_purge = 0
