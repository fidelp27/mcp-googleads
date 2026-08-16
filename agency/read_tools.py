"""Complementary read-only Google Ads tools."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from .api import format_json, search
from .config import ensure_customer_allowed, ensure_tool_allowed
from .governance import audited_tool


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)


def _date_clause(days: int, field: str = "segments.date") -> str:
    if not 1 <= days <= 365:
        raise ValueError("days must be between 1 and 365")
    end = date.today()
    start = end - timedelta(days=days - 1)
    return f"{field} BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"


def _id(value: str | int | None, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return text


def _run(tool_name: str, customer_id: str, query: str) -> str:
    ensure_tool_allowed(tool_name)
    customer_id = ensure_customer_allowed(customer_id)
    return format_json(
        {
            "customer_id": customer_id,
            "tool": tool_name,
            "fresh": True,
            "results": search(customer_id, query),
        }
    )


def _summarize_enhanced_conversions(
    *,
    customer_id: str,
    customer_rows: list[dict],
    action_rows: list[dict],
    metric_rows: list[dict],
    days: int,
) -> dict:
    """Turn raw GAQL rows into an Enhanced Conversions readiness report.

    Kept as a pure function so it can be tested without touching Google Ads.
    """
    # ----- customer-level tracking setup -----
    tracking: dict = {
        "conversion_tracking_id": None,
        "status": None,
        "google_ads_conversion_customer": None,
        "accepted_customer_data_terms": None,
        "enhanced_conversions_for_leads_enabled": None,
    }
    customer_name = None
    if customer_rows:
        customer = customer_rows[0].get("customer", {}) or {}
        customer_name = customer.get("descriptiveName")
        setting = customer.get("conversionTrackingSetting", {}) or {}
        tracking["conversion_tracking_id"] = setting.get("conversionTrackingId")
        tracking["status"] = setting.get("conversionTrackingStatus")
        tracking["google_ads_conversion_customer"] = setting.get("googleAdsConversionCustomer")
        tracking["accepted_customer_data_terms"] = setting.get("acceptedCustomerDataTerms")
        tracking["enhanced_conversions_for_leads_enabled"] = setting.get(
            "enhancedConversionsForLeadsEnabled"
        )

    # ----- per-action volume roll-up -----
    volume_by_action: dict[str, dict] = {}
    for row in metric_rows:
        segments = row.get("segments", {}) or {}
        metrics = row.get("metrics", {}) or {}
        resource = segments.get("conversionAction")
        if not resource:
            continue
        bucket = volume_by_action.setdefault(
            resource,
            {"all_conversions": 0.0, "conversions": 0.0, "conversions_value": 0.0},
        )
        bucket["all_conversions"] += float(metrics.get("allConversions") or 0)
        bucket["conversions"] += float(metrics.get("conversions") or 0)
        bucket["conversions_value"] += float(metrics.get("conversionsValue") or 0)

    # ----- per-action projection -----
    actions: list[dict] = []
    for row in action_rows:
        action = row.get("conversionAction", {}) or {}
        attribution = action.get("attributionModelSettings", {}) or {}
        resource_name = action.get("resourceName") or (
            f"customers/{customer_id}/conversionActions/{action.get('id')}"
            if action.get("id")
            else None
        )
        volume = volume_by_action.get(resource_name, {})
        actions.append(
            {
                "id": action.get("id"),
                "name": action.get("name"),
                "status": action.get("status"),
                "type": action.get("type"),
                "category": action.get("category"),
                "origin": action.get("origin"),
                "primary_for_goal": bool(action.get("primaryForGoal")),
                "included_in_conversions_metric": bool(
                    action.get("includeInConversionsMetric")
                ),
                "counting_type": action.get("countingType"),
                "attribution_model": attribution.get("attributionModel"),
                "data_driven_model_status": attribution.get("dataDrivenModelStatus"),
                "click_through_lookback_days": _to_int(
                    action.get("clickThroughLookbackWindowDays")
                ),
                "view_through_lookback_days": _to_int(
                    action.get("viewThroughLookbackWindowDays")
                ),
                "metrics": {
                    "window_days": days,
                    "all_conversions": round(volume.get("all_conversions", 0.0), 4),
                    "conversions": round(volume.get("conversions", 0.0), 4),
                    "conversions_value": round(volume.get("conversions_value", 0.0), 4),
                },
            }
        )

    # ----- alerts -----
    alerts: list[dict] = []
    has_lead_action = any(a["category"] == "LEAD" for a in actions)
    has_enabled_primary = any(
        a["status"] == "ENABLED" and a["primary_for_goal"] for a in actions
    )
    total_conversions_30d = sum(a["metrics"]["conversions"] for a in actions)

    if tracking["accepted_customer_data_terms"] is not True:
        alerts.append(
            {
                "severity": "critical" if has_lead_action else "warning",
                "code": "CUSTOMER_DATA_TERMS_NOT_ACCEPTED",
                "message": (
                    "Customer Data Terms not accepted. Enhanced Conversions and "
                    "Customer Match cannot ingest data until accepted at "
                    "Admin > Data manager."
                ),
            }
        )
    if has_lead_action and tracking["enhanced_conversions_for_leads_enabled"] is not True:
        alerts.append(
            {
                "severity": "high",
                "code": "EC4L_DISABLED_WITH_LEAD_ACTIONS",
                "message": (
                    "LEAD-category conversion actions exist but Enhanced "
                    "Conversions for Leads is disabled at customer level."
                ),
            }
        )
    if not has_enabled_primary:
        alerts.append(
            {
                "severity": "high",
                "code": "NO_PRIMARY_FOR_GOAL",
                "message": (
                    "No ENABLED conversion action is marked primary_for_goal. "
                    "Smart Bidding cannot optimize without a primary action."
                ),
            }
        )
    if total_conversions_30d == 0 and any(a["status"] == "ENABLED" for a in actions):
        alerts.append(
            {
                "severity": "info",
                "code": "NO_CONVERSIONS_IN_WINDOW",
                "message": (
                    f"Zero conversions recorded in the last {days} days across "
                    "enabled actions. Verify tag firing and enhanced conversion "
                    "payload before spend ramps up."
                ),
            }
        )
    for a in actions:
        if a["primary_for_goal"] and a["data_driven_model_status"] not in {
            None,
            "AVAILABLE",
        }:
            alerts.append(
                {
                    "severity": "warning",
                    "code": "DDA_NOT_AVAILABLE",
                    "message": (
                        f"Data-driven attribution not available for '{a['name']}' "
                        f"({a['data_driven_model_status']}); Google will fall back to "
                        "the model configured at account level."
                    ),
                }
            )
        if a["primary_for_goal"] and not a["included_in_conversions_metric"]:
            alerts.append(
                {
                    "severity": "warning",
                    "code": "PRIMARY_NOT_COUNTED",
                    "message": (
                        f"'{a['name']}' is primary_for_goal but "
                        "include_in_conversions_metric is false; Smart Bidding "
                        "will not use it."
                    ),
                }
            )

    summary = {
        "total_actions": len(actions),
        "enabled_actions": sum(1 for a in actions if a["status"] == "ENABLED"),
        "primary_for_goal_actions": sum(1 for a in actions if a["primary_for_goal"]),
        "actions_with_conversions": sum(
            1 for a in actions if a["metrics"]["conversions"] > 0
        ),
        "lead_actions_present": has_lead_action,
        "customer_data_terms_accepted": tracking["accepted_customer_data_terms"] is True,
        "enhanced_conversions_for_leads_enabled": (
            tracking["enhanced_conversions_for_leads_enabled"] is True
        ),
        "window_days": days,
        "total_conversions_in_window": round(total_conversions_30d, 4),
    }

    return {
        "customer_name": customer_name,
        "tracking": tracking,
        "conversion_actions": actions,
        "alerts": alerts,
        "summary": summary,
    }


def _to_int(value: str | int | None) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_AD_STRENGTH_LABELS = ("PENDING", "LEARNING", "LOW", "GOOD", "BEST", "UNKNOWN")
_AD_STRENGTH_BUCKETS = ("POOR", "AVERAGE", "GOOD", "EXCELLENT", "UNKNOWN")


def _summarize_ad_strength(*, rows: list[dict], days: int) -> dict:
    """Turn ad_group_ad GAQL rows into an Ad Strength diagnostic report.

    Kept pure so tests exercise every alert path without touching the API.
    """
    ads: list[dict] = []
    strength_counter = {b: 0 for b in _AD_STRENGTH_BUCKETS}
    label_counter = {l: 0 for l in _AD_STRENGTH_LABELS}
    pinned_ads = 0

    for row in rows:
        ad_group_ad = row.get("adGroupAd", {}) or {}
        ad = ad_group_ad.get("ad", {}) or {}
        rsa = ad.get("responsiveSearchAd", {}) or {}
        metrics = row.get("metrics", {}) or {}
        campaign = row.get("campaign", {}) or {}
        ad_group = row.get("adGroup", {}) or {}

        strength = (ad_group_ad.get("adStrength") or "UNKNOWN").upper()
        bucket = strength if strength in _AD_STRENGTH_BUCKETS else "UNKNOWN"
        strength_counter[bucket] += 1

        headlines_report = _asset_slot_report(rsa.get("headlines") or [], label_counter)
        descriptions_report = _asset_slot_report(
            rsa.get("descriptions") or [], label_counter
        )
        if headlines_report["pinned_count"] or descriptions_report["pinned_count"]:
            pinned_ads += 1

        impressions = int(metrics.get("impressions") or 0)
        clicks = int(metrics.get("clicks") or 0)
        ctr = (clicks / impressions) if impressions else 0.0

        path1 = rsa.get("path1")
        path2 = rsa.get("path2")
        if path1 and path2:
            path_display = f"{path1}/{path2}"
        elif path1:
            path_display = path1
        elif path2:
            path_display = f"/{path2}"
        else:
            path_display = None

        ads.append(
            {
                "campaign": {"id": campaign.get("id"), "name": campaign.get("name")},
                "ad_group": {"id": ad_group.get("id"), "name": ad_group.get("name")},
                "ad_id": ad.get("id"),
                "status": ad_group_ad.get("status"),
                "type": ad.get("type"),
                "ad_strength": bucket,
                "path_display": path_display,
                "metrics": {
                    "window_days": days,
                    "impressions": impressions,
                    "clicks": clicks,
                    "conversions": float(metrics.get("conversions") or 0),
                    "ctr": round(ctr, 4),
                },
                "headlines": headlines_report,
                "descriptions": descriptions_report,
                "suggestions": _ad_suggestions(
                    bucket, headlines_report, descriptions_report, impressions
                ),
            }
        )

    recommendations = _account_ad_recommendations(
        ads, strength_counter, label_counter
    )
    return {
        "period_days": days,
        "summary": {
            "total_ads": len(ads),
            "by_strength": strength_counter,
            "assets_by_label": label_counter,
            "ads_with_pinned_assets": pinned_ads,
        },
        "ads": ads,
        "recommendations": recommendations,
    }


def _asset_slot_report(items: list[dict], label_counter: dict) -> dict:
    counts_by_label = {l: 0 for l in _AD_STRENGTH_LABELS}
    normalized_items: list[dict] = []
    pinned = 0
    approved = 0
    for item in items:
        text = item.get("text") or ""
        label = (item.get("assetPerformanceLabel") or "UNKNOWN").upper()
        if label not in counts_by_label:
            label = "UNKNOWN"
        counts_by_label[label] += 1
        label_counter[label] += 1

        pinned_field = item.get("pinnedField")
        if pinned_field:
            pinned += 1

        policy = item.get("policySummaryInfo") or {}
        is_approved = policy.get("approvalStatus") == "APPROVED"
        if is_approved:
            approved += 1

        normalized_items.append(
            {
                "text": text,
                "label": label,
                "pinned_to": pinned_field,
                "approved": is_approved,
                "approval_status": policy.get("approvalStatus"),
            }
        )
    return {
        "count": len(items),
        "pinned_count": pinned,
        "approved_count": approved,
        "counts_by_label": counts_by_label,
        "items": normalized_items,
    }


def _ad_suggestions(
    strength: str,
    headlines: dict,
    descriptions: dict,
    impressions: int,
) -> list[dict]:
    out: list[dict] = []
    hl_count = headlines["count"]
    desc_count = descriptions["count"]
    hl_low = headlines["counts_by_label"].get("LOW", 0)
    desc_low = descriptions["counts_by_label"].get("LOW", 0)
    hl_pending = headlines["counts_by_label"].get("PENDING", 0)
    desc_pending = descriptions["counts_by_label"].get("PENDING", 0)
    hl_best = headlines["counts_by_label"].get("BEST", 0)

    if hl_count < 8:
        out.append(
            {
                "severity": "high" if hl_count < 5 else "warning",
                "code": "FEW_HEADLINES",
                "message": (
                    f"Only {hl_count} headlines. Google recommends 8-15 for RSAs; more variety improves Ad Strength."
                ),
            }
        )
    if desc_count < 3:
        out.append(
            {
                "severity": "high" if desc_count < 2 else "warning",
                "code": "FEW_DESCRIPTIONS",
                "message": (
                    f"Only {desc_count} descriptions. Add at least 3-4 for stronger creative testing."
                ),
            }
        )
    if strength == "POOR":
        out.append(
            {
                "severity": "high",
                "code": "AD_STRENGTH_POOR",
                "message": (
                    "Ad Strength POOR reduces auction eligibility. Replace weakest assets and add more distinct headlines."
                ),
            }
        )
    elif strength == "AVERAGE":
        out.append(
            {
                "severity": "warning",
                "code": "AD_STRENGTH_AVERAGE",
                "message": (
                    "Ad Strength AVERAGE leaves impressions on the table. Aim for GOOD/EXCELLENT."
                ),
            }
        )
    if hl_low or desc_low:
        out.append(
            {
                "severity": "warning",
                "code": "LOW_PERFORMING_ASSETS",
                "message": (
                    f"{hl_low} headline(s) and {desc_low} description(s) labeled LOW; replace them to lift Ad Strength."
                ),
            }
        )
    if impressions == 0:
        out.append(
            {
                "severity": "info",
                "code": "NO_IMPRESSIONS_IN_WINDOW",
                "message": (
                    "Ad has no impressions in the window; asset labels stay PENDING until enough serving history accumulates."
                ),
            }
        )
    elif impressions < 200 and (hl_pending or desc_pending):
        out.append(
            {
                "severity": "info",
                "code": "INSUFFICIENT_SERVING_HISTORY",
                "message": (
                    f"Only {impressions} impressions — several assets still PENDING. Give it 1-2 weeks of serving before rotating creative."
                ),
            }
        )
    if headlines["pinned_count"] >= hl_count and hl_count > 0:
        out.append(
            {
                "severity": "warning",
                "code": "OVER_PINNED_HEADLINES",
                "message": (
                    "Every headline is pinned; Google cannot experiment with combinations and Ad Strength will stay low."
                ),
            }
        )
    if hl_best and headlines["pinned_count"] == 0:
        # positive nudge, not a warning
        out.append(
            {
                "severity": "info",
                "code": "BEST_HEADLINES_WITHOUT_PINS",
                "message": (
                    f"{hl_best} headline(s) labeled BEST but none pinned. Consider pinning at least one BEST asset to position 1 to lock in brand voice."
                ),
            }
        )
    return out


def _account_ad_recommendations(
    ads: list[dict], strength_counter: dict, label_counter: dict
) -> list[dict]:
    total = len(ads)
    if total == 0:
        return [
            {
                "severity": "info",
                "code": "NO_ADS_MATCHED_FILTER",
                "message": "No RSAs matched the filter. Widen the window or drop min_impressions.",
            }
        ]
    out: list[dict] = []
    poor = strength_counter.get("POOR", 0)
    average = strength_counter.get("AVERAGE", 0)
    if poor:
        out.append(
            {
                "severity": "high",
                "code": "POOR_STRENGTH_SHARE",
                "message": f"{poor}/{total} RSAs are POOR. Prioritize rewriting those first.",
            }
        )
    if average >= total / 2 and average > 0:
        out.append(
            {
                "severity": "warning",
                "code": "MAJORITY_AVERAGE",
                "message": f"{average}/{total} RSAs sit at AVERAGE strength — creative-lift sprint has high ROI here.",
            }
        )
    low_assets = label_counter.get("LOW", 0)
    if low_assets:
        out.append(
            {
                "severity": "warning",
                "code": "LOW_ASSETS_ACROSS_ACCOUNT",
                "message": (
                    f"{low_assets} LOW-labeled assets across the account; consolidate a replacement batch."
                ),
            }
        )
    pending = label_counter.get("PENDING", 0)
    scored = sum(label_counter.get(l, 0) for l in ("LOW", "GOOD", "BEST"))
    if pending and scored == 0:
        out.append(
            {
                "severity": "info",
                "code": "ALL_PENDING",
                "message": (
                    f"All {pending} assets are still PENDING; the account has no serving history to grade against."
                ),
            }
        )
    return out


def register_read_tools(mcp) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_search_terms_report(
        customer_id: str = Field(description="Google Ads customer ID"),
        campaign_id: Optional[str] = Field(default=None, description="Optional campaign ID"),
        days: int = Field(default=30, description="Lookback period from 1 to 365 days"),
        min_impressions: int = Field(default=1, ge=0),
    ) -> str:
        """Return real search terms with cost, clicks and conversions."""
        campaign = _id(campaign_id, "campaign_id")
        filters = [_date_clause(days), f"metrics.impressions >= {int(min_impressions)}"]
        if campaign:
            filters.append(f"campaign.id = {campaign}")
        query = f"""
            SELECT search_term_view.search_term, search_term_view.status,
                   campaign.id, campaign.name, ad_group.id, ad_group.name,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM search_term_view
            WHERE {' AND '.join(filters)}
            ORDER BY metrics.cost_micros DESC
        """
        return _run("get_search_terms_report", customer_id, query)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_keyword_performance(
        customer_id: str,
        campaign_id: Optional[str] = None,
        ad_group_id: Optional[str] = None,
        days: int = 30,
    ) -> str:
        """Return keyword metrics, match type, Quality Score and impression share."""
        filters = [
            _date_clause(days),
            "ad_group_criterion.type = KEYWORD",
            "ad_group_criterion.status != REMOVED",
        ]
        if campaign_id:
            filters.append(f"campaign.id = {_id(campaign_id, 'campaign_id')}")
        if ad_group_id:
            filters.append(f"ad_group.id = {_id(ad_group_id, 'ad_group_id')}")
        query = f"""
            SELECT campaign.id, campaign.name, ad_group.id, ad_group.name,
                   ad_group_criterion.criterion_id,
                   ad_group_criterion.keyword.text,
                   ad_group_criterion.keyword.match_type,
                   ad_group_criterion.quality_info.quality_score,
                   ad_group_criterion.effective_cpc_bid_micros,
                   metrics.impressions, metrics.clicks, metrics.ctr,
                   metrics.average_cpc, metrics.cost_micros, metrics.conversions,
                   metrics.search_impression_share,
                   metrics.search_top_impression_share,
                   metrics.search_absolute_top_impression_share
            FROM keyword_view
            WHERE {' AND '.join(filters)}
            ORDER BY metrics.cost_micros DESC
        """
        return _run("get_keyword_performance", customer_id, query)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_geo_performance(
        customer_id: str,
        days: int = 30,
        geo_level: str = Field(default="country", description="country, region or city"),
    ) -> str:
        """Return performance segmented by the user's geographic location."""
        level = geo_level.strip().lower()
        fields = {
            "country": "user_location_view.country_criterion_id",
            "region": "user_location_view.region_criterion_id",
            "city": "user_location_view.city_criterion_id",
        }
        if level not in fields:
            raise ValueError("geo_level must be country, region or city")
        query = f"""
            SELECT {fields[level]}, user_location_view.targeting_location,
                   campaign.id, campaign.name,
                   metrics.impressions, metrics.clicks, metrics.ctr,
                   metrics.cost_micros, metrics.conversions,
                   metrics.conversions_value
            FROM user_location_view
            WHERE {_date_clause(days)}
            ORDER BY metrics.cost_micros DESC
        """
        return _run("get_geo_performance", customer_id, query)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_device_performance(
        customer_id: str,
        days: int = 30,
        campaign_id: Optional[str] = None,
    ) -> str:
        """Return performance grouped by mobile, desktop, tablet and other devices."""
        filters = [_date_clause(days)]
        if campaign_id:
            filters.append(f"campaign.id = {_id(campaign_id, 'campaign_id')}")
        query = f"""
            SELECT segments.device, campaign.id, campaign.name,
                   metrics.impressions, metrics.clicks, metrics.ctr,
                   metrics.average_cpc, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM campaign
            WHERE {' AND '.join(filters)}
            ORDER BY metrics.cost_micros DESC
        """
        return _run("get_device_performance", customer_id, query)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_time_of_day_performance(customer_id: str, days: int = 30) -> str:
        """Return performance by weekday and hour."""
        query = f"""
            SELECT segments.day_of_week, segments.hour,
                   metrics.impressions, metrics.clicks, metrics.ctr,
                   metrics.cost_micros, metrics.conversions,
                   metrics.conversions_value
            FROM customer
            WHERE {_date_clause(days)}
            ORDER BY segments.day_of_week, segments.hour
        """
        return _run("get_time_of_day_performance", customer_id, query)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_audience_performance(customer_id: str, days: int = 30) -> str:
        """Return performance for configured audience criteria."""
        query = f"""
            SELECT campaign.id, campaign.name, ad_group.id, ad_group.name,
                   ad_group_criterion.criterion_id,
                   ad_group_criterion.type, ad_group_criterion.status,
                   ad_group_criterion.user_list.user_list,
                   ad_group_criterion.user_interest.user_interest_category,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM ad_group_audience_view
            WHERE {_date_clause(days)}
            ORDER BY metrics.cost_micros DESC
        """
        return _run("get_audience_performance", customer_id, query)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_conversion_actions(customer_id: str) -> str:
        """List conversion actions with category, defaults, counting and status."""
        query = """
            SELECT conversion_action.id, conversion_action.name,
                   conversion_action.category, conversion_action.type,
                   conversion_action.status,
                   conversion_action.value_settings.default_value,
                   conversion_action.value_settings.default_currency_code,
                   conversion_action.counting_type,
                   conversion_action.click_through_lookback_window_days,
                   conversion_action.view_through_lookback_window_days
            FROM conversion_action
            WHERE conversion_action.status != REMOVED
            ORDER BY conversion_action.name
        """
        return _run("get_conversion_actions", customer_id, query)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_ad_strength_diagnostics(
        customer_id: str = Field(description="Google Ads customer ID"),
        campaign_id: Optional[str] = Field(
            default=None, description="Optional campaign filter"
        ),
        days: int = Field(default=30, description="Lookback window 1-365"),
        min_impressions: int = Field(
            default=0,
            description="Filter out ads with fewer than N impressions in the window",
            ge=0,
        ),
    ) -> str:
        """Return Ad Strength per RSA plus per-asset performance labels and actionable suggestions."""
        ensure_tool_allowed("get_ad_strength_diagnostics")
        cid = ensure_customer_allowed(customer_id)
        filters = [
            _date_clause(days),
            "ad_group_ad.status != 'REMOVED'",
            "ad_group_ad.ad.type = RESPONSIVE_SEARCH_AD",
            f"metrics.impressions >= {int(min_impressions)}",
        ]
        if campaign_id:
            filters.append(f"campaign.id = {_id(campaign_id, 'campaign_id')}")
        query = f"""
            SELECT
              campaign.id, campaign.name,
              ad_group.id, ad_group.name,
              ad_group_ad.ad.id, ad_group_ad.ad.type,
              ad_group_ad.status, ad_group_ad.ad_strength,
              ad_group_ad.ad.responsive_search_ad.headlines,
              ad_group_ad.ad.responsive_search_ad.descriptions,
              ad_group_ad.ad.responsive_search_ad.path1,
              ad_group_ad.ad.responsive_search_ad.path2,
              metrics.impressions, metrics.clicks, metrics.conversions
            FROM ad_group_ad
            WHERE {' AND '.join(filters)}
            ORDER BY metrics.impressions DESC
        """
        rows = search(cid, query)
        report = _summarize_ad_strength(rows=rows, days=days)
        return format_json({
            "customer_id": cid,
            "tool": "get_ad_strength_diagnostics",
            "fresh": True,
            "filter": {
                "campaign_id": campaign_id,
                "min_impressions": int(min_impressions),
                "days": days,
            },
            **report,
        })

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_enhanced_conversions_status(
        customer_id: str = Field(description="Google Ads customer ID"),
        days: int = Field(default=30, description="Lookback window 1-365"),
    ) -> str:
        """Audit Enhanced Conversions readiness: tracking setup, per-action config, 30d volume, alerts."""
        ensure_tool_allowed("get_enhanced_conversions_status")
        cid = ensure_customer_allowed(customer_id)

        customer_query = """
            SELECT customer.id, customer.descriptive_name,
                   customer.conversion_tracking_setting.conversion_tracking_id,
                   customer.conversion_tracking_setting.conversion_tracking_status,
                   customer.conversion_tracking_setting.google_ads_conversion_customer,
                   customer.conversion_tracking_setting.accepted_customer_data_terms,
                   customer.conversion_tracking_setting.enhanced_conversions_for_leads_enabled
            FROM customer
        """
        actions_query = """
            SELECT conversion_action.id, conversion_action.name,
                   conversion_action.status, conversion_action.type,
                   conversion_action.category, conversion_action.origin,
                   conversion_action.primary_for_goal,
                   conversion_action.include_in_conversions_metric,
                   conversion_action.counting_type,
                   conversion_action.attribution_model_settings.attribution_model,
                   conversion_action.attribution_model_settings.data_driven_model_status,
                   conversion_action.click_through_lookback_window_days,
                   conversion_action.view_through_lookback_window_days
            FROM conversion_action
            WHERE conversion_action.status != 'REMOVED'
            ORDER BY conversion_action.name
        """
        metrics_query = f"""
            SELECT segments.conversion_action, segments.conversion_action_name,
                   segments.conversion_action_category,
                   metrics.all_conversions, metrics.conversions, metrics.conversions_value
            FROM campaign
            WHERE {_date_clause(days)}
        """

        customer_rows = search(cid, customer_query)
        action_rows = search(cid, actions_query)
        metric_rows = search(cid, metrics_query)

        report = _summarize_enhanced_conversions(
            customer_id=cid,
            customer_rows=customer_rows,
            action_rows=action_rows,
            metric_rows=metric_rows,
            days=days,
        )
        return format_json({
            "customer_id": cid,
            "tool": "get_enhanced_conversions_status",
            "fresh": True,
            **report,
        })

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_conversion_performance(
        customer_id: str,
        conversion_action_id: Optional[str] = None,
        days: int = 30,
    ) -> str:
        """Return conversions by action, campaign, device and date."""
        filters = [_date_clause(days)]
        if conversion_action_id:
            filters.append(
                f"segments.conversion_action = "
                f"'customers/{ensure_customer_allowed(customer_id)}/conversionActions/"
                f"{_id(conversion_action_id, 'conversion_action_id')}'"
            )
        query = f"""
            SELECT segments.date, segments.device, segments.conversion_action,
                   segments.conversion_action_name,
                   campaign.id, campaign.name,
                   metrics.all_conversions, metrics.all_conversions_value,
                   metrics.conversions, metrics.conversions_value,
                   metrics.cost_per_conversion
            FROM campaign
            WHERE {' AND '.join(filters)}
            ORDER BY segments.date DESC, metrics.conversions DESC
        """
        return _run("get_conversion_performance", customer_id, query)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_change_history(
        customer_id: str,
        days: int = 14,
        resource_type: Optional[str] = None,
    ) -> str:
        """Return account changes, actor, timestamp and changed fields."""
        if not 1 <= days <= 30:
            raise ValueError("Google Ads change history supports 1 to 30 days")
        filters = [_date_clause(days, "change_event.change_date_time")]
        if resource_type:
            clean_type = resource_type.strip().upper()
            if not clean_type.replace("_", "").isalnum():
                raise ValueError("resource_type contains invalid characters")
            filters.append(f"change_event.change_resource_type = {clean_type}")
        query = f"""
            SELECT change_event.change_date_time, change_event.user_email,
                   change_event.client_type, change_event.change_resource_type,
                   change_event.change_resource_name,
                   change_event.resource_change_operation,
                   change_event.changed_fields,
                   change_event.old_resource, change_event.new_resource
            FROM change_event
            WHERE {' AND '.join(filters)}
            ORDER BY change_event.change_date_time DESC
            LIMIT 10000
        """
        return _run("get_change_history", customer_id, query)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_bidding_strategies(customer_id: str) -> str:
        """List portfolio and campaign bidding strategies."""
        query = """
            SELECT bidding_strategy.id, bidding_strategy.name,
                   bidding_strategy.type, bidding_strategy.status,
                   bidding_strategy.campaign_count,
                   bidding_strategy.target_cpa.target_cpa_micros,
                   bidding_strategy.target_roas.target_roas,
                   bidding_strategy.effective_currency_code
            FROM bidding_strategy
            ORDER BY bidding_strategy.name
        """
        return _run("get_bidding_strategies", customer_id, query)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_budget_utilization(customer_id: str, days: int = 30) -> str:
        """Return configured budget, spend, utilization and budget-limited status."""
        query = f"""
            SELECT campaign_budget.id, campaign_budget.name,
                   campaign_budget.amount_micros,
                   campaign_budget.total_amount_micros,
                   campaign_budget.status,
                   campaign.id, campaign.name, campaign.status,
                   campaign.primary_status,
                   campaign.primary_status_reasons,
                   metrics.cost_micros
            FROM campaign
            WHERE {_date_clause(days)}
              AND campaign.status != REMOVED
            ORDER BY metrics.cost_micros DESC
        """
        return _run("get_budget_utilization", customer_id, query)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_shared_sets(customer_id: str) -> str:
        """List shared negative keyword and placement sets."""
        query = """
            SELECT shared_set.id, shared_set.name, shared_set.type,
                   shared_set.status, shared_set.member_count,
                   shared_set.reference_count
            FROM shared_set
            WHERE shared_set.status != REMOVED
            ORDER BY shared_set.name
        """
        return _run("get_shared_sets", customer_id, query)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def list_recommendations(
        customer_id: str,
        type: Optional[str] = Field(
            default=None,
            description="Optional Google Ads recommendation type",
        ),
    ) -> str:
        """List active Google Ads recommendations and estimated impact."""
        filters: list[str] = []
        if type:
            clean_type = type.strip().upper()
            if not clean_type.replace("_", "").isalnum():
                raise ValueError("recommendation_type contains invalid characters")
            filters.append(f"recommendation.type = {clean_type}")
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        query = f"""
            SELECT recommendation.resource_name, recommendation.type,
                   recommendation.dismissed,
                   recommendation.impact.base_metrics,
                   recommendation.impact.potential_metrics
            FROM recommendation
            {where}
        """
        return _run("list_recommendations", customer_id, query)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_recommendation_detail(customer_id: str, recommendation_id: str) -> str:
        """Return the complete resource for one Google Ads recommendation."""
        clean_id = recommendation_id.strip()
        if not clean_id or "'" in clean_id or "\\" in clean_id:
            raise ValueError("Invalid recommendation_id")
        resource = (
            clean_id
            if clean_id.startswith("customers/")
            else f"customers/{ensure_customer_allowed(customer_id)}/recommendations/{clean_id}"
        )
        query = f"""
            SELECT recommendation.resource_name, recommendation.type,
                   recommendation.dismissed,
                   recommendation.impact.base_metrics,
                   recommendation.impact.potential_metrics
            FROM recommendation
            WHERE recommendation.resource_name = '{resource}'
        """
        return _run("get_recommendation_detail", customer_id, query)
