"""Append-only audit log and approval workflow."""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any, Callable

from .config import DATA_DIR, client_display_name, get_settings


_lock = RLock()
AUDIT_PATH = DATA_DIR / "audit.jsonl"
APPROVAL_PATH = DATA_DIR / "pending_approvals.jsonl"
SENSITIVE_KEYS = {
    "authorization",
    "developer_token",
    "developer-token",
    "client_secret",
    "refresh_token",
    "access_token",
    "image_base64",
    "members",
    "user_identifiers",
    "conversions",
    "adjustments",
    "call_conversions",
    "conversion_adjustments",
    "caller_id",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe(value: Any, key: str = "") -> Any:
    if key.lower() in SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): _safe(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return f"[{len(value)} bytes]"
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _append(path: Path, entry: dict[str, Any]) -> None:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            DATA_DIR.chmod(0o700)
        except OSError:
            pass
        line = json.dumps(_safe(entry), ensure_ascii=False, sort_keys=True) + "\n"
        targets = [path]
        if path == AUDIT_PATH:
            targets.append(DATA_DIR / f"audit-{utc_now().date().isoformat()}.jsonl")
        for target in targets:
            with target.open("a", encoding="utf-8") as stream:
                stream.write(line)
            try:
                target.chmod(0o600)
            except OSError:
                pass


def record_audit(
    tool_name: str,
    customer_id: str | None,
    parameters: dict[str, Any],
    status: str,
    result: Any = None,
    *,
    dry_run: bool | None = None,
    operation_id: str | None = None,
    mutation_id: str | None = None,
    idempotency_key: str | None = None,
    previous_state: Any = None,
) -> str:
    audit_id = str(uuid.uuid4())
    entry = {
        "audit_id": audit_id,
        "timestamp_utc": utc_now().isoformat(),
        "operator": get_settings().operator_name,
        "customer_id": customer_id,
        "client_name": client_display_name(customer_id) if customer_id else None,
        "tool_name": tool_name,
        "parameters": parameters,
        "status": status,
        "result": result,
        "dry_run": dry_run,
        "operation_id": operation_id,
        "mutation_id": mutation_id,
        "idempotency_key": idempotency_key,
        "previous_state": previous_state,
    }
    _append(AUDIT_PATH, entry)
    return audit_id


def audited_tool(function: Callable[..., Any]) -> Callable[..., Any]:
    """Audit an MCP tool without changing its introspected signature."""

    signature = inspect.signature(function)

    if inspect.iscoroutinefunction(function):
        @functools.wraps(function)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            bound = signature.bind_partial(*args, **kwargs)
            customer_id = bound.arguments.get("customer_id")
            try:
                result = await function(*args, **kwargs)
                record_audit(function.__name__, customer_id, dict(bound.arguments), "success", result)
                return result
            except Exception as error:
                record_audit(function.__name__, customer_id, dict(bound.arguments), "error", str(error))
                raise

        return async_wrapper

    @functools.wraps(function)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind_partial(*args, **kwargs)
        customer_id = bound.arguments.get("customer_id")
        try:
            result = function(*args, **kwargs)
            record_audit(function.__name__, customer_id, dict(bound.arguments), "success", result)
            return result
        except Exception as error:
            record_audit(function.__name__, customer_id, dict(bound.arguments), "error", str(error))
            raise

    return sync_wrapper


def _snake_to_camel(value: str) -> str:
    parts = value.split(".")
    def _camel(token: str) -> str:
        chunks = token.split("_")
        return chunks[0] + "".join(c.capitalize() for c in chunks[1:])
    return ".".join(_camel(p) for p in parts)


def _get_nested(obj: Any, path: str) -> Any:
    """Walk a dotted path on nested dicts (safe for missing keys)."""
    if obj is None:
        return None
    current: Any = obj
    for key in path.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current


def build_diff(
    before: Any,
    after: Any,
    *,
    mask_fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return {field, old, new} entries for every changed field.

    When `mask_fields` is provided (snake_case field paths from
    Google Ads `updateMask`), only those fields are compared and the
    output is stable (one entry per mask field, even if unchanged).
    Otherwise the top-level keys of both dicts are compared and only
    the changed ones are emitted. Values are compared by equality
    after JSON-normalizing so tuples vs lists don't false-positive.
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    entries: list[dict[str, Any]] = []
    if mask_fields:
        for snake_path in mask_fields:
            clean = snake_path.strip()
            if not clean:
                continue
            camel_path = _snake_to_camel(clean)
            old_val = _get_nested(before, camel_path)
            new_val = _get_nested(after, camel_path)
            if _normalize(old_val) == _normalize(new_val):
                entries.append({"field": clean, "old": old_val, "new": new_val, "changed": False})
            else:
                entries.append({"field": clean, "old": old_val, "new": new_val, "changed": True})
        return entries
    keys = set(before.keys()) | set(after.keys())
    for key in sorted(keys):
        old_val = before.get(key)
        new_val = after.get(key)
        if _normalize(old_val) != _normalize(new_val):
            entries.append({"field": key, "old": old_val, "new": new_val, "changed": True})
    return entries


def _normalize(value: Any) -> Any:
    """Normalize a value for equality comparison.

    Google returns numeric fields as strings ('amountMicros': '5000000') but
    payloads may use ints. Normalize obvious cases so we don't flag no-ops.
    """
    if isinstance(value, str) and value.lstrip("-").isdigit():
        try:
            return int(value)
        except ValueError:
            return value
    return value


def build_diff_from_operations(
    previous_state: Any,
    operations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """For every update operation, produce a diff against previous_state.

    Handles the common single-resource case (previous_state is a dict for
    one campaign/budget/etc). For multi-resource updates callers should
    pass structured previous_state and expect a per-operation entry back.
    """
    if not operations:
        return []
    if not isinstance(previous_state, (dict, list)):
        return []
    result: list[dict[str, Any]] = []
    for i, op in enumerate(operations):
        update_body = op.get("update") if isinstance(op, dict) else None
        if not isinstance(update_body, dict):
            continue
        mask = (op.get("updateMask") or "").split(",") if isinstance(op, dict) else []
        if isinstance(previous_state, list):
            before = previous_state[i] if i < len(previous_state) else {}
        else:
            before = previous_state
        diff = build_diff(before if isinstance(before, dict) else {}, update_body, mask_fields=[f.strip() for f in mask if f.strip()])
        result.append(
            {
                "resource": update_body.get("resourceName"),
                "update_mask": mask,
                "changes": diff,
            }
        )
    return result


def payload_hash(tool_name: str, customer_id: str, payload: Any) -> str:
    raw = json.dumps(
        {"tool": tool_name, "customer_id": customer_id, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_approval(
    tool_name: str,
    customer_id: str,
    payload: Any,
    *,
    expected_confirmation: str | None = None,
    previous_state: Any = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    created_at = utc_now()
    approval = {
        "event": "created",
        "approval_id": str(uuid.uuid4()),
        "created_at": created_at.isoformat(),
        "expires_at": (created_at + timedelta(minutes=settings.approval_ttl_minutes)).isoformat(),
        "operator": settings.operator_name,
        "customer_id": customer_id,
        "client_name": client_display_name(customer_id),
        "tool_name": tool_name,
        "payload_hash": payload_hash(tool_name, customer_id, payload),
        "payload": payload,
        "expected_confirmation": expected_confirmation,
        "previous_state": previous_state,
        "idempotency_key": idempotency_key,
        "client_approval_required": settings.client_approval_required,
        "client_approved": not settings.client_approval_required,
        "status": "pending",
    }
    _append(APPROVAL_PATH, approval)
    return approval


def _approval_events() -> list[dict[str, Any]]:
    if not APPROVAL_PATH.exists():
        return []
    events: list[dict[str, Any]] = []
    with _lock:
        for line in APPROVAL_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def approvals() -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for event in _approval_events():
        approval_id = event.get("approval_id")
        if not approval_id:
            continue
        if event.get("event") == "created":
            state[approval_id] = dict(event)
        elif approval_id in state:
            state[approval_id].update(event.get("changes", {}))
            state[approval_id]["last_event_at"] = event.get("timestamp_utc")
    now = utc_now()
    for item in state.values():
        expires = datetime.fromisoformat(item["expires_at"])
        if item.get("status") == "pending" and expires < now:
            item["status"] = "expired"
    return state


def get_approval(approval_id: str) -> dict[str, Any]:
    approval = approvals().get(approval_id)
    if not approval:
        raise ValueError("Unknown approval_id")
    return approval


def update_approval(approval_id: str, **changes: Any) -> dict[str, Any]:
    get_approval(approval_id)
    _append(
        APPROVAL_PATH,
        {
            "event": "updated",
            "approval_id": approval_id,
            "timestamp_utc": utc_now().isoformat(),
            "operator": get_settings().operator_name,
            "changes": changes,
        },
    )
    return get_approval(approval_id)


def validate_approval(
    approval_id: str,
    tool_name: str,
    customer_id: str,
    payload: Any,
    confirmation_text: str | None,
) -> dict[str, Any]:
    approval = get_approval(approval_id)
    if approval["status"] != "pending":
        raise PermissionError(f"Approval {approval_id} is {approval['status']}")
    if approval["operator"] != get_settings().operator_name:
        raise PermissionError("Approval belongs to another operator")
    if approval["tool_name"] != tool_name or approval["customer_id"] != customer_id:
        raise PermissionError("Approval does not match this operation")
    if approval["payload_hash"] != payload_hash(tool_name, customer_id, payload):
        raise PermissionError("Payload changed after validation; request a new dry-run")
    expected = approval.get("expected_confirmation")
    if expected and confirmation_text != expected:
        raise PermissionError(f"confirmation_text must exactly match: {expected}")
    if approval.get("client_approval_required") and not approval.get("client_approved"):
        raise PermissionError("This operation is waiting for client approval")
    return approval


def find_idempotent_result(idempotency_key: str | None) -> dict[str, Any] | None:
    if not idempotency_key or not AUDIT_PATH.exists():
        return None
    for line in reversed(AUDIT_PATH.read_text(encoding="utf-8").splitlines()):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("idempotency_key") == idempotency_key and entry.get("status") == "executed":
            return entry
    return None


def query_audit_log(
    customer_id: str | None = None,
    operator: str | None = None,
    days: int = 30,
    tool_name: str | None = None,
) -> list[dict[str, Any]]:
    if not AUDIT_PATH.exists():
        return []
    threshold = utc_now() - timedelta(days=max(1, min(days, 3650)))
    results: list[dict[str, Any]] = []
    for line in AUDIT_PATH.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
            timestamp = datetime.fromisoformat(entry["timestamp_utc"])
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
        if timestamp < threshold:
            continue
        if customer_id and entry.get("customer_id") != customer_id:
            continue
        if operator and entry.get("operator") != operator:
            continue
        if tool_name and entry.get("tool_name") != tool_name:
            continue
        results.append(entry)
    return results
