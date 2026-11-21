"""Agency reporting, waste detection, optimization plans and alerts."""

from __future__ import annotations

import html
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Optional

from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .api import format_json, search, search as _api_search, search_with_meta
from .config import (
    client_display_name,
    client_metadata,
    ensure_customer_allowed,
    get_settings,
    load_json_config,
)
from .dates import DateWindow, coverage_metadata, resolve_date_range
from .governance import approvals, audited_tool


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _period_window(
    value: str | int | tuple[date, date],
    *,
    time_zone: str | None = None,
    complete_days_only: bool = True,
) -> DateWindow:
    if isinstance(value, tuple):
        return resolve_date_range(
            time_zone=time_zone,
            start_date=value[0].isoformat(),
            end_date=value[1].isoformat(),
        )
    if isinstance(value, int):
        return resolve_date_range(
            days=value,
            time_zone=time_zone,
            complete_days_only=complete_days_only,
        )
    normalized = value.strip().lower()
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
        return resolve_date_range(
            days=presets[normalized],
            time_zone=time_zone,
            complete_days_only=complete_days_only,
        )
    if ":" in normalized:
        start_raw, end_raw = normalized.split(":", 1)
        return resolve_date_range(
            time_zone=time_zone,
            start_date=start_raw,
            end_date=end_raw,
        )
    raise ValueError(
        "period must be week, month, quarter or YYYY-MM-DD:YYYY-MM-DD"
    )


def _period(value: str) -> tuple[date, date]:
    window = _period_window(value, complete_days_only=True)
    return window.start, window.end


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


def _account_metadata(customer_id: str) -> tuple[str, str | None]:
    rows = search(
        customer_id,
        """
        SELECT customer.currency_code, customer.time_zone
        FROM customer
        LIMIT 1
        """,
    )
    customer = rows[0].get("customer", {}) if rows else {}
    currency_code = (
        customer.get("currencyCode")
        or customer.get("currency_code")
        or client_metadata(customer_id).get("currency")
        or "UNKNOWN"
    )
    time_zone = customer.get("timeZone") or customer.get("time_zone")
    return str(currency_code).upper(), str(time_zone) if time_zone else None


def _search_bounded(customer_id: str, query: str) -> tuple[list[dict[str, Any]], bool]:
    """Run bounded search returning `(rows, truncated)` while preserving `reports.search` monkeypatches."""
    if search is not _api_search:
        return search(customer_id, query), False
    meta = search_with_meta(customer_id, query)
    return meta["rows"], bool(meta["truncated"])


def _report_data(
    customer_id: str,
    period_spec: str | int | tuple[date, date],
    include_recommendations: bool,
    *,
    complete_days_only: bool = True,
) -> dict[str, Any]:
    customer_id = ensure_customer_allowed(customer_id)
    currency_code, time_zone = _account_metadata(customer_id)
    window = _period_window(
        period_spec,
        time_zone=time_zone,
        complete_days_only=complete_days_only,
    )
    period = (window.start, window.end)
    coverage = coverage_metadata(currency_code=currency_code, window=window)
    current_rows, current_truncated = _search_bounded(
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
    previous_rows, previous_truncated = _search_bounded(
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
    any_truncated = bool(current_truncated or previous_truncated)
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
    payload: dict[str, Any] = {
        "customer_id": customer_id,
        "client_name": client_display_name(customer_id),
        **coverage,
        "truncated": any_truncated,
        "totals_are_partial": any_truncated,
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
    if any_truncated:
        payload["truncation_warning"] = (
            "One or more queries reached GOOGLE_ADS_MAX_SEARCH_ROWS; account totals reflect only "
            "the returned subset. Narrow the date range or raise GOOGLE_ADS_MAX_SEARCH_ROWS."
        )
    return payload


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
    currency = data.get("currency_code") or "UNKNOWN"
    currency_suffix = f" {currency}" if currency != "UNKNOWN" else ""
    lines = [
        f"# {branding.get('report_title', 'Google Ads performance report')}",
        "",
        f"**Client:** {data['client_name']}",
        f"**Agency:** {branding.get('agency_name', 'Agency')}",
        f"**Currency:** {currency}",
        f"**Period:** {data['period']['start']} to {data['period']['end']}",
        "",
        "## Executive summary",
        "",
        f"- Spend: {metrics['cost_micros'] / 1_000_000:,.2f}{currency_suffix}",
        f"- Impressions: {metrics['impressions']:,.0f}",
        f"- Clicks: {metrics['clicks']:,.0f}",
        f"- CTR: {metrics['ctr']:.2%}",
        f"- Conversions: {metrics['conversions']:,.2f}",
        f"- CPA: {metrics['cpa_micros'] / 1_000_000:,.2f}{currency_suffix}",
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


def _consolidate_by_currency(
    results: dict[str, Any],
) -> tuple[dict[str, float] | None, dict[str, dict[str, float]], list[str], bool, str | None]:
    """Aggregate client metrics strictly per currency to avoid mixing currencies."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in results.values():
        if "metrics" not in item:
            continue
        currency = str(item.get("currency_code") or "UNKNOWN").upper()
        buckets.setdefault(currency, []).append({"metrics": item["metrics"]})

    currencies = sorted(buckets)
    totals_by_currency = {
        currency: _sum_metrics(rows) for currency, rows in sorted(buckets.items())
    }
    if not currencies:
        return _sum_metrics([]), {}, [], False, None
    if len(currencies) == 1:
        only_currency = currencies[0]
        return (
            {"currency_code": only_currency, **totals_by_currency[only_currency]},
            totals_by_currency,
            currencies,
            False,
            None,
        )
    warning = (
        f"Clients use multiple currencies ({', '.join(currencies)}). "
        "Totals are separated in managed_totals_by_currency and not combined."
    )
    return None, totals_by_currency, currencies, True, warning


def _resolve_alert_customer_ids(
    customer_ids: Optional[list[str]],
    alert_config: dict[str, Any],
) -> list[str]:
    """Resolve target customer IDs for check_alerts without treating 'defaults' as a customer."""
    if customer_ids:
        return list(customer_ids)
    configured = [key for key in alert_config if key != "defaults"]
    if configured:
        return configured
    clients = load_json_config("clients.json", {})
    if clients:
        return list(clients)
    allowed = sorted(get_settings().allowed_customer_ids)
    if allowed:
        return allowed
    raise ValueError(
        "Provide customer_ids or configure customer entries in alerts.json, "
        "clients.json, or ALLOWED_CUSTOMER_IDS"
    )


class ReportBranding(BaseModel):
    """Typed branding overrides for white-label reports (strict JSON Schema compatible)."""

    agency_name: Optional[str] = Field(default=None, description="Agency display name")
    report_title: Optional[str] = Field(default=None, description="Custom report title")
    primary_color: Optional[str] = Field(default=None, description="Hex brand color (e.g. #1a73e8)")
    logo_url: Optional[str] = Field(default=None, description="Optional agency logo URL")


def register_report_tools(mcp) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def generate_client_report(
        customer_id: str,
        period: str = "month",
        include_recommendations: bool = True,
        format: str = "markdown",
        branding: Optional[ReportBranding] = None,
        complete_days_only: bool = True,
    ) -> str:
        """Generate a white-label client report in JSON, Markdown or HTML using the account time zone."""
        data = _report_data(
            customer_id,
            period,
            include_recommendations,
            complete_days_only=complete_days_only,
        )
        branding_overrides: dict[str, Any] = {}
        if isinstance(branding, ReportBranding):
            branding_overrides = branding.model_dump(exclude_none=True)
        elif isinstance(branding, dict):
            branding_overrides = {k: v for k, v in branding.items() if v is not None}
        effective_branding = {
            **load_json_config("branding.json", {}),
            **branding_overrides,
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
    def generate_agency_dashboard(
        days: int = 30,
        complete_days_only: bool = True,
    ) -> str:
        """Generate consolidated spend, metrics, alerts and pending work for all clients (currencies separated)."""
        if not 1 <= days <= 365:
            raise ValueError("days must be between 1 and 365")
        clients = load_json_config("clients.json", {})
        customer_ids = list(clients) or list(get_settings().allowed_customer_ids)
        if not customer_ids:
            raise ValueError("Configure clients.json or ALLOWED_CUSTOMER_IDS")
        results: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=min(8, len(customer_ids))) as pool:
            futures = {
                pool.submit(
                    _report_data,
                    customer_id,
                    days,
                    False,
                    complete_days_only=complete_days_only,
                ): customer_id
                for customer_id in customer_ids
            }
            for future in as_completed(futures):
                customer_id = futures[future]
                try:
                    results[customer_id] = future.result()
                except Exception as error:
                    results[customer_id] = {"error": str(error)}
        (
            totals,
            totals_by_currency,
            currencies,
            mixed_currencies,
            currency_warning,
        ) = _consolidate_by_currency(results)
        pending = [
            item for item in approvals().values() if item.get("status") == "pending"
        ]
        alert_config = load_json_config("alerts.json", {})
        dashboard_alerts: list[dict[str, Any]] = []
        for customer_id, result in results.items():
            if "metrics" not in result:
                continue
            normalized_cid = result.get("customer_id", customer_id)
            thresholds = {
                **alert_config.get("defaults", {}),
                **alert_config.get(normalized_cid, {}),
            }
            metrics = result["metrics"]
            currency_code = result.get("currency_code") or "UNKNOWN"
            average_daily_spend = metrics["cost_micros"] / days
            if (
                thresholds.get("max_daily_spend_micros")
                and average_daily_spend
                > float(thresholds["max_daily_spend_micros"])
            ):
                dashboard_alerts.append(
                    {
                        "customer_id": normalized_cid,
                        "currency_code": currency_code,
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
                        "customer_id": normalized_cid,
                        "currency_code": currency_code,
                        "type": "low_ctr",
                        "actual": metrics["ctr"],
                        "threshold": thresholds["min_ctr"],
                    }
                )
        truncated_clients = sorted(
            cid_key
            for cid_key, item in results.items()
            if isinstance(item, dict) and item.get("truncated")
        )
        any_client_truncated = bool(truncated_clients)
        payload: dict[str, Any] = {
            "period_days": days,
            "complete_days_only": complete_days_only,
            "queried_at_utc": datetime.now(timezone.utc).isoformat(),
            "currencies": currencies,
            "mixed_currencies": mixed_currencies,
            "truncated": any_client_truncated,
            "totals_are_partial": any_client_truncated,
            "managed_totals": totals,
            "managed_totals_by_currency": totals_by_currency,
            "clients": results,
            "pending_approvals": len(pending),
            "alerts": dashboard_alerts,
        }
        if currency_warning:
            payload["currency_warning"] = currency_warning
        if any_client_truncated:
            payload["truncated_customer_ids"] = truncated_clients
            payload["truncation_warning"] = (
                f"One or more client reports reached GOOGLE_ADS_MAX_SEARCH_ROWS ({', '.join(truncated_clients)}); "
                "agency totals include partial client metrics."
            )
        return format_json(payload)

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
        complete_days_only: bool = True,
    ) -> str:
        """Compare account metrics between two explicit or preset periods in the account time zone."""
        allowed = {
            "impressions", "clicks", "cost_micros", "conversions", "conversions_value"
        }
        if set(metrics) - allowed:
            raise ValueError(f"Unsupported metrics: {sorted(set(metrics) - allowed)}")

        cid = ensure_customer_allowed(customer_id)
        currency_code, time_zone = _account_metadata(cid)

        def totals(period_value: str) -> tuple[DateWindow, dict[str, float], bool]:
            win = _period_window(
                period_value,
                time_zone=time_zone,
                complete_days_only=complete_days_only,
            )
            rows, is_truncated = _search_bounded(
                cid,
                f"""
                SELECT metrics.impressions, metrics.clicks, metrics.cost_micros,
                       metrics.conversions, metrics.conversions_value
                FROM customer
                WHERE {win.gaql_clause}
                """,
            )
            return win, _sum_metrics(rows), is_truncated

        win_a, a, trunc_a = totals(period_a)
        win_b, b, trunc_b = totals(period_b)
        any_truncated = bool(trunc_a or trunc_b)
        comparison = {}
        for metric in metrics:
            absolute = b[metric] - a[metric]
            comparison[metric] = {
                "period_a": a[metric],
                "period_b": b[metric],
                "absolute_change": absolute,
                "percentage_change": absolute / a[metric] * 100 if a[metric] else None,
            }
        comp_payload: dict[str, Any] = {
            "customer_id": cid,
            "currency_code": currency_code,
            "time_zone": win_b.time_zone,
            "queried_at_utc": datetime.now(timezone.utc).isoformat(),
            "period_a": win_a.to_dict(),
            "period_b": win_b.to_dict(),
            "truncated": any_truncated,
            "totals_are_partial": any_truncated,
            "conversion_lag_warning": win_b.conversion_lag_warning or win_a.conversion_lag_warning,
            "metrics": comparison,
        }
        if any_truncated:
            comp_payload["truncation_warning"] = (
                "One or more period queries reached GOOGLE_ADS_MAX_SEARCH_ROWS; "
                "compared totals reflect only the returned subset."
            )
        return format_json(comp_payload)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_performance_trends(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
        days: Annotated[int, Field(description="Lookback window (1 to 365 days)")] = 30,
        granularity: Annotated[
            str,
            Field(description="Aggregation granularity: 'total', 'daily', or 'weekly'"),
        ] = "daily",
        complete_days_only: Annotated[
            bool,
            Field(description="Exclude the current incomplete day in the account time zone (recommended)"),
        ] = True,
        campaign_id: Annotated[
            Optional[str],
            Field(description="Optional campaign ID filter"),
        ] = None,
        include_conversions_by_conversion_date: Annotated[
            bool,
            Field(description="Include conversions_by_conversion_date alongside interaction-date conversions"),
        ] = True,
    ) -> str:
        """Return total, daily, or weekly performance trends with timezone, currency, and conversion-lag metadata."""
        cid = ensure_customer_allowed(customer_id)
        gran = (granularity or "daily").strip().lower()
        if gran not in {"total", "daily", "weekly"}:
            raise ValueError("granularity must be 'total', 'daily', or 'weekly'")
        if campaign_id is not None:
            clean_camp = str(campaign_id).strip()
            if not clean_camp.isdigit() or int(clean_camp) <= 0:
                raise ValueError("campaign_id must be a positive integer")
        else:
            clean_camp = None

        currency_code, time_zone = _account_metadata(cid)
        window = resolve_date_range(
            days=days,
            time_zone=time_zone,
            complete_days_only=complete_days_only,
        )
        coverage = coverage_metadata(currency_code=currency_code, window=window)

        segment_field = {
            "total": None,
            "daily": "segments.date",
            "weekly": "segments.week",
        }[gran]
        select_fields = [
            "metrics.impressions",
            "metrics.clicks",
            "metrics.cost_micros",
            "metrics.conversions",
            "metrics.conversions_value",
        ]
        if include_conversions_by_conversion_date:
            select_fields.extend(
                [
                    "metrics.conversions_by_conversion_date",
                    "metrics.conversions_value_by_conversion_date",
                ]
            )
        if segment_field:
            select_fields.insert(0, segment_field)

        resource = "campaign" if clean_camp else "customer"
        where_parts = [window.gaql_clause]
        if clean_camp:
            where_parts.append(f"campaign.id = {clean_camp}")
        order_clause = f"ORDER BY {segment_field} ASC" if segment_field else ""
        query = (
            f"SELECT {', '.join(select_fields)} "
            f"FROM {resource} "
            f"WHERE {' AND '.join(where_parts)} "
            f"{order_clause}"
        )
        meta = search_with_meta(cid, query)
        series: list[dict[str, Any]] = []
        total_by_conv_date = 0.0
        total_val_by_conv_date = 0.0
        for row in meta["rows"]:
            m = row.get("metrics", {}) or {}
            seg = row.get("segments", {}) or {}
            impressions = float(m.get("impressions") or 0)
            clicks = float(m.get("clicks") or 0)
            cost_micros = float(m.get("costMicros") or m.get("cost_micros") or 0)
            conversions = float(m.get("conversions") or 0)
            conv_value = float(m.get("conversionsValue") or m.get("conversions_value") or 0)
            point: dict[str, Any] = {
                "impressions": impressions,
                "clicks": clicks,
                "ctr": round(clicks / impressions, 4) if impressions else 0.0,
                "cost_micros": cost_micros,
                "conversions": conversions,
                "conversions_value": conv_value,
            }
            if segment_field == "segments.date":
                point["date"] = seg.get("date")
            elif segment_field == "segments.week":
                point["week"] = seg.get("week")
            if include_conversions_by_conversion_date:
                conv_cd = float(
                    m.get("conversionsByConversionDate")
                    or m.get("conversions_by_conversion_date")
                    or 0
                )
                val_cd = float(
                    m.get("conversionsValueByConversionDate")
                    or m.get("conversions_value_by_conversion_date")
                    or 0
                )
                point["conversions_by_conversion_date"] = conv_cd
                point["conversions_value_by_conversion_date"] = val_cd
                total_by_conv_date += conv_cd
                total_val_by_conv_date += val_cd
            series.append(point)

        totals = _sum_metrics(meta["rows"])
        if include_conversions_by_conversion_date:
            totals["conversions_by_conversion_date"] = round(total_by_conv_date, 4)
            totals["conversions_value_by_conversion_date"] = round(total_val_by_conv_date, 4)

        payload: dict[str, Any] = {
            "customer_id": cid,
            "client_name": client_display_name(cid),
            "campaign_id": clean_camp,
            "granularity": gran,
            **coverage,
            "returned_rows": meta["returned_rows"],
            "truncated": meta["truncated"],
            "totals_are_partial": meta["truncated"],
            "totals": totals,
            "series": series,
            "conversion_date_note": (
                "`conversions` attributes events to the ad interaction (click) date; "
                "`conversions_by_conversion_date` attributes them to the date the conversion occurred."
            ),
        }
        if meta["truncated"]:
            payload["truncation_warning"] = (
                "Trend query reached GOOGLE_ADS_MAX_SEARCH_ROWS; totals and series reflect only "
                "the returned subset."
            )
        return format_json(payload)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def detect_waste(
        customer_id: str,
        days: int = 30,
        min_waste_micros: int = 1_000_000,
        complete_days_only: bool = True,
    ) -> str:
        """Detect and prioritize non-converting spend and low-value traffic in the account time zone."""
        if min_waste_micros < 0:
            raise ValueError("min_waste_micros cannot be negative")
        cid = ensure_customer_allowed(customer_id)
        currency_code, time_zone = _account_metadata(cid)
        window = resolve_date_range(
            days=days,
            time_zone=time_zone,
            complete_days_only=complete_days_only,
        )
        period = (window.start, window.end)
        coverage = coverage_metadata(currency_code=currency_code, window=window)
        terms, terms_truncated = _search_bounded(
            cid,
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
            cid,
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
            cid,
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
            cid,
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
        waste_payload: dict[str, Any] = {
            "customer_id": cid,
            **coverage,
            "truncated": terms_truncated,
            "totals_are_partial": terms_truncated,
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
        if terms_truncated:
            waste_payload["truncation_warning"] = (
                "Wasted search terms query reached GOOGLE_ADS_MAX_SEARCH_ROWS; "
                "total_detected_waste_micros reflects only the returned subset."
            )
        return format_json(waste_payload)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def generate_optimization_plan(
        customer_id: str,
        days: int = 30,
        min_waste_micros: int = 1_000_000,
        complete_days_only: bool = True,
    ) -> str:
        """Generate prioritized, non-executing MCP actions from current account data in the account time zone."""
        cid = ensure_customer_allowed(customer_id)
        currency_code, time_zone = _account_metadata(cid)
        window = resolve_date_range(
            days=days,
            time_zone=time_zone,
            complete_days_only=complete_days_only,
        )
        period = (window.start, window.end)
        coverage = coverage_metadata(currency_code=currency_code, window=window)
        terms = search(
            cid,
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
            cid,
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
            cid,
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
                        "customer_id": cid,
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
                        "customer_id": cid,
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
                        "customer_id": cid,
                        "budget_id": row["campaignBudget"]["id"],
                        "new_amount_micros": int(current * 1.2),
                        "dry_run": True,
                    },
                }
            )
        return format_json(
            {
                "customer_id": cid,
                **coverage,
                "period": {"start": period[0].isoformat(), "end": period[1].isoformat()},
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
        ids = _resolve_alert_customer_ids(customer_ids, alert_config)
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
