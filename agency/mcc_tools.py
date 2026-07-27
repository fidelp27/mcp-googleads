"""Multi-client MCC tools and local client context."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any, Optional

from mcp.types import ToolAnnotations

from .api import format_json, search
from .config import (
    client_metadata,
    ensure_customer_allowed,
    get_active_customer_id,
    get_settings,
    set_active_customer_id,
)
from .governance import audited_tool


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
