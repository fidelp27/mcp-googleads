"""MCP tools for audit inspection, approvals and supported reversions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from mcp.types import ToolAnnotations

from .api import format_json
from .audit_retention import export_audit_log as _export_audit_log
from .audit_retention import rotate_audit_log as _rotate_audit_log
from .config import ensure_customer_allowed
from .governance import (
    APPROVAL_PATH,
    approvals,
    audited_tool,
    query_audit_log,
    record_audit,
    update_approval,
)
from .mutations import execute_mutation


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
LOCAL_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def register_governance_tools(mcp) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_audit_log(
        customer_id: Optional[str] = None,
        operator: Optional[str] = None,
        days: int = 30,
        tool_name: Optional[str] = None,
    ) -> str:
        """Query the append-only local audit trail."""
        normalized = ensure_customer_allowed(customer_id) if customer_id else None
        return format_json(
            query_audit_log(normalized, operator, days, tool_name)
        )

    @mcp.tool(annotations=LOCAL_WRITE)
    @audited_tool
    def export_audit_log(
        destination: str,
        format: str = "jsonl",
        days: Optional[int] = None,
        operator: Optional[str] = None,
        customer_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        status: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> str:
        """Write filtered audit entries to a local file.

        Formats: `jsonl` (default, preserves nesting), `csv` (flat cols with
        nested fields as JSON strings), `markdown_report` (grouped by day).
        Filter by `days`, `operator`, `customer_id`, `tool_name`, `status`,
        or ISO `since`/`until` timestamps. Writes with mode 0600 (best effort).
        """
        normalized_customer = (
            ensure_customer_allowed(customer_id) if customer_id else None
        )
        return format_json(
            _export_audit_log(
                destination=destination,
                fmt=format,
                days=days,
                operator=operator,
                customer_id=normalized_customer,
                tool_name=tool_name,
                status=status,
                since=since,
                until=until,
            )
        )

    @mcp.tool(annotations=LOCAL_WRITE)
    @audited_tool
    def rotate_audit_log(
        retention_days: Optional[int] = None,
    ) -> str:
        """Archive daily audit files older than retention_days into monthly zips.

        Defaults to `AUDIT_RETENTION_DAYS` from settings. Only files matching
        `audit-YYYY-MM-DD.jsonl` are archived; the main `audit.jsonl` and
        anything else in the data dir is left alone.
        """
        return format_json(_rotate_audit_log(retention_days=retention_days))

    def pending(min_age_minutes: int, client_only: bool = False) -> list[dict[str, Any]]:
        threshold = datetime.now(timezone.utc) - timedelta(minutes=max(0, min_age_minutes))
        result = []
        for item in approvals().values():
            if item.get("status") != "pending":
                continue
            if client_only and not (
                item.get("client_approval_required") and not item.get("client_approved")
            ):
                continue
            created = datetime.fromisoformat(item["created_at"])
            if created <= threshold:
                result.append(item)
        return result

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_pending_approvals(min_age_minutes: int = 0) -> str:
        """List validated operations still awaiting execution or approval."""
        return format_json(pending(min_age_minutes))

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def list_expiring_approvals(
        minutes_until_expiry: int = 15,
    ) -> str:
        """List pending approvals that will expire within `minutes_until_expiry`.

        Approvals expire after APPROVAL_TTL_MINUTES (default 60) — this tool
        surfaces the ones about to hit the wall so the operator can execute or
        request a fresh dry-run before the current one is dropped. Sorted by
        the shortest time remaining first.
        """
        if minutes_until_expiry < 0:
            raise ValueError("minutes_until_expiry must be >= 0")
        now = datetime.now(timezone.utc)
        soon = now + timedelta(minutes=minutes_until_expiry)
        expiring: list[dict[str, Any]] = []
        for item in approvals().values():
            if item.get("status") != "pending":
                continue
            try:
                expires_at = datetime.fromisoformat(item["expires_at"])
            except (KeyError, ValueError, TypeError):
                continue
            if expires_at > soon:
                continue
            remaining = expires_at - now
            expiring.append(
                {
                    "approval_id": item.get("approval_id"),
                    "tool_name": item.get("tool_name"),
                    "customer_id": item.get("customer_id"),
                    "client_name": item.get("client_name"),
                    "operator": item.get("operator"),
                    "created_at": item.get("created_at"),
                    "expires_at": item.get("expires_at"),
                    "seconds_until_expiry": int(remaining.total_seconds()),
                    "already_expired": remaining.total_seconds() < 0,
                    "expected_confirmation": item.get("expected_confirmation"),
                    "client_approval_required": item.get("client_approval_required"),
                    "client_approved": item.get("client_approved"),
                }
            )
        expiring.sort(key=lambda x: x["seconds_until_expiry"])
        return format_json(
            {
                "as_of": now.isoformat(),
                "minutes_until_expiry": minutes_until_expiry,
                "count": len(expiring),
                "already_expired_count": sum(1 for e in expiring if e["already_expired"]),
                "approvals": expiring,
            }
        )

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def list_pending_client_approvals(min_age_minutes: int = 0) -> str:
        """List changes specifically waiting for client-side approval."""
        return format_json(pending(min_age_minutes, client_only=True))

    @mcp.tool(annotations=LOCAL_WRITE)
    @audited_tool
    def approve_operation(
        approval_id: str,
        client_approval_reference: str,
        consent_confirmed: bool = False,
    ) -> str:
        """Record a client's explicit approval; this does not execute the mutation."""
        if not consent_confirmed:
            raise PermissionError("consent_confirmed=true is required")
        if not client_approval_reference.strip():
            raise ValueError("client_approval_reference is required")
        updated = update_approval(
            approval_id,
            client_approved=True,
            client_approval_reference=client_approval_reference.strip(),
        )
        record_audit(
            "approve_operation",
            updated.get("customer_id"),
            {"approval_id": approval_id, "reference": client_approval_reference},
            "approved",
            {"client_approved": True},
            operation_id=approval_id,
        )
        return format_json(updated)

    @mcp.tool(annotations=LOCAL_WRITE)
    @audited_tool
    def reject_operation(approval_id: str, reason: str) -> str:
        """Reject a pending operation without calling Google Ads."""
        if not reason.strip():
            raise ValueError("reason is required")
        updated = update_approval(
            approval_id, status="rejected", rejection_reason=reason.strip()
        )
        record_audit(
            "reject_operation",
            updated.get("customer_id"),
            {"approval_id": approval_id, "reason": reason},
            "rejected",
            operation_id=approval_id,
        )
        return format_json(updated)

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def revert_operation(
        operation_id: str,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Revert a supported budget or status update using its captured previous state."""
        matches = [
            entry
            for entry in query_audit_log(days=3650)
            if entry.get("audit_id") == operation_id
            or entry.get("operation_id") == operation_id
        ]
        if not matches:
            raise ValueError("Operation was not found in the audit log")
        original = matches[-1]
        if original.get("status") != "executed":
            raise ValueError("Only executed operations can be reverted")
        previous = original.get("previous_state")
        if not previous:
            raise ValueError("This operation has no reversible previous state")
        tool = original.get("tool_name")
        customer_id = ensure_customer_allowed(original["customer_id"])
        if tool == "update_campaign_budget":
            resource_name = previous.get("resourceName")
            amount = previous.get("amountMicros")
            service = "campaignBudgets"
            operations = [
                {
                    "update": {
                        "resourceName": resource_name,
                        "amountMicros": amount,
                    },
                    "updateMask": "amount_micros",
                }
            ]
        elif tool in {
            "update_campaign", "pause_campaign", "enable_campaign",
            "update_ad_group", "pause_ad_group", "enable_ad_group",
        }:
            resource_name = previous.get("resourceName")
            status = previous.get("status")
            if not resource_name or not status:
                raise ValueError("Previous status is not available")
            service = "campaigns" if "campaign" in tool else "adGroups"
            operations = [
                {
                    "update": {"resourceName": resource_name, "status": status},
                    "updateMask": "status",
                }
            ]
        else:
            raise ValueError(f"Automatic revert is not supported for {tool}")
        return execute_mutation(
            tool_name="revert_operation",
            customer_id=customer_id,
            service=service,
            operations=operations,
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
            expected_confirmation=operation_id,
            previous_state=original,
        )
