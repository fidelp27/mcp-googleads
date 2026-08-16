"""Agency reporting, waste detection, optimization plans and alerts."""

from __future__ import annotations

import html
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any, Optional

from mcp.types import ToolAnnotations

from .api import format_json, search
from .config import (
    client_display_name,
    ensure_customer_allowed,
    get_settings,
    load_json_config,
)
from .governance import approvals, audited_tool


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _period(value: str) -> tuple[date, date]:
    normalized = value.strip().lower()
    today = date.today()
    presets = {
        "week": 7,
        "last_week": 7,
        "semana": 7,
        "month": 30,
        "last_month": 30,
        "mes": 30,
        "quarter": 90,
        "last_quarter": 90,
        "trimestre": 90,
    }
    if normalized in presets:
        days = presets[normalized]
        return today - timedelta(days=days - 1), today
    if ":" in normalized:
        start_raw, end_raw = normalized.split(":", 1)
        start = date.fromisoformat(start_raw)
        end = date.fromisoformat(end_raw)
        if start > end or (end - start).days > 365:
            raise ValueError("Invalid period range")
        return start, end
    raise ValueError(
        "period must be week, month, quarter or YYYY-MM-DD:YYYY-MM-DD"
    )


def _clause(period: tuple[date, date]) -> str:
    return f"segments.date BETWEEN '{period[0].isoformat()}' AND '{period[1].isoformat()}'"


def _sum_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    totals = {
        "impressions": 0.0,
        "clicks": 0.0,
        "cost_micros": 0.0,
        "conversions": 0.0,
        "conversions_value": 0.0,
    }
    for row in rows:
        metrics = row.get("metrics", {})
        for key in totals:
            totals[key] += float(metrics.get(key) or metrics.get(_camel(key)) or 0)
    totals["ctr"] = totals["clicks"] / totals["impressions"] if totals["impressions"] else 0
    totals["cpa_micros"] = (
        totals["cost_micros"] / totals["conversions"] if totals["conversions"] else 0
    )
    totals["roas"] = (
        totals["conversions_value"] / (totals["cost_micros"] / 1_000_000)
        if totals["cost_micros"]
        else 0
    )
    return totals


def _camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part.title() for part in parts[1:])


def _report_data(
    customer_id: str,
    period: tuple[date, date],
    include_recommendations: bool,
) -> dict[str, Any]:
    customer_id = ensure_customer_allowed(customer_id)
    current_rows = search(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status,
               metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value
        FROM campaign
        WHERE {_clause(period)} AND campaign.status != REMOVED
        ORDER BY metrics.cost_micros DESC
        """,
    )
    length = (period[1] - period[0]).days + 1
    previous_end = period[0] - timedelta(days=1)
    previous = (previous_end - timedelta(days=length - 1), previous_end)
    previous_rows = search(
        customer_id,
        f"""
        SELECT metrics.impressions, metrics.clicks, metrics.cost_micros,
               metrics.conversions, metrics.conversions_value
        FROM customer
        WHERE {_clause(previous)}
        """,
    )
    keyword_rows = search(
        customer_id,
        f"""
        SELECT campaign.name, ad_group.name,
               ad_group_criterion.keyword.text,
               ad_group_criterion.keyword.match_type,
               metrics.conversions, metrics.cost_micros
        FROM keyword_view
        WHERE {_clause(period)}
          AND ad_group_criterion.status != REMOVED
        ORDER BY metrics.conversions DESC
        LIMIT 5
        """,
    )
    waste_rows = search(
        customer_id,
        f"""
        SELECT search_term_view.search_term, campaign.id, campaign.name,
               metrics.clicks, metrics.cost_micros, metrics.conversions
        FROM search_term_view
        WHERE {_clause(period)}
          AND metrics.conversions = 0
          AND metrics.cost_micros > 0
        ORDER BY metrics.cost_micros DESC
        LIMIT 20
        """,
    )
    current = _sum_metrics(current_rows)
    previous_totals = _sum_metrics(previous_rows)
    variations: dict[str, float | None] = {}
    for metric in ("impressions", "clicks", "cost_micros", "conversions", "conversions_value"):
        old = previous_totals[metric]
        variations[metric] = ((current[metric] - old) / old * 100) if old else None
    recommendations: list[dict[str, Any]] = []
    if include_recommendations:
        recommendations = search(
            customer_id,
            """
            SELECT recommendation.resource_name, recommendation.type
            FROM recommendation
            LIMIT 20
            """,
        )
    return {
        "customer_id": customer_id,
        "client_name": client_display_name(customer_id),
        "period": {"start": period[0].isoformat(), "end": period[1].isoformat()},
        "previous_period": {
            "start": previous[0].isoformat(),
            "end": previous[1].isoformat(),
        },
        "metrics": current,
        "variation_pct": variations,
        "top_campaigns_by_spend": current_rows[:5],
        "top_keywords_by_conversions": keyword_rows,
        "waste": waste_rows,
        "recommendations": recommendations,
        "suggested_next_actions": _suggestions(current, waste_rows, recommendations),
    }


def _suggestions(
    metrics: dict[str, float],
    waste: list[dict[str, Any]],
    recommendations: list[dict[str, Any]],
) -> list[str]:
    suggestions: list[str] = []
    if waste:
        suggestions.append(
            "Review high-spend search terms with zero conversions and add justified negatives."
        )
    if metrics["conversions"] == 0 and metrics["cost_micros"] > 0:
        suggestions.append("Audit conversion tracking before changing bidding or budgets.")
    if metrics["ctr"] < 0.02 and metrics["impressions"] >= 100:
        suggestions.append("Review RSA messaging and keyword-to-ad relevance.")
    if recommendations:
        suggestions.append("Review Google Ads recommendations; do not apply automatically.")
    return suggestions


def _markdown_report(data: dict[str, Any], branding: dict[str, Any]) -> str:
    metrics = data["metrics"]
    lines = [
        f"# {branding.get('report_title', 'Google Ads performance report')}",
        "",
        f"**Client:** {data['client_name']}",
        f"**Agency:** {branding.get('agency_name', 'Agency')}",
        f"**Period:** {data['period']['start']} to {data['period']['end']}",
        "",
        "## Executive summary",
        "",
        f"- Spend: {metrics['cost_micros'] / 1_000_000:,.2f}",
        f"- Impressions: {metrics['impressions']:,.0f}",
        f"- Clicks: {metrics['clicks']:,.0f}",
        f"- CTR: {metrics['ctr']:.2%}",
        f"- Conversions: {metrics['conversions']:,.2f}",
        f"- CPA: {metrics['cpa_micros'] / 1_000_000:,.2f}",
        "",
        "## Suggested next actions",
        "",
    ]
    lines.extend(f"- {item}" for item in data["suggested_next_actions"])
    lines.extend(["", "## Machine-readable detail", "", "```json", format_json(data), "```"])
    return "\n".join(lines)


def _html_report(data: dict[str, Any], branding: dict[str, Any]) -> str:
    primary = str(branding.get("primary_color", "#1a73e8"))
    markdown = _markdown_report(data, branding)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(str(branding.get('report_title', 'Google Ads report')))}</title>
  <style>
    body {{ font-family: Arial, sans-serif; max-width: 960px; margin: 40px auto; color: #202124; }}
    h1, h2 {{ color: {html.escape(primary)}; }}
    pre {{ white-space: pre-wrap; background: #f8f9fa; padding: 16px; border-radius: 8px; }}
  </style>
</head>
<body>
  <pre>{html.escape(markdown)}</pre>
</body>
</html>"""


def register_report_tools(mcp) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def generate_client_report(
        customer_id: str,
        period: str = "month",
        include_recommendations: bool = True,
        format: str = "markdown",
        branding: Optional[dict[str, Any]] = None,
    ) -> str:
        """Generate a white-label client report in JSON, Markdown or HTML."""
        data = _report_data(customer_id, _period(period), include_recommendations)
        effective_branding = {
            **load_json_config("branding.json", {}),
            **(branding or {}),
        }
        format = format.strip().lower()
        if format == "json":
            return format_json(data)
        if format == "markdown":
            return _markdown_report(data, effective_branding)
        if format == "html":
            return _html_report(data, effective_branding)
        raise ValueError("format must be json, markdown or html")

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def generate_agency_dashboard(days: int = 30) -> str:
        """Generate consolidated spend, metrics, alerts and pending work for all clients."""
        if not 1 <= days <= 365:
            raise ValueError("days must be between 1 and 365")
        clients = load_json_config("clients.json", {})
        customer_ids = list(clients) or list(get_settings().allowed_customer_ids)
        if not customer_ids:
            raise ValueError("Configure clients.json or ALLOWED_CUSTOMER_IDS")
        period = (date.today() - timedelta(days=days - 1), date.today())
        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=min(8, len(customer_ids))) as pool:
            futures = {
                pool.submit(_report_data, customer_id, period, False): customer_id
                for customer_id in customer_ids
            }
            for future in as_completed(futures):
                customer_id = futures[future]
                try:
                    results[customer_id] = future.result()
                except Exception as error:
                    results[customer_id] = {"error": str(error)}
        totals = _sum_metrics(
            [{"metrics": item.get("metrics", {})} for item in results.values() if "metrics" in item]
        )
        pending = [
            item for item in approvals().values() if item.get("status") == "pending"
        ]
        alert_config = load_json_config("alerts.json", {})
        dashboard_alerts: list[dict[str, Any]] = []
        for customer_id, result in results.items():
            if "metrics" not in result:
                continue
            thresholds = {
                **alert_config.get("defaults", {}),
                **alert_config.get(customer_id, {}),
            }
            metrics = result["metrics"]
            average_daily_spend = metrics["cost_micros"] / days
            if (
                thresholds.get("max_daily_spend_micros")
                and average_daily_spend
                > float(thresholds["max_daily_spend_micros"])
            ):
                dashboard_alerts.append(
                    {
                        "customer_id": customer_id,
                        "type": "average_daily_spend",
                        "actual": average_daily_spend,
                        "threshold": thresholds["max_daily_spend_micros"],
                    }
                )
            if (
                thresholds.get("min_ctr")
                and metrics["ctr"] < float(thresholds["min_ctr"])
            ):
                dashboard_alerts.append(
                    {
                        "customer_id": customer_id,
                        "type": "low_ctr",
                        "actual": metrics["ctr"],
                        "threshold": thresholds["min_ctr"],
                    }
                )
        return format_json(
            {
                "period_days": days,
                "managed_totals": totals,
                "clients": results,
                "pending_approvals": len(pending),
                "alerts": dashboard_alerts,
            }
        )

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def compare_periods(
        customer_id: str,
        period_a: str,
        period_b: str,
        metrics: list[str] = [
            "impressions",
            "clicks",
            "cost_micros",
            "conversions",
            "conversions_value",
        ],
    ) -> str:
        """Compare account metrics between two explicit or preset periods."""
        allowed = {
            "impressions", "clicks", "cost_micros", "conversions", "conversions_value"
        }
        if set(metrics) - allowed:
            raise ValueError(f"Unsupported metrics: {sorted(set(metrics) - allowed)}")

        def totals(period_value: str) -> dict[str, float]:
            parsed = _period(period_value)
            rows = search(
                customer_id,
                f"""
                SELECT metrics.impressions, metrics.clicks, metrics.cost_micros,
                       metrics.conversions, metrics.conversions_value
                FROM customer
                WHERE {_clause(parsed)}
                """,
            )
            return _sum_metrics(rows)

        a = totals(period_a)
        b = totals(period_b)
        comparison = {}
        for metric in metrics:
            absolute = b[metric] - a[metric]
            comparison[metric] = {
                "period_a": a[metric],
                "period_b": b[metric],
                "absolute_change": absolute,
                "percentage_change": absolute / a[metric] * 100 if a[metric] else None,
            }
        return format_json(comparison)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def detect_waste(
        customer_id: str,
        days: int = 30,
        min_waste_micros: int = 1_000_000,
    ) -> str:
        """Detect and prioritize non-converting spend and low-value traffic."""
        if min_waste_micros < 0:
            raise ValueError("min_waste_micros cannot be negative")
        period = (date.today() - timedelta(days=days - 1), date.today())
        terms = search(
            customer_id,
            f"""
            SELECT search_term_view.search_term, campaign.id, campaign.name,
                   ad_group.id, ad_group.name, metrics.impressions,
                   metrics.clicks, metrics.cost_micros, metrics.conversions
            FROM search_term_view
            WHERE {_clause(period)}
              AND metrics.conversions = 0
              AND metrics.cost_micros >= {min_waste_micros}
            ORDER BY metrics.cost_micros DESC
            """,
        )
        keywords = search(
            customer_id,
            f"""
            SELECT ad_group_criterion.criterion_id,
                   ad_group_criterion.keyword.text,
                   campaign.id, ad_group.id,
                   metrics.impressions, metrics.ctr,
                   metrics.cost_micros, metrics.conversions
            FROM keyword_view
            WHERE {_clause(period)}
              AND metrics.impressions >= 100
              AND metrics.ctr < 0.01
            ORDER BY metrics.cost_micros DESC
            LIMIT 100
            """,
        )
        ad_groups = search(
            customer_id,
            f"""
            SELECT campaign.id, campaign.name, ad_group.id, ad_group.name,
                   metrics.impressions, metrics.clicks,
                   metrics.cost_micros, metrics.conversions
            FROM ad_group
            WHERE {_clause(period)}
              AND ad_group.status = ENABLED
              AND metrics.conversions = 0
              AND metrics.cost_micros >= {min_waste_micros}
            ORDER BY metrics.cost_micros DESC
            LIMIT 100
            """,
        )
        campaigns = search(
            customer_id,
            f"""
            SELECT campaign.id, campaign.name,
                   metrics.search_impression_share,
                   metrics.search_budget_lost_impression_share,
                   metrics.cost_micros, metrics.conversions
            FROM campaign
            WHERE {_clause(period)}
              AND campaign.status = ENABLED
            ORDER BY metrics.search_budget_lost_impression_share DESC
            LIMIT 100
            """,
        )
        return format_json(
            {
                "customer_id": ensure_customer_allowed(customer_id),
                "period": {"start": period[0].isoformat(), "end": period[1].isoformat()},
                "wasted_search_terms": terms,
                "low_ctr_keywords": keywords,
                "non_converting_ad_groups": ad_groups,
                "budget_limited_campaigns": campaigns,
                "total_detected_waste_micros": sum(
                    int(row.get("metrics", {}).get("costMicros", 0)) for row in terms
                ),
                "warning": "Detected waste is advisory; review relevance before adding negatives.",
            }
        )

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def generate_optimization_plan(
        customer_id: str,
        days: int = 30,
        min_waste_micros: int = 1_000_000,
    ) -> str:
        """Generate prioritized, non-executing MCP actions from current account data."""
        period = (date.today() - timedelta(days=days - 1), date.today())
        terms = search(
            customer_id,
            f"""
            SELECT search_term_view.search_term, campaign.id,
                   metrics.cost_micros, metrics.conversions
            FROM search_term_view
            WHERE {_clause(period)}
              AND metrics.conversions = 0
              AND metrics.cost_micros >= {min_waste_micros}
            ORDER BY metrics.cost_micros DESC
            LIMIT 50
            """,
        )
        ads = search(
            customer_id,
            f"""
            SELECT ad_group.id, ad_group_ad.ad.id,
                   metrics.impressions, metrics.clicks,
                   metrics.cost_micros, metrics.conversions
            FROM ad_group_ad
            WHERE {_clause(period)}
              AND ad_group_ad.status = ENABLED
              AND metrics.impressions >= 100
              AND metrics.conversions = 0
              AND metrics.cost_micros >= {min_waste_micros}
            ORDER BY metrics.cost_micros DESC
            LIMIT 25
            """,
        )
        constrained = search(
            customer_id,
            f"""
            SELECT campaign.id, campaign.name, campaign.primary_status_reasons,
                   campaign_budget.id, campaign_budget.amount_micros,
                   metrics.cost_micros, metrics.conversions,
                   metrics.conversions_value
            FROM campaign
            WHERE {_clause(period)}
              AND campaign.status = ENABLED
            ORDER BY metrics.conversions_value DESC
            LIMIT 100
            """,
        )
        actions = []
        for row in terms:
            actions.append(
                {
                    "priority": "high",
                    "reason": "Search term spent without conversions",
                    "evidence": row,
                    "tool": "add_negative_keyword",
                    "arguments": {
                        "customer_id": ensure_customer_allowed(customer_id),
                        "level": "CAMPAIGN",
                        "parent_id": row["campaign"]["id"],
                        "keyword_text": row["searchTermView"]["searchTerm"],
                        "match_type": "EXACT",
                        "dry_run": True,
                    },
                }
            )
        for row in ads:
            actions.append(
                {
                    "priority": "medium",
                    "reason": "Enabled ad spent without conversions",
                    "evidence": row,
                    "tool": "pause_ad",
                    "arguments": {
                        "customer_id": ensure_customer_allowed(customer_id),
                        "ad_group_id": row["adGroup"]["id"],
                        "ad_id": row["adGroupAd"]["ad"]["id"],
                        "dry_run": True,
                    },
                }
            )
        for row in constrained:
            reasons = row.get("campaign", {}).get("primaryStatusReasons", [])
            if not any("BUDGET" in str(reason) for reason in reasons):
                continue
            current = int(row.get("campaignBudget", {}).get("amountMicros", 0))
            if not current:
                continue
            actions.append(
                {
                    "priority": "medium",
                    "reason": "Campaign reports a budget-related serving constraint",
                    "evidence": row,
                    "tool": "update_campaign_budget",
                    "arguments": {
                        "customer_id": ensure_customer_allowed(customer_id),
                        "budget_id": row["campaignBudget"]["id"],
                        "new_amount_micros": int(current * 1.2),
                        "dry_run": True,
                    },
                }
            )
        return format_json(
            {
                "customer_id": ensure_customer_allowed(customer_id),
                "generated_at": date.today().isoformat(),
                "actions": actions,
                "executed": False,
                "review_required": True,
            }
        )

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def check_alerts(customer_ids: Optional[list[str]] = None) -> str:
        """Evaluate configured spend, CTR, CPA and waste thresholds."""
        alert_config = load_json_config("alerts.json", {})
        ids = customer_ids or list(alert_config) or list(get_settings().allowed_customer_ids)
        alerts: list[dict[str, Any]] = []
        for customer_id in ids:
            normalized = ensure_customer_allowed(customer_id)
            thresholds = {
                **alert_config.get("defaults", {}),
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
                str(row.get("campaign", {}).get("id")): float(
                    row.get("metrics", {}).get("ctr", 0)
                )
                for row in baseline_rows
            }
            for row in rows:
                metrics = row.get("metrics", {})
                campaign_id = str(row.get("campaign", {}).get("id"))
                weekly_ctr = baseline_ctr.get(campaign_id, 0)
                daily_ctr = float(metrics.get("ctr", 0))
                ctr_drop_pct = (
                    (weekly_ctr - daily_ctr) / weekly_ctr * 100 if weekly_ctr else 0
                )
                checks = [
                    (
                        "daily_spend",
                        float(metrics.get("costMicros", 0)),
                        float(thresholds.get("max_daily_spend_micros", 0)),
                        ">",
                    ),
                    (
                        "low_ctr",
                        daily_ctr,
                        float(thresholds.get("min_ctr", 0)),
                        "<",
                    ),
                    (
                        "ctr_drop",
                        ctr_drop_pct,
                        float(thresholds.get("max_ctr_drop_pct", 0)),
                        ">",
                    ),
                    (
                        "high_cpa",
                        float(metrics.get("costPerConversion", 0)),
                        float(thresholds.get("max_cpa_micros", 0)),
                        ">",
                    ),
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
                            "campaign": row.get("campaign"),
                            "type": "search_term_waste",
                            "actual": row.get("metrics", {}).get("costMicros", 0),
                            "threshold": waste_threshold,
                            "search_term": row.get("searchTermView", {}).get(
                                "searchTerm"
                            ),
                        }
                    )
        return format_json({"alerts": alerts, "count": len(alerts)})
