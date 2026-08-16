"""Multi-client MCC tools and local client context."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any, Optional

from mcp.types import ToolAnnotations

from .api import format_json, mutate, search
from .config import (
    client_display_name,
    client_metadata,
    ensure_customer_allowed,
    ensure_tool_allowed,
    get_active_customer_id,
    get_settings,
    set_active_customer_id,
)
from .governance import (
    audited_tool,
    create_approval,
    record_audit,
    update_approval,
    validate_approval,
)


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
LOCAL_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
BULK_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

MAX_BULK_TARGETS = 50


def _id_positive(value: str | int, field: str) -> str:
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return text


def _resource(customer_id: str, collection: str, value: str | int) -> str:
    return f"customers/{customer_id}/{collection}/{_id_positive(value, collection)}"


def _bulk_pause_campaign(customer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    campaign_id = payload.get("campaign_id")
    if not campaign_id:
        raise ValueError(f"targets[{customer_id}] must include 'campaign_id' for pause_campaign")
    resource = _resource(customer_id, "campaigns", campaign_id)
    rows = search(
        customer_id,
        f"SELECT campaign.status, campaign.name FROM campaign WHERE campaign.resource_name = '{resource}'",
    )
    if not rows:
        raise ValueError(f"campaign {campaign_id} not found in customer {customer_id}")
    campaign = rows[0].get("campaign", {}) or {}
    return {
        "service": "campaigns",
        "operations": [
            {
                "update": {"resourceName": resource, "status": "PAUSED"},
                "updateMask": "status",
            }
        ],
        "previous_state": {"status": campaign.get("status")},
        "target_name": campaign.get("name"),
    }


def _bulk_pause_ad(customer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    ad_group_id = payload.get("ad_group_id")
    ad_id = payload.get("ad_id")
    if not ad_group_id or not ad_id:
        raise ValueError(
            f"targets[{customer_id}] must include 'ad_group_id' and 'ad_id' for pause_ad"
        )
    resource = (
        f"customers/{customer_id}/adGroupAds/"
        f"{_id_positive(ad_group_id, 'ad_group_id')}~{_id_positive(ad_id, 'ad_id')}"
    )
    rows = search(
        customer_id,
        f"SELECT ad_group_ad.status FROM ad_group_ad "
        f"WHERE ad_group_ad.resource_name = '{resource}'",
    )
    if not rows:
        raise ValueError(f"ad {ad_id} not found in customer {customer_id}")
    ad = rows[0].get("adGroupAd", {}) or {}
    return {
        "service": "adGroupAds",
        "operations": [
            {
                "update": {"resourceName": resource, "status": "PAUSED"},
                "updateMask": "status",
            }
        ],
        "previous_state": {"status": ad.get("status")},
        "target_name": f"ad {ad_id}",
    }


def _bulk_update_budget(customer_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    budget_id = payload.get("budget_id")
    new_micros = payload.get("amount_micros")
    if not budget_id or new_micros is None:
        raise ValueError(
            f"targets[{customer_id}] must include 'budget_id' and 'amount_micros' for update_campaign_budget"
        )
    try:
        new_amount = int(new_micros)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"targets[{customer_id}].amount_micros must be an integer") from exc
    if new_amount <= 0:
        raise ValueError(f"targets[{customer_id}].amount_micros must be positive")

    settings = get_settings()
    resource = _resource(customer_id, "campaignBudgets", budget_id)
    rows = search(
        customer_id,
        f"SELECT campaign_budget.amount_micros, campaign_budget.name "
        f"FROM campaign_budget WHERE campaign_budget.resource_name = '{resource}'",
    )
    if not rows:
        raise ValueError(f"budget {budget_id} not found in customer {customer_id}")
    budget = rows[0].get("campaignBudget", {}) or {}
    previous_amount = int(budget.get("amountMicros") or 0)

    absolute_increase = new_amount - previous_amount
    if absolute_increase > 0:
        cap_micros = settings.max_daily_budget_increase_micros
        if cap_micros and absolute_increase > cap_micros:
            raise ValueError(
                f"targets[{customer_id}] increase of {absolute_increase} micros exceeds "
                f"MAX_DAILY_BUDGET_INCREASE_MICROS={cap_micros}"
            )
        if previous_amount > 0:
            pct = absolute_increase / previous_amount * 100
            if settings.max_budget_increase_pct and pct > settings.max_budget_increase_pct:
                raise ValueError(
                    f"targets[{customer_id}] increase {pct:.1f}% exceeds "
                    f"MAX_BUDGET_INCREASE_PCT={settings.max_budget_increase_pct}%"
                )

    return {
        "service": "campaignBudgets",
        "operations": [
            {
                "update": {"resourceName": resource, "amountMicros": str(new_amount)},
                "updateMask": "amountMicros",
            }
        ],
        "previous_state": {
            "amount_micros": previous_amount,
            "name": budget.get("name"),
        },
        "target_name": budget.get("name") or f"budget {budget_id}",
    }


BULK_OPERATIONS: dict[str, Any] = {
    "pause_campaign": _bulk_pause_campaign,
    "pause_ad": _bulk_pause_ad,
    "update_campaign_budget": _bulk_update_budget,
}


def _prepare_bulk_targets(
    operation_name: str, targets: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    if operation_name not in BULK_OPERATIONS:
        raise ValueError(
            f"operation_name must be one of {sorted(BULK_OPERATIONS)}"
        )
    if not isinstance(targets, dict) or not targets:
        raise ValueError("targets must be a non-empty dict {customer_id: payload}")
    if len(targets) > MAX_BULK_TARGETS:
        raise ValueError(
            f"targets is limited to {MAX_BULK_TARGETS} customers per bulk call"
        )
    builder = BULK_OPERATIONS[operation_name]
    prepared: list[dict[str, Any]] = []
    for customer_id, payload in targets.items():
        normalized_cid = ensure_customer_allowed(customer_id)
        payload = payload or {}
        info = builder(normalized_cid, payload)
        prepared.append(
            {
                "customer_id": normalized_cid,
                "client_name": client_display_name(normalized_cid),
                "target_payload": payload,
                "service": info["service"],
                "operations": info["operations"],
                "previous_state": info["previous_state"],
                "target_name": info.get("target_name"),
            }
        )
    return prepared


def _mcc_id() -> str:
    value = os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")
    if not value:
        raise ValueError("GOOGLE_ADS_LOGIN_CUSTOMER_ID is required for MCC tools")
    return ensure_customer_allowed(value)


def register_mcc_tools(mcp) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def list_client_accounts() -> str:
        """List client accounts directly or transitively below the configured MCC."""
        mcc_id = _mcc_id()
        rows = search(
            mcc_id,
            """
            SELECT customer_client.id, customer_client.descriptive_name,
                   customer_client.currency_code, customer_client.time_zone,
                   customer_client.status, customer_client.manager,
                   customer_client.level, customer_client.test_account
            FROM customer_client
            WHERE customer_client.level <= 10
            ORDER BY customer_client.level, customer_client.descriptive_name
            """,
        )
        allowed = {
            ensure_customer_allowed(value)
            for value in get_settings().allowed_customer_ids
        }

        def last_activity(customer_id: str) -> str | None:
            activity = search(
                customer_id,
                """
                SELECT segments.date, metrics.impressions
                FROM customer
                WHERE segments.date DURING LAST_30_DAYS
                  AND metrics.impressions > 0
                ORDER BY segments.date DESC
                LIMIT 1
                """,
            )
            return activity[0].get("segments", {}).get("date") if activity else None

        enriched = []
        for row in rows:
            client = dict(row.get("customerClient", {}))
            client_id = str(client.get("id", ""))
            client["lastActivityDate"] = None
            client["activityStatus"] = (
                "not_queried_not_whitelisted"
                if allowed and client_id not in allowed
                else "pending"
            )
            enriched.append(client)
        queryable = [
            client for client in enriched
            if client.get("id") and client["activityStatus"] == "pending"
        ]
        if queryable:
            with ThreadPoolExecutor(max_workers=min(8, len(queryable))) as pool:
                futures = {
                    pool.submit(last_activity, str(client["id"])): client
                    for client in queryable
                }
                for future in as_completed(futures):
                    client = futures[future]
                    try:
                        client["lastActivityDate"] = future.result()
                        client["activityStatus"] = (
                            "active" if client["lastActivityDate"] else "no_recent_activity"
                        )
                    except Exception as error:
                        client["activityStatus"] = f"error: {error}"
        return format_json({"manager_customer_id": mcc_id, "clients": enriched})

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_client_context(customer_id: Optional[str] = None) -> str:
        """Return Google Ads state plus local agency metadata for one client."""
        resolved = customer_id or get_active_customer_id()
        if not resolved:
            raise ValueError("customer_id is required when no active context is set")
        resolved = ensure_customer_allowed(resolved)
        rows = search(
            resolved,
            """
            SELECT customer.id, customer.descriptive_name, customer.currency_code,
                   customer.time_zone, customer.status,
                   customer.optimization_score,
                   metrics.cost_micros
            FROM customer
            WHERE segments.date DURING LAST_30_DAYS
            """,
        )
        campaigns = search(
            resolved,
            """
            SELECT campaign.id, campaign.campaign_budget,
                   campaign_budget.id, campaign_budget.amount_micros
            FROM campaign
            WHERE campaign.status = ENABLED
            """,
        )
        budgets = {
            str(row.get("campaignBudget", {}).get("id")): int(
                row.get("campaignBudget", {}).get("amountMicros", 0)
            )
            for row in campaigns
            if row.get("campaignBudget", {}).get("id")
        }
        return format_json(
            {
                "customer_id": resolved,
                "google_ads": rows[0] if rows else {},
                "active_campaign_count": len(campaigns),
                "managed_daily_budget_micros": sum(budgets.values()),
                "agency_metadata": client_metadata(resolved),
                "active_context": get_active_customer_id() == resolved,
            }
        )

    @mcp.tool(annotations=LOCAL_WRITE)
    @audited_tool
    def switch_context(customer_id: str) -> str:
        """Set the default target client for context-aware local tools."""
        normalized = set_active_customer_id(customer_id)
        return format_json(
            {
                "active_customer_id": normalized,
                "note": (
                    "Explicit customer_id parameters still take precedence. "
                    "GOOGLE_ADS_LOGIN_CUSTOMER_ID remains the MCC authentication header."
                ),
            }
        )

    @mcp.tool(annotations=BULK_WRITE)
    @audited_tool
    def mcc_bulk_apply(
        operation_name: str,
        targets: dict[str, dict[str, Any]],
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Apply the same mutation across N customers under a single approval.

        Currently supported operation_name values:
          - pause_campaign          (target payload: {"campaign_id": ...})
          - pause_ad                (target payload: {"ad_group_id": ..., "ad_id": ...})
          - update_campaign_budget  (target payload: {"budget_id": ..., "amount_micros": ...})

        Flow: call once with dry_run=true to preview every mutation and get an
        approval_id, then call again with dry_run=false + the same targets +
        approval_id + confirmation_text ("EXECUTE BULK <N>" where N is the target
        count). Each per-customer execution is audited individually with a shared
        bulk_id, and per-customer failures never stop the batch.
        """
        ensure_tool_allowed("mcc_bulk_apply", write=True)
        settings = get_settings()
        if settings.client_approval_required:
            raise PermissionError(
                "mcc_bulk_apply is not compatible with CLIENT_APPROVAL_REQUIRED=true; "
                "run per-client approvals instead."
            )

        prepared = _prepare_bulk_targets(operation_name, targets)
        target_count = len(prepared)
        mcc_id = _mcc_id()
        bulk_payload = {
            "operation_name": operation_name,
            "targets": {
                item["customer_id"]: item["target_payload"] for item in prepared
            },
            "target_count": target_count,
        }
        expected_confirmation = f"EXECUTE BULK {target_count}"

        # ----- dry-run preview -----
        if dry_run:
            previews: list[dict[str, Any]] = []
            for item in prepared:
                try:
                    response = mutate(
                        item["customer_id"],
                        item["service"],
                        item["operations"],
                        validate_only=True,
                    )
                    previews.append(
                        {
                            "customer_id": item["customer_id"],
                            "client_name": item["client_name"],
                            "target_name": item["target_name"],
                            "previous_state": item["previous_state"],
                            "google_validation": response,
                            "status": "validated",
                        }
                    )
                except Exception as error:
                    previews.append(
                        {
                            "customer_id": item["customer_id"],
                            "client_name": item["client_name"],
                            "target_name": item["target_name"],
                            "status": "validation_error",
                            "error": str(error),
                        }
                    )
            approval = create_approval(
                "mcc_bulk_apply",
                mcc_id,
                bulk_payload,
                expected_confirmation=expected_confirmation,
                previous_state=[
                    {"customer_id": item["customer_id"], **item["previous_state"]}
                    for item in prepared
                ],
            )
            record_audit(
                "mcc_bulk_apply",
                mcc_id,
                bulk_payload,
                "validated",
                previews,
                dry_run=True,
                operation_id=approval["approval_id"],
            )
            errors = [p for p in previews if p["status"] == "validation_error"]
            return format_json(
                {
                    "status": "validated",
                    "executed": False,
                    "approval_id": approval["approval_id"],
                    "expires_at": approval["expires_at"],
                    "manager_customer_id": mcc_id,
                    "operation_name": operation_name,
                    "target_count": target_count,
                    "expected_confirmation": expected_confirmation,
                    "validation_errors": len(errors),
                    "previews": previews,
                    "next_step": (
                        "Call mcc_bulk_apply again with the same targets, "
                        "dry_run=false, this approval_id, and "
                        f"confirmation_text='{expected_confirmation}'. "
                        "Per-customer failures will not stop the batch."
                    ),
                }
            )

        # ----- execute -----
        if not approval_id:
            raise PermissionError("approval_id is required when dry_run=false")
        approval = validate_approval(
            approval_id,
            "mcc_bulk_apply",
            mcc_id,
            bulk_payload,
            confirmation_text,
        )
        results: list[dict[str, Any]] = []
        for item in prepared:
            try:
                response = mutate(
                    item["customer_id"],
                    item["service"],
                    item["operations"],
                    validate_only=False,
                )
                audit_id = record_audit(
                    "mcc_bulk_apply.execute",
                    item["customer_id"],
                    {
                        "operation_name": operation_name,
                        "target_payload": item["target_payload"],
                    },
                    "executed",
                    response,
                    dry_run=False,
                    operation_id=approval_id,
                    previous_state=item["previous_state"],
                )
                results.append(
                    {
                        "customer_id": item["customer_id"],
                        "client_name": item["client_name"],
                        "target_name": item["target_name"],
                        "status": "executed",
                        "audit_id": audit_id,
                        "result": response,
                    }
                )
            except Exception as error:
                audit_id = record_audit(
                    "mcc_bulk_apply.execute",
                    item["customer_id"],
                    {
                        "operation_name": operation_name,
                        "target_payload": item["target_payload"],
                    },
                    "error",
                    str(error),
                    dry_run=False,
                    operation_id=approval_id,
                )
                results.append(
                    {
                        "customer_id": item["customer_id"],
                        "client_name": item["client_name"],
                        "target_name": item["target_name"],
                        "status": "error",
                        "audit_id": audit_id,
                        "error": str(error),
                    }
                )
        succeeded = sum(1 for r in results if r["status"] == "executed")
        failed = target_count - succeeded
        update_approval(
            approval_id,
            status="executed",
            executed_count=succeeded,
            failed_count=failed,
        )
        return format_json(
            {
                "status": "completed" if failed == 0 else "partial",
                "executed": True,
                "approval_id": approval_id,
                "operation_name": operation_name,
                "target_count": target_count,
                "executed_count": succeeded,
                "failed_count": failed,
                "results": results,
            }
        )

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def run_across_clients(
        operation_name: str,
        customer_ids: list[str],
        params: Optional[dict[str, Any]] = None,
    ) -> str:
        """Run a supported read-only summary operation across clients in parallel."""
        operation = operation_name.strip()
        params = params or {}
        allowed = {"account_summary", "campaign_summary", "device_summary"}
        if operation not in allowed:
            raise ValueError(f"operation_name must be one of {sorted(allowed)}")
        days = int(params.get("days", 30))
        if not 1 <= days <= 365:
            raise ValueError("days must be between 1 and 365")
        end = date.today()
        start = end - timedelta(days=days - 1)
        date_filter = (
            f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
        )
        queries = {
            "account_summary": f"""
                SELECT customer.id, customer.descriptive_name,
                       metrics.impressions, metrics.clicks,
                       metrics.cost_micros, metrics.conversions
                FROM customer
                WHERE {date_filter}
            """,
            "campaign_summary": f"""
                SELECT campaign.id, campaign.name, campaign.status,
                       metrics.impressions, metrics.clicks,
                       metrics.cost_micros, metrics.conversions
                FROM campaign
                WHERE {date_filter}
                  AND campaign.status != REMOVED
            """,
            "device_summary": f"""
                SELECT segments.device, metrics.impressions, metrics.clicks,
                       metrics.cost_micros, metrics.conversions
                FROM customer
                WHERE {date_filter}
            """,
        }
        normalized = [ensure_customer_allowed(value) for value in customer_ids]
        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=min(8, len(normalized))) as pool:
            futures = {
                pool.submit(search, customer_id, queries[operation]): customer_id
                for customer_id in normalized
            }
            for future in as_completed(futures):
                customer_id = futures[future]
                try:
                    results[customer_id] = future.result()
                except Exception as error:
                    results[customer_id] = {"error": str(error)}
        return format_json({"operation": operation, "results": results})
