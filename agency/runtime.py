"""Runtime context, storage abstraction, and dynamic multi-MCC routing.

By default (open-source desktop/self-hosted mode), `RuntimeContext` reads from
environment variables and local files under `DATA_DIR` / `CONFIG_DIR`.
Future multi-tenant / hosted deployments can bind a request-scoped
`RuntimeContext` via `use_runtime_context(...)` backed by Postgres/KMS without
changing any MCP tool implementation.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Iterator, Protocol

from .config import DATA_DIR, client_metadata, normalize_customer_id


class StorageBackend(Protocol):
    """Minimal storage contract for audit/approval/state persistence."""

    def append_jsonl(self, collection: str, entry: dict[str, Any]) -> None: ...

    def read_jsonl(self, collection: str) -> list[dict[str, Any]]: ...


class LocalFileStorageBackend:
    """Default local JSONL storage backend rooted at `data_dir`."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir
        self._lock = RLock()

    @property
    def data_dir(self) -> Path:
        return self._data_dir if self._data_dir is not None else DATA_DIR

    def _path(self, collection: str) -> Path:
        safe = "".join(ch for ch in collection if ch.isalnum() or ch in {"-", "_"})
        if not safe:
            raise ValueError("Invalid storage collection name")
        return self.data_dir / f"{safe}.jsonl"

    def append_jsonl(self, collection: str, entry: dict[str, Any]) -> None:
        with self._lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            target = self._path(collection)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    def read_jsonl(self, collection: str) -> list[dict[str, Any]]:
        target = self._path(collection)
        if not target.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self._lock:
            for line in target.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows


@dataclass
class RuntimeContext:
    """Per-session or per-request runtime configuration for tool execution."""

    tenant_id: str = "local"
    operator_name: str | None = None
    default_login_customer_id: str | None = None
    login_customer_map: dict[str, str] = field(default_factory=dict)
    storage: StorageBackend = field(default_factory=LocalFileStorageBackend)


_runtime_context: ContextVar[RuntimeContext | None] = ContextVar(
    "google_ads_runtime_context", default=None
)
_session_login_map_lock = RLock()
_session_login_map: dict[str, str] = {}


def get_runtime_context() -> RuntimeContext:
    ctx = _runtime_context.get()
    if ctx is not None:
        return ctx
    return RuntimeContext(
        tenant_id="local",
        operator_name=os.getenv("OPERATOR_NAME"),
        default_login_customer_id=os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "") or None,
    )


@contextmanager
def use_runtime_context(ctx: RuntimeContext) -> Iterator[RuntimeContext]:
    token = _runtime_context.set(ctx)
    try:
        yield ctx
    finally:
        _runtime_context.reset(token)


def parse_login_customer_map(raw: str) -> dict[str, str]:
    """Parse `GOOGLE_ADS_LOGIN_CUSTOMER_MAP` (`cid:mcc,cid2:`) into normalized entries.

    An empty manager value (`cid2:`) explicitly marks a direct account that must
    NOT inherit the global `GOOGLE_ADS_LOGIN_CUSTOMER_ID`.
    """
    mapping: dict[str, str] = {}
    for entry in (raw or "").split(","):
        item = entry.strip()
        if not item or ":" not in item:
            continue
        cid_raw, manager_raw = item.split(":", 1)
        cid = normalize_customer_id(cid_raw)
        manager = normalize_customer_id(manager_raw) if manager_raw.strip() else ""
        mapping[cid] = manager
    return mapping


def register_login_customer(customer_id: str, manager_id: str | None) -> None:
    """Remember the manager (or direct='' access) for a customer in the active session.

    Accounts explicitly pinned in `GOOGLE_ADS_LOGIN_CUSTOMER_MAP` (for example by a
    validated managed/remote profile) are never overwritten by dynamic discovery.
    """
    cid = normalize_customer_id(customer_id)
    mgr = normalize_customer_id(manager_id) if manager_id else ""
    ctx = _runtime_context.get()
    if ctx is not None:
        ctx.login_customer_map[cid] = mgr
        return
    env_map = parse_login_customer_map(os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_MAP", ""))
    if cid in env_map:
        return
    with _session_login_map_lock:
        _session_login_map[cid] = mgr


def clear_session_login_map() -> None:
    with _session_login_map_lock:
        _session_login_map.clear()


def resolve_login_customer_id(
    customer_id: str,
    *,
    explicit_login_customer_id: str | None = None,
) -> str | None:
    """Resolve the effective `login-customer-id` header for a target account.

    Precedence:
    1. Explicit per-call override (`explicit_login_customer_id`).
    2. Request-scoped `RuntimeContext.login_customer_map` (supports direct='' override).
    3. `GOOGLE_ADS_LOGIN_CUSTOMER_MAP` env var (`cid:mcc,direct_cid:`) — pinned profile/host routes.
    4. Session-discovered map (`register_login_customer`, populated e.g. by MCC discovery).
    5. `config/clients.json` per-client `manager_id` / `login_customer_id` ("" = direct).
    6. Fallback to `RuntimeContext.default_login_customer_id` / `GOOGLE_ADS_LOGIN_CUSTOMER_ID`
       (omitted when querying the manager account itself).
    """
    cid = normalize_customer_id(customer_id)
    if explicit_login_customer_id is not None:
        clean = explicit_login_customer_id.strip()
        return normalize_customer_id(clean) if clean else None

    ctx = _runtime_context.get()
    if ctx is not None and cid in ctx.login_customer_map:
        mapped = ctx.login_customer_map[cid]
        return mapped or None

    env_map = parse_login_customer_map(os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_MAP", ""))
    if cid in env_map:
        return env_map[cid] or None

    with _session_login_map_lock:
        if cid in _session_login_map:
            mapped = _session_login_map[cid]
            return mapped or None

    meta = client_metadata(cid)
    if "manager_id" in meta or "login_customer_id" in meta:
        raw_mgr = meta.get("manager_id")
        if raw_mgr is None:
            raw_mgr = meta.get("login_customer_id")
        clean_mgr = str(raw_mgr or "").strip()
        return normalize_customer_id(clean_mgr) if clean_mgr else None

    fallback_raw = (
        ctx.default_login_customer_id
        if ctx is not None
        else os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")
    )
    if fallback_raw and str(fallback_raw).strip():
        fallback = normalize_customer_id(str(fallback_raw))
        return fallback
    return None
