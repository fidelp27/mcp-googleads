"""Uniform mutation execution with validate-only previews and approvals."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from .api import batch_mutate, format_json, mutate
from .config import client_display_name, ensure_customer_allowed, ensure_tool_allowed
from .governance import (
    _normalize,
    build_diff_from_operations,
    create_approval,
    find_idempotent_result,
    record_audit,
    update_approval,
    validate_approval,
)
from .locks import ResourceLockedError, resource_lock


def verify_not_stale(
    expected_state: Any,
    live_state: Any,
    *,
    fields: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Compare snapshot state captured at dry-run against current live state.

    Returns a list of drifted fields (`[{field, expected, actual}]`). Empty list
    means the live resource matches the approved snapshot.
    """
    if not isinstance(expected_state, dict) or not isinstance(live_state, dict):
        if expected_state != live_state:
            return [{"field": "state", "expected": expected_state, "actual": live_state}]
        return []
    keys = list(fields) if fields else sorted(expected_state.keys())
    drifted: list[dict[str, Any]] = []
    for key in keys:
        old_val = expected_state.get(key)
        cur_val = live_state.get(key)
        if _normalize(old_val) != _normalize(cur_val):
            drifted.append({"field": key, "expected": old_val, "actual": cur_val})
    return drifted


def stable_idempotency_key(tool_name: str, customer_id: str, payload: Any) -> str:
    raw = json.dumps(
        {"tool": tool_name, "customer_id": customer_id, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def execute_mutation(
    *,
    tool_name: str,
    customer_id: str,
    service: str,
    operations: list[dict[str, Any]],
    dry_run: bool,
    approval_id: str | None,
    confirmation_text: str | None,
    expected_confirmation: str | None = None,
    previous_state: Any = None,
    idempotency_key: str | None = None,
    batch: bool = False,
    state_verifier: Callable[[], Any] | None = None,
    stale_state_fields: tuple[str, ...] | None = None,
) -> str:
    customer_id = ensure_customer_allowed(customer_id)
    ensure_tool_allowed(tool_name, write=True)
    payload = {"service": service, "operations": operations, "batch": batch}
    key = idempotency_key or stable_idempotency_key(tool_name, customer_id, payload)

    if dry_run:
        response = (
            batch_mutate(customer_id, operations, validate_only=True)
            if batch
            else mutate(customer_id, service, operations, validate_only=True)
        )
        approval = create_approval(
            tool_name,
            customer_id,
            payload,
            expected_confirmation=expected_confirmation,
            previous_state=previous_state,
            idempotency_key=key,
        )
        diff = build_diff_from_operations(previous_state, operations)
        record_audit(
            tool_name,
            customer_id,
            payload,
            "validated",
            {"response": response, "diff": diff},
            dry_run=True,
            operation_id=approval["approval_id"],
            idempotency_key=key,
            previous_state=previous_state,
        )
        return format_json(
            {
                "status": "validated",
                "executed": False,
                "approval_id": approval["approval_id"],
                "expires_at": approval["expires_at"],
                "customer_id": customer_id,
                "client_name": client_display_name(customer_id),
                "tool": tool_name,
                "confirmation_required": bool(expected_confirmation),
                "expected_confirmation": expected_confirmation,
                "client_approval_required": approval["client_approval_required"],
                "preview": payload,
                "diff": diff,
                "google_validation": response,
                "next_step": (
                    f"Call {tool_name} again with the same arguments, dry_run=false, "
                    "this approval_id and the exact confirmation_text when requested."
                ),
            }
        )

    if not approval_id:
        raise PermissionError("approval_id is required when dry_run=false")
    approval = validate_approval(
        approval_id,
        tool_name,
        customer_id,
        payload,
        confirmation_text,
    )
    cached = find_idempotent_result(approval.get("idempotency_key"))
    if cached:
        update_approval(approval_id, status="executed", reused=True)
        return format_json(
            {
                "status": "already_executed",
                "executed": True,
                "approval_id": approval_id,
                "audit_id": cached.get("audit_id"),
                "result": cached.get("result"),
            }
        )
    if state_verifier is not None and approval.get("previous_state") is not None:
        live_state = state_verifier()
        drifted = verify_not_stale(
            approval.get("previous_state"),
            live_state,
            fields=stale_state_fields,
        )
        if drifted:
            update_approval(approval_id, status="stale", stale_diff=drifted)
            record_audit(
                tool_name,
                customer_id,
                payload,
                "stale_state",
                {"drifted_fields": drifted},
                dry_run=False,
                operation_id=approval_id,
                previous_state=approval.get("previous_state"),
            )
            raise PermissionError(
                "Stale approval: resource state in Google Ads changed after dry-run "
                f"({', '.join(item['field'] for item in drifted)}). Request a fresh dry-run."
            )
    try:
        lock_key = _lock_key_for(operations, approval.get("previous_state"))
        if lock_key is None:
            response = (
                batch_mutate(customer_id, operations, validate_only=False)
                if batch
                else mutate(customer_id, service, operations, validate_only=False)
            )
            lock_info = None
        else:
            try:
                with resource_lock(customer_id, lock_key) as info:
                    lock_info = info
                    response = (
                        batch_mutate(customer_id, operations, validate_only=False)
                        if batch
                        else mutate(customer_id, service, operations, validate_only=False)
                    )
            except ResourceLockedError as lock_error:
                record_audit(
                    tool_name,
                    customer_id,
                    payload,
                    "lock_conflict",
                    {
                        "resource_key": lock_error.resource_key,
                        "timeout_seconds": lock_error.timeout_seconds,
                    },
                    dry_run=False,
                    operation_id=approval_id,
                )
                raise
        mutation_id = response.get("requestId") or response.get("request-id")
        audit_id = record_audit(
            tool_name,
            customer_id,
            payload,
            "executed",
            response,
            dry_run=False,
            operation_id=approval_id,
            mutation_id=mutation_id,
            idempotency_key=approval.get("idempotency_key"),
            previous_state=approval.get("previous_state"),
        )
        update_approval(approval_id, status="executed", audit_id=audit_id)
        result_payload = {
            "status": "executed",
            "executed": True,
            "approval_id": approval_id,
            "audit_id": audit_id,
            "customer_id": customer_id,
            "client_name": client_display_name(customer_id),
            "result": response,
        }
        if lock_info is not None:
            result_payload["lock_wait_ms"] = lock_info["lock_wait_ms"]
        return format_json(result_payload)
    except Exception as error:
        record_audit(
            tool_name,
            customer_id,
            payload,
            "error",
            str(error),
            dry_run=False,
            operation_id=approval_id,
        )
        raise


def _lock_key_for(
    operations: list[dict[str, Any]] | None,
    previous_state: Any,
) -> str | None:
    """Pick a resource key to lock: prefer the update's resourceName."""
    if not operations:
        return None
    for op in operations:
        if not isinstance(op, dict):
            continue
        update_body = op.get("update")
        if isinstance(update_body, dict):
            resource = update_body.get("resourceName")
            if resource:
                return str(resource)
    if isinstance(previous_state, dict):
        candidate = previous_state.get("resourceName")
        if candidate:
            return str(candidate)
    return None
