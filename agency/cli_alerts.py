"""Executable CLI for scheduled or on-demand Google Ads alerts (`google-ads-mcp-server alerts`)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .analysis_profiles import profile_thresholds
from .api import search
from .config import client_display_name, ensure_customer_allowed, load_json_config
from .demo import DEMO_CUSTOMER_IDS
from .reports import _account_metadata, _resolve_alert_customer_ids


def evaluate_alerts(
    *,
    customer_ids: list[str] | None = None,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Evaluate spend, CTR, CTR drop, CPA, and search-term waste thresholds."""
    alert_config = load_json_config("alerts.json", {})
    ids = _resolve_alert_customer_ids(customer_ids, alert_config)
    profile_overrides = profile_thresholds(profile_id) if profile_id else {}
    alerts: list[dict[str, Any]] = []

    for customer_id in ids:
        normalized = ensure_customer_allowed(customer_id)
        currency_code, time_zone = _account_metadata(normalized)
        thresholds = {
            **alert_config.get("defaults", {}),
            **profile_overrides,
            **alert_config.get(normalized, {}),
        }
        rows = search(
            normalized,
            """
            SELECT campaign.id, campaign.name,
                   metrics.impressions, metrics.clicks, metrics.ctr,
                   metrics.cost_micros, metrics.conversions,
                   metrics.cost_per_conversion
            FROM campaign
            WHERE segments.date DURING YESTERDAY
              AND campaign.status = ENABLED
            """,
        )
        baseline_rows = search(
            normalized,
            """
            SELECT campaign.id, metrics.ctr
            FROM campaign
            WHERE segments.date DURING LAST_7_DAYS
              AND campaign.status = ENABLED
            """,
        )
        baseline_ctr = {
            str((row.get("campaign") or {}).get("id")): float((row.get("metrics") or {}).get("ctr", 0))
            for row in baseline_rows
        }
        for row in rows:
            metrics = row.get("metrics", {}) or {}
            campaign_id = str((row.get("campaign") or {}).get("id"))
            weekly_ctr = baseline_ctr.get(campaign_id, 0)
            daily_ctr = float(metrics.get("ctr", 0))
            ctr_drop_pct = (weekly_ctr - daily_ctr) / weekly_ctr * 100 if weekly_ctr else 0
            checks = [
                ("daily_spend", float(metrics.get("costMicros", 0)), float(thresholds.get("max_daily_spend_micros", 0)), ">"),
                ("low_ctr", daily_ctr, float(thresholds.get("min_ctr", 0)), "<"),
                ("ctr_drop", ctr_drop_pct, float(thresholds.get("max_ctr_drop_pct", 0)), ">"),
                ("high_cpa", float(metrics.get("costPerConversion", 0)), float(thresholds.get("max_cpa_micros", 0)), ">"),
            ]
            for alert_type, actual, threshold, operator in checks:
                triggered = threshold and (
                    (operator == ">" and actual > threshold)
                    or (operator == "<" and actual < threshold)
                )
                if triggered:
                    alerts.append(
                        {
                            "customer_id": normalized,
                            "client_name": client_display_name(normalized),
                            "currency_code": currency_code,
                            "time_zone": time_zone,
                            "campaign": row.get("campaign"),
                            "type": alert_type,
                            "actual": actual,
                            "threshold": threshold,
                        }
                    )
        waste_threshold = int(thresholds.get("max_waste_micros", 0) or 0)
        if waste_threshold:
            waste_rows = search(
                normalized,
                f"""
                SELECT search_term_view.search_term, campaign.id, campaign.name,
                       metrics.cost_micros, metrics.conversions
                FROM search_term_view
                WHERE segments.date DURING YESTERDAY
                  AND metrics.conversions = 0
                  AND metrics.cost_micros >= {waste_threshold}
                ORDER BY metrics.cost_micros DESC
                LIMIT 100
                """,
            )
            for row in waste_rows:
                alerts.append(
                    {
                        "customer_id": normalized,
                        "client_name": client_display_name(normalized),
                        "currency_code": currency_code,
                        "time_zone": time_zone,
                        "campaign": row.get("campaign"),
                        "type": "search_term_waste",
                        "actual": (row.get("metrics") or {}).get("costMicros", 0),
                        "threshold": waste_threshold,
                        "search_term": (row.get("searchTermView") or {}).get("searchTerm"),
                    }
                )

    return {
        "profile_id": profile_id,
        "accounts_checked": ids,
        "count": len(alerts),
        "alerts": alerts,
    }


def format_alerts_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Google Ads Alerts",
        "",
        f"- **Accounts checked:** {', '.join(result['accounts_checked'])}",
        f"- **Profile:** {result['profile_id'] or 'default (alerts.json)'}",
        f"- **Triggered alerts:** {result['count']}",
        "",
    ]
    if not result["alerts"]:
        lines.append("No alerts triggered.")
        return "\n".join(lines)
    lines.extend(
        [
            "| Account | Currency | Campaign | Type | Actual | Threshold | Detail |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for alert in result["alerts"]:
        camp_name = (alert.get("campaign") or {}).get("name") or "-"
        detail = alert.get("search_term") or "-"
        lines.append(
            f"| {alert['client_name']} ({alert['customer_id']}) | {alert.get('currency_code', 'UNKNOWN')} "
            f"| {camp_name} | `{alert['type']}` | {alert['actual']} | {alert['threshold']} | {detail} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="google-ads-mcp-server alerts",
        description="Run Google Ads threshold alerts on demand or from cron/CI.",
    )
    parser.add_argument("--customer", help="Comma-separated 10-digit customer IDs")
    parser.add_argument("--profile", dest="profile_id", help="Analysis profile ID for TOML threshold overrides")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--demo", action="store_true", help="Run against synthetic --demo accounts")
    parser.add_argument(
        "--fail-on-alerts",
        action="store_true",
        help="Exit with status 2 when one or more alerts are triggered (useful for cron/CI)",
    )
    args = parser.parse_args(argv)

    previous_env = {
        key: os.environ.get(key)
        for key in ("GOOGLE_ADS_DEMO_MODE", "READ_ONLY_MODE", "ALLOWED_CUSTOMER_IDS")
    }
    try:
        if args.demo:
            os.environ["GOOGLE_ADS_DEMO_MODE"] = "true"
            os.environ["READ_ONLY_MODE"] = "true"
            os.environ["ALLOWED_CUSTOMER_IDS"] = ",".join(DEMO_CUSTOMER_IDS)

        cids = (
            [item.strip() for item in args.customer.split(",") if item.strip()]
            if args.customer
            else None
        )
        result = evaluate_alerts(customer_ids=cids, profile_id=args.profile_id)
        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(format_alerts_markdown(result))

        if args.fail_on_alerts and result["count"] > 0:
            sys.exit(2)
    finally:
        if args.demo:
            for key, old_value in previous_env.items():
                if old_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_value
