"""Encrypted tenant profile store and on-demand loopback worker pool for self-serve remote MCP.

Security & concurrency invariants:
- Google OAuth credentials (refresh tokens) are ALWAYS encrypted with Fernet
  (`MCP_REMOTE_MASTER_KEY`) both inside SQLite (`tenants.db`) and when
  materialized into a worker's ephemeral `profile.json`.
- Each customer ID stores its validated access route (`""` for direct access or
  a 10-digit MCC manager ID) in `customer_routes`, preventing mixed direct/MCC
  header leaks and unauthorized manager edits.
- Worker capacity (`max_active_workers`) is reserved atomically under
  `_pool_lock` BEFORE spawning a process, so concurrent sign-ups can never
  overshoot the limit.
- In-flight MCP requests (`in_flight > 0`) are never interrupted by idle or LRU
  eviction; reconfigure/revoke marks workers as `draining` and waits briefly for
  active requests to complete before terminating.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
import hashlib
import json
import os
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any, AsyncIterator, Callable, Iterator

from cryptography.fernet import Fernet, InvalidToken


class PoolCapacityError(RuntimeError):
    """Raised when `max_active_workers` is reached and every active worker has in-flight requests."""


class DisconnectEpochMismatchError(RuntimeError):
    """Raised when an OAuth grant or tenant write was superseded by a newer disconnection."""


def require_master_key(raw_key: str | None = None) -> bytes:
    """Validate and return a Fernet key from `MCP_REMOTE_MASTER_KEY`."""
    value = (raw_key if raw_key is not None else os.environ.get("MCP_REMOTE_MASTER_KEY", "")).strip()
    if not value:
        raise ValueError(
            "MCP_REMOTE_MASTER_KEY is required for self-serve remote mode. "
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    key_bytes = value.encode("ascii")
    try:
        Fernet(key_bytes)
    except Exception as exc:
        raise ValueError(
            "MCP_REMOTE_MASTER_KEY must be a valid 32-byte URL-safe base64 Fernet key."
        ) from exc
    return key_bytes


def encrypt_profile_payload(payload: dict[str, Any], master_key: bytes) -> dict[str, Any]:
    """Wrap a profile dict in a Fernet-encrypted envelope safe for disk storage."""
    token = Fernet(master_key).encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return {"format": "fernet-v1", "encrypted": token.decode("ascii")}


def decrypt_profile_payload(raw_data: dict[str, Any], master_key: bytes | None = None) -> dict[str, Any]:
    """Return plaintext profile dict, decrypting Fernet envelopes in memory when present."""
    if not isinstance(raw_data, dict) or "encrypted" not in raw_data:
        return raw_data
    key = master_key if master_key is not None else require_master_key()
    try:
        decrypted = Fernet(key).decrypt(str(raw_data["encrypted"]).encode("ascii"))
    except (InvalidToken, ValueError, TypeError) as exc:
        raise ValueError("Unable to decrypt managed profile with MCP_REMOTE_MASTER_KEY.") from exc
    parsed = json.loads(decrypted.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Decrypted profile payload is invalid.")
    return parsed


def subject_slug(subject: str) -> str:
    """Stable filesystem-safe identifier derived from an OAuth subject."""
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:24]


def _normalize_routes(
    customers: list[str],
    manager: str = "",
    customer_routes: dict[str, str] | None = None,
) -> tuple[tuple[str, ...], dict[str, str], str]:
    """Normalize customer IDs and per-account access routes (`cid -> '' | mcc_id`)."""
    routes: dict[str, str] = {}
    if customer_routes:
        for cid, mgr in customer_routes.items():
            routes[str(cid)] = str(mgr or "")
        ordered_customers = tuple(dict.fromkeys(list(customers) or list(routes.keys())))
        for cid in ordered_customers:
            routes.setdefault(cid, str(manager or ""))
        routes = {cid: routes[cid] for cid in ordered_customers}
    else:
        ordered_customers = tuple(dict.fromkeys(str(c) for c in customers))
        routes = {cid: str(manager or "") for cid in ordered_customers}

    unique_mgrs = {mgr for mgr in routes.values() if mgr}
    # Only populate legacy single `manager` when ALL accounts share that same MCC (no direct accounts mixed in)
    single_manager = (
        next(iter(unique_mgrs))
        if len(unique_mgrs) == 1 and all(bool(m) for m in routes.values())
        else ""
    )
    return ordered_customers, routes, single_manager


@dataclass(frozen=True)
class TenantProfile:
    subject: str
    credentials: dict[str, Any] = field(repr=False)
    customers: tuple[str, ...]
    manager: str = ""
    customer_routes: dict[str, str] = field(default_factory=dict)
    preset: str = "core"
    modules: str | None = None
    allow_writes: bool = False
    email: str | None = None
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""

    def effective_routes(self) -> dict[str, str]:
        if self.customer_routes:
            return dict(self.customer_routes)
        return {cid: self.manager for cid in self.customers}

    def to_worker_payload(self) -> dict[str, Any]:
        routes = self.effective_routes()
        payload: dict[str, Any] = {
            "credentials": self.credentials,
            "customer": self.customers[0],
            "customers": list(self.customers),
            "manager": self.manager,
            "customer_routes": routes,
            "preset": self.preset,
        }
        if self.modules:
            payload["modules"] = self.modules
        return payload

    def public_summary(self) -> dict[str, Any]:
        routes = self.effective_routes()
        return {
            "connected": self.status == "active" and bool(self.customers),
            "subject": self.subject,
            "email": self.email,
            "customers": list(self.customers),
            "manager": self.manager or None,
            "customer_routes": routes,
            "preset": self.preset,
            "modules": self.modules,
            "allow_writes": self.allow_writes,
            "read_only_mode": not self.allow_writes,
            "updated_at": self.updated_at,
        }


class EncryptedProfileStore:
    """SQLite-backed encrypted tenant store for self-serve remote users."""

    def __init__(self, state_dir: Path, master_key: bytes) -> None:
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._fernet = Fernet(master_key)
        self._master_key = master_key
        self._db_path = self.state_dir / "tenants.db"
        self._lock = RLock()
        self._init_db()

    def _connect(self, *, isolation_level: str | None = "") -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=30.0,
            isolation_level=isolation_level,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._connect()
            try:
                yield conn
            finally:
                conn.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        """Execute a multi-process-safe SQLite write transaction (`BEGIN IMMEDIATE`).

        Acquiring SQLite's RESERVED write lock BEFORE any `SELECT` ensures that
        `disconnect_epoch` checks and subsequent mutations are atomic even across
        multiple `EncryptedProfileStore` instances or processes sharing `tenants.db`.
        """
        with self._lock:
            conn = self._connect(isolation_level=None)
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                else:
                    if conn.in_transaction:
                        conn.execute("COMMIT")
            finally:
                conn.close()

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect(isolation_level=None)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()
        with self._write_transaction() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    subject TEXT PRIMARY KEY,
                    email TEXT,
                    encrypted_credentials BLOB NOT NULL,
                    customers_json TEXT NOT NULL,
                    manager TEXT NOT NULL DEFAULT '',
                    customer_routes_json TEXT NOT NULL DEFAULT '{}',
                    preset TEXT NOT NULL DEFAULT 'core',
                    modules TEXT,
                    allow_writes INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(tenants)").fetchall()}
            if "customer_routes_json" not in cols:
                conn.execute(
                    "ALTER TABLE tenants ADD COLUMN customer_routes_json TEXT NOT NULL DEFAULT '{}'"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_pending (
                    state TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    encrypted_payload BLOB NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subject_disconnect_epochs (
                    subject TEXT PRIMARY KEY,
                    epoch INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def _get_disconnect_epoch_unlocked(self, conn: sqlite3.Connection, subject: str) -> int:
        row = conn.execute(
            "SELECT epoch FROM subject_disconnect_epochs WHERE subject = ?",
            (subject,),
        ).fetchone()
        return int(row["epoch"]) if row is not None else 0

    def get_disconnect_epoch(self, subject: str) -> int:
        """Return monotonic disconnection counter for `subject` (incremented on every revoke)."""
        with self._read_connection() as conn:
            return self._get_disconnect_epoch_unlocked(conn, subject)

    def _row_to_tenant(self, row: sqlite3.Row | None, subject: str) -> TenantProfile | None:
        if row is None or not row["encrypted_credentials"]:
            return None
        try:
            creds = json.loads(self._fernet.decrypt(row["encrypted_credentials"]).decode("utf-8"))
        except InvalidToken as exc:
            raise ValueError(f"Failed to decrypt credentials for subject '{subject}'.") from exc
        customers = tuple(json.loads(row["customers_json"]))
        if not customers:
            return None
        raw_routes = json.loads(row["customer_routes_json"] or "{}")
        if not isinstance(raw_routes, dict) or not raw_routes:
            raw_routes = {cid: row["manager"] or "" for cid in customers}
        return TenantProfile(
            subject=row["subject"],
            credentials=creds,
            customers=customers,
            manager=row["manager"] or "",
            customer_routes={str(k): str(v or "") for k, v in raw_routes.items()},
            preset=row["preset"] or "core",
            modules=row["modules"],
            allow_writes=bool(row["allow_writes"]),
            email=row["email"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def upsert_tenant(
        self,
        *,
        subject: str,
        credentials: dict[str, Any],
        customers: list[str],
        manager: str = "",
        customer_routes: dict[str, str] | None = None,
        preset: str = "core",
        modules: str | None = None,
        allow_writes: bool = False,
        email: str | None = None,
        expected_disconnect_epoch: int | None = None,
    ) -> TenantProfile:
        if not subject or not subject.strip():
            raise ValueError("OAuth subject is required.")
        if not customers and not customer_routes:
            raise ValueError("At least one Google Ads customer ID must be selected.")
        if not credentials or not credentials.get("refresh_token"):
            raise ValueError("Google OAuth credentials with refresh_token are required.")

        ordered_customers, routes, single_manager = _normalize_routes(
            customers, manager=manager, customer_routes=customer_routes
        )
        now = datetime.now(timezone.utc).isoformat()
        encrypted_creds = self._fernet.encrypt(
            json.dumps(credentials, separators=(",", ":")).encode("utf-8")
        )
        with self._write_transaction() as conn:
            if expected_disconnect_epoch is not None:
                current_epoch = self._get_disconnect_epoch_unlocked(conn, subject)
                if current_epoch != expected_disconnect_epoch:
                    raise DisconnectEpochMismatchError(
                        f"OAuth flow for subject '{subject}' was superseded by a newer disconnection."
                    )
            existing = conn.execute(
                "SELECT created_at FROM tenants WHERE subject = ?", (subject,)
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO tenants (
                    subject, email, encrypted_credentials, customers_json,
                    manager, customer_routes_json, preset, modules, allow_writes,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(subject) DO UPDATE SET
                    email = excluded.email,
                    encrypted_credentials = excluded.encrypted_credentials,
                    customers_json = excluded.customers_json,
                    manager = excluded.manager,
                    customer_routes_json = excluded.customer_routes_json,
                    preset = excluded.preset,
                    modules = excluded.modules,
                    allow_writes = excluded.allow_writes,
                    status = 'active',
                    updated_at = excluded.updated_at
                """,
                (
                    subject,
                    email,
                    encrypted_creds,
                    json.dumps(list(ordered_customers)),
                    single_manager,
                    json.dumps(routes, sort_keys=True),
                    preset or "core",
                    modules,
                    1 if allow_writes else 0,
                    created_at,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM tenants WHERE subject = ? AND status = 'active'",
                (subject,),
            ).fetchone()
        tenant = self._row_to_tenant(row, subject)
        assert tenant is not None
        return tenant

    def get_tenant(self, subject: str) -> TenantProfile | None:
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM tenants WHERE subject = ? AND status = 'active'",
                (subject,),
            ).fetchone()
        return self._row_to_tenant(row, subject)

    def update_preferences(
        self,
        subject: str,
        *,
        customers: list[str] | None = None,
        customer_routes: dict[str, str] | None = None,
        preset: str | None = None,
        modules: str | None = None,
        allow_writes: bool | None = None,
    ) -> TenantProfile:
        now = datetime.now(timezone.utc).isoformat()
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tenants WHERE subject = ? AND status = 'active'",
                (subject,),
            ).fetchone()
            current = self._row_to_tenant(row, subject)
            if current is None:
                raise ValueError("No active Google Ads connection found for this user.")
            routes = customer_routes if customer_routes is not None else current.effective_routes()
            selected = customers if customers is not None else list(routes.keys())
            routes = {cid: routes[cid] for cid in selected if cid in routes}
            ordered_customers, norm_routes, single_manager = _normalize_routes(
                selected, customer_routes=routes
            )
            next_preset = preset if preset is not None else current.preset
            next_modules = modules if modules is not None else current.modules
            next_writes = allow_writes if allow_writes is not None else current.allow_writes
            conn.execute(
                """
                UPDATE tenants
                SET customers_json = ?, manager = ?, customer_routes_json = ?,
                    preset = ?, modules = ?, allow_writes = ?, updated_at = ?
                WHERE subject = ? AND status = 'active'
                """,
                (
                    json.dumps(list(ordered_customers)),
                    single_manager,
                    json.dumps(norm_routes, sort_keys=True),
                    next_preset or "core",
                    next_modules,
                    1 if next_writes else 0,
                    now,
                    subject,
                ),
            )
            updated_row = conn.execute(
                "SELECT * FROM tenants WHERE subject = ? AND status = 'active'",
                (subject,),
            ).fetchone()
        tenant = self._row_to_tenant(updated_row, subject)
        assert tenant is not None
        return tenant

    def revoke_tenant(self, subject: str) -> dict[str, Any] | None:
        """Mark tenant revoked, wipe encrypted credentials, and return prior creds for Google revocation."""
        now = datetime.now(timezone.utc).isoformat()
        prior_creds: dict[str, Any] | None = None
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT * FROM tenants WHERE subject = ? AND status = 'active'",
                (subject,),
            ).fetchone()
            prior = self._row_to_tenant(row, subject)
            if prior is not None:
                prior_creds = prior.credentials
            conn.execute(
                """
                UPDATE tenants
                SET status = 'revoked', encrypted_credentials = X'', customers_json = '[]',
                    manager = '', customer_routes_json = '{}', updated_at = ?
                WHERE subject = ?
                """,
                (now, subject),
            )
            conn.execute(
                """
                INSERT INTO subject_disconnect_epochs (subject, epoch)
                VALUES (?, 1)
                ON CONFLICT(subject) DO UPDATE SET epoch = epoch + 1
                """,
                (subject,),
            )
            conn.execute("DELETE FROM oauth_pending WHERE subject = ?", (subject,))
        return prior_creds

    def list_active_subjects(self) -> list[str]:
        with self._read_connection() as conn:
            rows = conn.execute(
                "SELECT subject FROM tenants WHERE status = 'active' ORDER BY updated_at DESC"
            ).fetchall()
        return [str(r["subject"]) for r in rows]

    # ---------- Short-lived encrypted OAuth flow state (tickets, CSRF + PKCE, grants) ----------

    def save_oauth_pending(
        self,
        state: str,
        subject: str,
        payload: dict[str, Any],
        *,
        ttl_seconds: int = 600,
        expected_disconnect_epoch: int | None = None,
    ) -> None:
        expires_at = time.time() + max(60, ttl_seconds)
        blob = self._fernet.encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        with self._write_transaction() as conn:
            if expected_disconnect_epoch is not None:
                current_epoch = self._get_disconnect_epoch_unlocked(conn, subject)
                if current_epoch != expected_disconnect_epoch:
                    raise DisconnectEpochMismatchError(
                        f"OAuth flow for subject '{subject}' was superseded by a newer disconnection."
                    )
            conn.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (time.time(),))
            conn.execute(
                """
                INSERT OR REPLACE INTO oauth_pending (state, subject, encrypted_payload, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (state, subject, blob, expires_at),
            )

    def pop_oauth_pending(self, state: str, *, expected_subject: str | None = None) -> dict[str, Any] | None:
        now = time.time()
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT subject, encrypted_payload, expires_at FROM oauth_pending WHERE state = ?",
                (state,),
            ).fetchone()
            conn.execute("DELETE FROM oauth_pending WHERE state = ? OR expires_at < ?", (state, now))
        if row is None or float(row["expires_at"]) < now:
            return None
        if expected_subject is not None and row["subject"] != expected_subject:
            return None
        try:
            data = json.loads(self._fernet.decrypt(row["encrypted_payload"]).decode("utf-8"))
        except InvalidToken:
            return None
        data["_subject"] = row["subject"]
        return data

    def materialize_encrypted_profile(
        self,
        tenant: TenantProfile,
        *,
        instance_id: str | None = None,
    ) -> Path:
        """Write an encrypted worker profile into the tenant's private runtime directory.

        When `instance_id` is provided, writes a per-instance file (`profile-<id>.json`)
        so a reconnecting worker never collides with a previous worker that is still draining.
        """
        runtime_dir = self.state_dir / "workers" / subject_slug(tenant.subject)
        runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", instance_id or "")
        filename = f"profile-{safe_id}.json" if safe_id else "profile.json"
        profile_path = runtime_dir / filename
        envelope = encrypt_profile_payload(tenant.to_worker_payload(), self._master_key)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=runtime_dir, delete=False
        ) as handle:
            tmp = Path(handle.name)
            try:
                handle.write(json.dumps(envelope, indent=2))
                handle.close()
                try:
                    os.chmod(tmp, 0o600)
                except OSError:
                    pass
                tmp.replace(profile_path)
            finally:
                tmp.unlink(missing_ok=True)
        return profile_path

    def wipe_runtime_profile(
        self,
        subject: str,
        *,
        exclude_paths: set[Path] | None = None,
    ) -> None:
        runtime_dir = self.state_dir / "workers" / subject_slug(subject)
        if not runtime_dir.exists():
            return
        excluded = {p.resolve() for p in (exclude_paths or set())}
        legacy_path = runtime_dir / "profile.json"
        if legacy_path.exists() and legacy_path.resolve() not in excluded:
            legacy_path.unlink(missing_ok=True)
        for candidate in runtime_dir.glob("profile-*.json"):
            if candidate.resolve() not in excluded:
                candidate.unlink(missing_ok=True)


def _find_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class _ManagedWorker:
    subject: str
    port: int
    url: str
    process: Any
    last_used: float
    profile_fingerprint: str
    profile_path: Path | None = None
    in_flight: int = 0
    draining: bool = False
    disconnecting: bool = False
    on_drained: list[Callable[[], Any]] = field(default_factory=list)
    drained_event: asyncio.Event = field(default_factory=asyncio.Event)


class DynamicWorkerPool:
    """Lazy-spawning, capacity-bounded, in-flight-safe loopback worker pool."""

    def __init__(
        self,
        *,
        store: EncryptedProfileStore,
        worker_key: str,
        master_key: bytes,
        allow_user_writes: bool = False,
        idle_timeout_seconds: float = 900.0,
        max_active_workers: int = 50,
        startup_timeout_seconds: float = 15.0,
        drain_timeout_seconds: float = 5.0,
        spawner: Callable[[TenantProfile, Path, int, str, bytes], Any] | None = None,
        readiness_probe: Callable[[int, float], Any] | None = None,
    ) -> None:
        self.store = store
        self.worker_key = worker_key
        self.master_key = master_key
        self.allow_user_writes = bool(allow_user_writes)
        self.idle_timeout_seconds = max(5.0, float(idle_timeout_seconds))
        self.max_active_workers = max(1, int(max_active_workers))
        self.startup_timeout_seconds = max(1.0, float(startup_timeout_seconds))
        self.drain_timeout_seconds = max(0.2, float(drain_timeout_seconds))
        self._spawner = spawner or self._default_spawner
        self._readiness_probe = readiness_probe or self._default_readiness_probe
        self._workers: dict[str, _ManagedWorker] = {}
        self._draining_workers: list[_ManagedWorker] = []
        self._finalizing_workers: list[_ManagedWorker] = []
        self._pending_revocations: dict[str, list[asyncio.Event]] = {}
        self._reserved_subjects: set[str] = set()
        self._subject_locks: dict[str, asyncio.Lock] = {}
        self._pool_lock = asyncio.Lock()

    def _effective_tenant(self, tenant: TenantProfile) -> TenantProfile:
        """Apply current host write policy (`allow_user_writes`) at worker start time."""
        effective_writes = bool(tenant.allow_writes and self.allow_user_writes)
        if effective_writes == tenant.allow_writes:
            return tenant
        return TenantProfile(
            subject=tenant.subject,
            credentials=tenant.credentials,
            customers=tenant.customers,
            manager=tenant.manager,
            customer_routes=dict(tenant.customer_routes),
            preset=tenant.preset,
            modules=tenant.modules,
            allow_writes=effective_writes,
            email=tenant.email,
            status=tenant.status,
            created_at=tenant.created_at,
            updated_at=tenant.updated_at,
        )

    def _lock_for(self, subject: str) -> asyncio.Lock:
        lock = self._subject_locks.get(subject)
        if lock is None:
            lock = asyncio.Lock()
            self._subject_locks[subject] = lock
        return lock

    @staticmethod
    def _fingerprint(tenant: TenantProfile) -> str:
        raw = json.dumps(
            {
                "customers": tenant.customers,
                "manager": tenant.manager,
                "customer_routes": tenant.effective_routes(),
                "preset": tenant.preset,
                "modules": tenant.modules,
                "allow_writes": tenant.allow_writes,
                "updated_at": tenant.updated_at,
            },
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_alive(process: Any) -> bool:
        if process is None:
            return False
        poll = getattr(process, "poll", None)
        if callable(poll):
            return poll() is None
        return bool(getattr(process, "alive", True))

    @staticmethod
    def _terminate(process: Any) -> None:
        if process is None:
            return
        terminate = getattr(process, "terminate", None)
        wait = getattr(process, "wait", None)
        kill = getattr(process, "kill", None)
        if callable(terminate):
            try:
                terminate()
                if callable(wait):
                    wait(timeout=3)
            except Exception:
                if callable(kill):
                    try:
                        kill()
                    except Exception:
                        pass

    @staticmethod
    def _default_spawner(
        tenant: TenantProfile,
        profile_path: Path,
        port: int,
        worker_key: str,
        master_key: bytes,
    ) -> subprocess.Popen[bytes]:
        connect_script = Path(__file__).resolve().parents[1] / "google_ads_connect.py"
        cmd = [
            sys.executable,
            "-E",
            "-s",
            str(connect_script),
            "--profile",
            str(profile_path),
            "serve",
            "--worker-port",
            str(port),
            "--preset",
            tenant.preset,
        ]
        if tenant.modules:
            cmd.extend(["--modules", tenant.modules])
        if tenant.allow_writes:
            cmd.append("--allow-writes")

        env = {
            **os.environ,
            "MCP_REMOTE_WORKER_KEY": worker_key,
            "MCP_REMOTE_MASTER_KEY": master_key.decode("ascii"),
            "OPERATOR_NAME": f"remote:{tenant.subject}",
        }
        return subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    @staticmethod
    async def _default_readiness_probe(port: int, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port),
                    timeout=0.5,
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                return True
            except OSError:
                await asyncio.sleep(0.1)
        return False

    @staticmethod
    def _unlink_profile(profile_path: Path | None) -> None:
        if profile_path is not None:
            try:
                profile_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _live_profile_paths_unlocked(self, subject: str) -> set[Path]:
        paths: set[Path] = set()
        active = self._workers.get(subject)
        if active is not None and active.profile_path is not None:
            paths.add(active.profile_path)
        for w in (*self._draining_workers, *self._finalizing_workers):
            if w.subject == subject and w.profile_path is not None:
                paths.add(w.profile_path)
        return paths

    def _release_pending_revocation_unlocked(self, subject: str, ev: asyncio.Event) -> None:
        events = self._pending_revocations.get(subject)
        if events and ev in events:
            events.remove(ev)
            if not events:
                self._pending_revocations.pop(subject, None)
        ev.set()

    @staticmethod
    async def _wait_event(ev: asyncio.Event, timeout: float) -> bool:
        if ev.is_set():
            return True
        try:
            await asyncio.wait_for(ev.wait(), timeout=max(0.01, timeout))
            return True
        except asyncio.TimeoutError:
            return False

    def is_draining(self, subject: str) -> bool:
        """Return True if `subject` currently has a live worker still draining in-flight requests."""
        return any(
            w.subject == subject and self._is_alive(w.process)
            for w in self._draining_workers
        )

    def is_finalizing(self, subject: str) -> bool:
        """Return True if `subject` has a worker finalizing or a Google revocation currently in flight."""
        return (
            any(w.subject == subject for w in self._finalizing_workers)
            or bool(self._pending_revocations.get(subject))
        )

    def is_disconnecting(self, subject: str) -> bool:
        """Return True while a disconnect for `subject` is still draining or revoking on Google."""
        return (
            any(
                w.subject == subject and w.disconnecting and self._is_alive(w.process)
                for w in self._draining_workers
            )
            or any(
                w.subject == subject and w.disconnecting
                for w in self._finalizing_workers
            )
            or bool(self._pending_revocations.get(subject))
        )

    async def _finalize_worker(
        self,
        worker: _ManagedWorker,
        callbacks: list[Callable[[], Any]],
    ) -> None:
        """Terminate a worker, unlink ONLY its own per-instance profile, and run deferred callbacks."""
        try:
            self._terminate(worker.process)
            self._unlink_profile(worker.profile_path)
            if callbacks:
                await self._run_on_drained(callbacks)
        finally:
            async with self._pool_lock:
                if worker in self._finalizing_workers:
                    self._finalizing_workers.remove(worker)
                worker.drained_event.set()

    async def _wait_for_drain(self, worker: _ManagedWorker) -> None:
        deadline = time.monotonic() + self.drain_timeout_seconds
        while worker.in_flight > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    async def _drain_and_maybe_terminate(
        self,
        worker: _ManagedWorker,
        *,
        force_on_timeout: bool = False,
    ) -> bool:
        """Wait for in-flight requests to finish; never kill an in-flight worker unless `force_on_timeout=True`."""
        await self._wait_for_drain(worker)
        should_finalize = False
        already_removed = False
        callbacks: list[Callable[[], Any]] = []
        async with self._pool_lock:
            if worker not in self._draining_workers:
                already_removed = True
            elif worker.in_flight == 0 or force_on_timeout:
                self._draining_workers.remove(worker)
                self._finalizing_workers.append(worker)
                callbacks = list(worker.on_drained)
                worker.on_drained.clear()
                should_finalize = True
        if should_finalize:
            await self._finalize_worker(worker, callbacks)
            return True
        if already_removed:
            if force_on_timeout:
                await worker.drained_event.wait()
                return True
            return await self._wait_event(worker.drained_event, self.drain_timeout_seconds)
        # Still in-flight: stays counted in `_draining_workers` and will finalize immediately
        # when the last in-flight request calls `release_worker_handle(worker)`.
        return False

    async def _ensure_worker_and_acquire(self, subject: str, *, hold_in_flight: bool) -> _ManagedWorker | None:
        raw_tenant = self.store.get_tenant(subject)
        if raw_tenant is None:
            await self.stop_worker(subject)
            return None

        tenant = self._effective_tenant(raw_tenant)
        fp = self._fingerprint(tenant)
        async with self._lock_for(subject):
            stale_worker: _ManagedWorker | None = None
            async with self._pool_lock:
                existing = self._workers.get(subject)
                if existing is not None:
                    if (
                        not existing.draining
                        and existing.profile_fingerprint == fp
                        and self._is_alive(existing.process)
                    ):
                        existing.last_used = time.monotonic()
                        if hold_in_flight:
                            existing.in_flight += 1
                        return existing
                    existing.draining = True
                    stale_worker = self._workers.pop(subject, None)
                    if stale_worker is not None:
                        self._draining_workers.append(stale_worker)

            if stale_worker is not None:
                await self._drain_and_maybe_terminate(stale_worker, force_on_timeout=False)

            # Atomically reserve a capacity slot under `_pool_lock` BEFORE spawning!
            evicted_workers: list[tuple[_ManagedWorker, list[Callable[[], Any]]]] = []
            async with self._pool_lock:
                now = time.monotonic()
                # Clean up any already-dead draining workers first
                alive_draining: list[_ManagedWorker] = []
                for w in self._draining_workers:
                    if self._is_alive(w.process):
                        alive_draining.append(w)
                    else:
                        cbs = list(w.on_drained)
                        w.on_drained.clear()
                        self._finalizing_workers.append(w)
                        evicted_workers.append((w, cbs))
                self._draining_workers[:] = alive_draining

                # Reap dead or idle workers with ZERO in-flight requests first
                for subj in list(self._workers):
                    w = self._workers[subj]
                    if not self._is_alive(w.process):
                        popped = self._workers.pop(subj)
                        cbs = list(popped.on_drained)
                        popped.on_drained.clear()
                        self._finalizing_workers.append(popped)
                        evicted_workers.append((popped, cbs))
                    elif w.in_flight == 0 and (now - w.last_used) >= self.idle_timeout_seconds:
                        w.draining = True
                        popped = self._workers.pop(subj)
                        cbs = list(popped.on_drained)
                        popped.on_drained.clear()
                        self._finalizing_workers.append(popped)
                        evicted_workers.append((popped, cbs))

                # Enforce max_active_workers including draining workers AND in-progress reservations
                while (
                    len(self._workers)
                    + len(self._draining_workers)
                    + len(self._reserved_subjects)
                ) >= self.max_active_workers:
                    idle_candidates = [
                        (w.last_used, subj)
                        for subj, w in self._workers.items()
                        if w.in_flight == 0
                    ]
                    if not idle_candidates:
                        raise PoolCapacityError(
                            f"Worker pool reached capacity ({self.max_active_workers}) with all workers busy."
                        )
                    idle_candidates.sort()
                    _, victim = idle_candidates[0]
                    w_victim = self._workers.pop(victim)
                    w_victim.draining = True
                    cbs = list(w_victim.on_drained)
                    w_victim.on_drained.clear()
                    self._finalizing_workers.append(w_victim)
                    evicted_workers.append((w_victim, cbs))

                self._reserved_subjects.add(subject)

            for w_evicted, cbs in evicted_workers:
                await self._finalize_worker(w_evicted, cbs)

            proc = None
            profile_path: Path | None = None
            try:
                profile_path = self.store.materialize_encrypted_profile(
                    tenant,
                    instance_id=secrets.token_hex(8),
                )
                port = _find_free_loopback_port()
                proc = self._spawner(tenant, profile_path, port, self.worker_key, self.master_key)
                ready = await self._readiness_probe(port, self.startup_timeout_seconds)
                if not ready or not self._is_alive(proc):
                    self._terminate(proc)
                    raise RuntimeError(f"Worker failed to start for subject '{subject}' on port {port}.")

                worker = _ManagedWorker(
                    subject=subject,
                    port=port,
                    url=f"http://127.0.0.1:{port}/mcp",
                    process=proc,
                    last_used=time.monotonic(),
                    profile_fingerprint=fp,
                    profile_path=profile_path,
                    in_flight=1 if hold_in_flight else 0,
                )
                async with self._pool_lock:
                    self._reserved_subjects.discard(subject)
                    self._workers[subject] = worker
                return worker
            except BaseException:
                async with self._pool_lock:
                    self._reserved_subjects.discard(subject)
                if proc is not None:
                    self._terminate(proc)
                self._unlink_profile(profile_path)
                raise

    async def acquire_worker_handle(self, subject: str) -> _ManagedWorker | None:
        """Acquire a worker handle with `in_flight` incremented; caller MUST call `release_worker_handle`."""
        return await self._ensure_worker_and_acquire(subject, hold_in_flight=True)

    async def _run_on_drained(self, callbacks: list[Callable[[], Any]]) -> None:
        for cb in callbacks:
            try:
                res = cb()
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                pass

    async def release_worker_handle(self, worker: _ManagedWorker) -> None:
        should_finalize = False
        callbacks: list[Callable[[], Any]] = []
        async with self._pool_lock:
            worker.in_flight = max(0, worker.in_flight - 1)
            worker.last_used = time.monotonic()
            if worker.draining and worker.in_flight == 0 and worker in self._draining_workers:
                self._draining_workers.remove(worker)
                self._finalizing_workers.append(worker)
                should_finalize = True
                callbacks = list(worker.on_drained)
                worker.on_drained.clear()
        if should_finalize:
            await self._finalize_worker(worker, callbacks)

    @asynccontextmanager
    async def acquire_worker(self, subject: str) -> AsyncIterator[str | None]:
        """Acquire a worker URL and protect it from eviction until the request completes."""
        handle = await self.acquire_worker_handle(subject)
        if handle is None:
            yield None
            return
        try:
            yield handle.url
        finally:
            await self.release_worker_handle(handle)

    async def resolve_worker_url(self, subject: str) -> str | None:
        """Return active worker URL for `subject`, spawning lazily if needed, or None if not connected."""
        worker = await self._ensure_worker_and_acquire(subject, hold_in_flight=False)
        return worker.url if worker is not None else None

    async def evict_idle_workers(self) -> int:
        """Stop all workers idle beyond `idle_timeout_seconds` that have ZERO in-flight requests."""
        now = time.monotonic()
        victims: list[tuple[_ManagedWorker, list[Callable[[], Any]]]] = []
        dead_draining: list[tuple[_ManagedWorker, list[Callable[[], Any]]]] = []
        async with self._pool_lock:
            alive_draining: list[_ManagedWorker] = []
            for w in self._draining_workers:
                if self._is_alive(w.process):
                    alive_draining.append(w)
                else:
                    cbs = list(w.on_drained)
                    w.on_drained.clear()
                    self._finalizing_workers.append(w)
                    dead_draining.append((w, cbs))
            self._draining_workers[:] = alive_draining

            for subj in list(self._workers):
                w = self._workers[subj]
                if not self._is_alive(w.process):
                    popped = self._workers.pop(subj)
                    cbs = list(popped.on_drained)
                    popped.on_drained.clear()
                    self._finalizing_workers.append(popped)
                    victims.append((popped, cbs))
                elif w.in_flight == 0 and (now - w.last_used) >= self.idle_timeout_seconds:
                    w.draining = True
                    popped = self._workers.pop(subj)
                    cbs = list(popped.on_drained)
                    popped.on_drained.clear()
                    self._finalizing_workers.append(popped)
                    victims.append((popped, cbs))
        for w, cbs in dead_draining:
            await self._finalize_worker(w, cbs)
        for w, cbs in victims:
            await self._finalize_worker(w, cbs)
        return len(victims)

    async def disconnect_worker(
        self,
        subject: str,
        *,
        wipe_profile: bool = False,
        on_drained: Callable[[], Any] | None = None,
        mark_disconnecting: bool = False,
    ) -> tuple[bool, bool]:
        """Stop or drain a worker cleanly (idempotent while draining or revoking).

        Returns `(worker_stopped, still_pending)`:
        - `(True, False)`: worker(s) and/or pending disconnect revocations finished cleanly.
        - `(False, True)`: at least one worker for `subject` still has `in_flight > 0` OR
          a deferred Google revocation/finalization is still in flight; repeated calls
          continue returning `(False, True)` until everything finishes.
        - `(False, False)`: no active, draining, or finalizing worker/revocation existed.
        """
        own_rev_event: asyncio.Event | None = None
        if mark_disconnecting and on_drained is not None:
            # Synchronously register pending revocation BEFORE the first `await` so there is
            # zero race window between `store.revoke_tenant()` and `is_disconnecting() == True`.
            own_rev_event = asyncio.Event()
            self._pending_revocations.setdefault(subject, []).append(own_rev_event)

        handed_off_to_worker = False
        try:
            dead_draining: list[tuple[_ManagedWorker, list[Callable[[], Any]]]] = []
            async with self._lock_for(subject):
                async with self._pool_lock:
                    alive_draining: list[_ManagedWorker] = []
                    for w in self._draining_workers:
                        if w.subject == subject and not self._is_alive(w.process):
                            cbs = list(w.on_drained)
                            w.on_drained.clear()
                            if mark_disconnecting:
                                w.disconnecting = True
                            self._finalizing_workers.append(w)
                            dead_draining.append((w, cbs))
                        else:
                            alive_draining.append(w)
                    self._draining_workers[:] = alive_draining

                    active_worker = self._workers.pop(subject, None)
                    if active_worker is not None:
                        active_worker.draining = True
                        if mark_disconnecting:
                            active_worker.disconnecting = True
                        self._draining_workers.append(active_worker)
                        if on_drained is not None:
                            active_worker.on_drained.append(on_drained)
                            handed_off_to_worker = True

                    targets = [
                        w
                        for w in self._draining_workers
                        if w.subject == subject and self._is_alive(w.process)
                    ]
                    if mark_disconnecting:
                        for w in targets:
                            w.disconnecting = True
                    if active_worker is None and targets and on_drained is not None:
                        targets[-1].on_drained.append(on_drained)
                        handed_off_to_worker = True

                    if handed_off_to_worker and own_rev_event is not None:
                        self._release_pending_revocation_unlocked(subject, own_rev_event)
                        own_rev_event = None

                    finalizing_targets = [
                        w for w in self._finalizing_workers if w.subject == subject
                    ]
                    if mark_disconnecting:
                        for w in finalizing_targets:
                            w.disconnecting = True
                    other_rev_events = [
                        ev
                        for ev in self._pending_revocations.get(subject, [])
                        if ev is not own_rev_event
                    ]

            for dw, cbs in dead_draining:
                await self._finalize_worker(dw, cbs)

            still_pending = False
            for w in targets:
                done = await self._drain_and_maybe_terminate(w, force_on_timeout=False)
                if not done:
                    still_pending = True

            for fw in finalizing_targets:
                if not await self._wait_event(fw.drained_event, self.drain_timeout_seconds):
                    still_pending = True

            for ev in other_rev_events:
                if not await self._wait_event(ev, self.drain_timeout_seconds):
                    still_pending = True

            if on_drained is not None and not handed_off_to_worker:
                await self._run_on_drained([on_drained])

            if still_pending:
                return False, True

            if wipe_profile:
                async with self._pool_lock:
                    exclude = self._live_profile_paths_unlocked(subject)
                self.store.wipe_runtime_profile(subject, exclude_paths=exclude)

            return bool(targets or finalizing_targets), False
        finally:
            if own_rev_event is not None:
                async with self._pool_lock:
                    self._release_pending_revocation_unlocked(subject, own_rev_event)

    async def stop_worker(
        self,
        subject: str,
        *,
        wipe_profile: bool = False,
        force_on_timeout: bool = False,
    ) -> bool:
        if not force_on_timeout:
            stopped, draining = await self.disconnect_worker(subject, wipe_profile=wipe_profile)
            return stopped or draining
        async with self._lock_for(subject):
            async with self._pool_lock:
                worker = self._workers.pop(subject, None)
                if worker is not None:
                    worker.draining = True
                    self._draining_workers.append(worker)
                targets = [w for w in self._draining_workers if w.subject == subject]
                finalizing_targets = [w for w in self._finalizing_workers if w.subject == subject]
            for w in targets:
                await self._drain_and_maybe_terminate(w, force_on_timeout=True)
            for fw in finalizing_targets:
                await fw.drained_event.wait()
            if wipe_profile:
                async with self._pool_lock:
                    exclude = self._live_profile_paths_unlocked(subject)
                self.store.wipe_runtime_profile(subject, exclude_paths=exclude)
            return bool(targets or finalizing_targets)

    async def shutdown_all(self) -> None:
        workers_with_cbs: list[tuple[_ManagedWorker, list[Callable[[], Any]]]] = []
        async with self._pool_lock:
            workers = list(self._workers.values()) + list(self._draining_workers)
            for w in workers:
                w.draining = True
                cbs = list(w.on_drained)
                w.on_drained.clear()
                self._finalizing_workers.append(w)
                workers_with_cbs.append((w, cbs))
            self._workers.clear()
            self._draining_workers.clear()
            self._reserved_subjects.clear()
        for worker, cbs in workers_with_cbs:
            await self._wait_for_drain(worker)
            await self._finalize_worker(worker, cbs)

    def active_worker_count(self) -> int:
        active = sum(1 for w in self._workers.values() if self._is_alive(w.process))
        draining = sum(1 for w in self._draining_workers if self._is_alive(w.process))
        return active + draining
