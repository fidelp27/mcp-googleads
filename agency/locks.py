"""File-based mutex per Google Ads resource for multi-operator safety.

Two operators calling `update_campaign_budget` on the same campaign at
the same time can silently trample each other's changes — Google Ads
sees the last write. This module provides a portable file lock per
`(customer_id, resource_name)` pair so the second call waits (up to
LOCK_TIMEOUT_SECONDS) instead of racing.

The lock is thin: it's advisory (no OS-level file mode), portable
Windows/Unix via the `filelock` library, and its acquired/released
metadata is returned to callers so `execute_mutation` can include
`lock_wait_ms` in the audit entry.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from .config import DATA_DIR


LOCK_ROOT = DATA_DIR / "locks"
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("LOCK_TIMEOUT_SECONDS", "30"))
_RESOURCE_SANITIZE_RE = re.compile(r"[^a-zA-Z0-9]+")


class ResourceLockedError(RuntimeError):
    """Raised when a resource lock cannot be acquired within the timeout."""

    def __init__(
        self,
        *,
        customer_id: str,
        resource_key: str,
        timeout_seconds: float,
        lock_path: str,
    ) -> None:
        self.customer_id = customer_id
        self.resource_key = resource_key
        self.timeout_seconds = timeout_seconds
        self.lock_path = lock_path
        super().__init__(
            f"RESOURCE_LOCKED customer={customer_id} resource={resource_key} "
            f"timeout={timeout_seconds}s lock_path={lock_path}"
        )


def _lock_path(customer_id: str, resource_key: str) -> Path:
    """Return the .lock filesystem path for a (customer, resource) pair.

    resource_key is any string that identifies the resource being mutated
    (a Google Ads resource name is the natural fit). We hash it so the
    filesystem doesn't have to deal with slashes or long paths.
    """
    hashed = hashlib.sha256(resource_key.encode("utf-8")).hexdigest()[:24]
    safe_prefix = _RESOURCE_SANITIZE_RE.sub("_", resource_key)[:40] or "resource"
    parent = LOCK_ROOT / customer_id
    parent.mkdir(parents=True, exist_ok=True)
    return parent / f"{safe_prefix}.{hashed}.lock"


@contextmanager
def resource_lock(
    customer_id: str,
    resource_key: str,
    *,
    timeout_seconds: float | None = None,
):
    """Acquire an advisory file lock for (customer_id, resource_key).

    Usage:
        with resource_lock(cid, "customers/.../campaigns/123") as info:
            # mutation happens here; info["lock_wait_ms"] tells you how
            # long you waited to acquire it.
            ...

    Raises ResourceLockedError if the lock can't be acquired before the
    timeout expires.
    """
    if not customer_id:
        raise ValueError("customer_id is required for resource_lock")
    if not resource_key:
        raise ValueError("resource_key is required for resource_lock")
    effective_timeout = (
        DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else float(timeout_seconds)
    )
    if effective_timeout < 0:
        raise ValueError("timeout_seconds must be non-negative")
    path = _lock_path(str(customer_id), str(resource_key))
    lock = FileLock(str(path))
    start = time.monotonic()
    try:
        lock.acquire(timeout=effective_timeout)
    except Timeout as exc:
        raise ResourceLockedError(
            customer_id=str(customer_id),
            resource_key=str(resource_key),
            timeout_seconds=effective_timeout,
            lock_path=str(path),
        ) from exc
    waited_ms = int((time.monotonic() - start) * 1000)
    try:
        yield {
            "lock_path": str(path),
            "lock_wait_ms": waited_ms,
            "acquired_at_monotonic": time.monotonic(),
        }
    finally:
        lock.release()


def lock_paths_for(customer_id: str) -> list[Path]:
    """Return every lock file currently persisted for a customer (helper for cleanup)."""
    directory = LOCK_ROOT / customer_id
    if not directory.exists():
        return []
    return sorted(directory.glob("*.lock"))
