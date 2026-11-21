"""Complementary read-only Google Ads tools."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from .api import format_json, search, search_with_meta
from .config import ensure_customer_allowed, ensure_tool_allowed
from .errors import GoogleAdsError
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
    meta = search_with_meta(customer_id, query)
    payload = {
        "customer_id": customer_id,
        "tool": tool_name,
        "fresh": True,
        "returned_rows": meta["returned_rows"],
        "truncated": meta["truncated"],
        "results": meta["rows"],
    }
    if meta["truncated"]:
        payload["max_rows"] = meta["max_rows"]
        payload["truncation_note"] = (
            f"Results capped at {meta['max_rows']} rows to protect LLM context. "
            "Add narrower filters or a shorter date window for complete rows."
        )
    return format_json(payload)


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


def _summarize_impression_share(*, rows: list[dict], level: str, days: int) -> dict:
    """Turn campaign/ad_group rows into an IS-losses report with a diagnosis label.

    Pure function so unit tests can exercise every diagnosis without a live API.
    Google returns share fields in [0, 1]; missing fields mean the metric is
    not eligible (usually because impressions were below the reporting threshold).
    """
    units: list[dict] = []
    counter = {
        "healthy": 0,
        "budget_limited": 0,
        "rank_limited": 0,
        "mixed": 0,
        "not_serving": 0,
        "insufficient_data": 0,
    }
    for row in rows:
        metrics = row.get("metrics", {}) or {}
        campaign = row.get("campaign", {}) or {}
        ad_group = row.get("adGroup", {}) or {}

        impressions = int(metrics.get("impressions") or 0)
        clicks = int(metrics.get("clicks") or 0)
        cost_micros = int(metrics.get("costMicros") or 0)

        is_impr = _to_share(metrics.get("searchImpressionShare"))
        is_budget = _to_share(metrics.get("searchBudgetLostImpressionShare"))
        is_rank = _to_share(metrics.get("searchRankLostImpressionShare"))
        is_top = _to_share(metrics.get("searchTopImpressionShare"))
        is_budget_top = _to_share(metrics.get("searchBudgetLostTopImpressionShare"))
        is_rank_top = _to_share(metrics.get("searchRankLostTopImpressionShare"))
        is_abs_top = _to_share(metrics.get("searchAbsoluteTopImpressionShare"))
        is_budget_abs = _to_share(metrics.get("searchBudgetLostAbsoluteTopImpressionShare"))
        is_rank_abs = _to_share(metrics.get("searchRankLostAbsoluteTopImpressionShare"))

        diagnosis, message = _diagnose_impression_share(
            impressions=impressions,
            is_impr=is_impr,
            is_budget=is_budget,
            is_rank=is_rank,
            level=level,
        )
        counter[diagnosis] += 1

        unit = {
            "campaign": {"id": campaign.get("id"), "name": campaign.get("name")},
            "metrics": {
                "window_days": days,
                "impressions": impressions,
                "clicks": clicks,
                "cost_micros": cost_micros,
            },
            "impression_share": {
                "current": is_impr,
                "top": is_top,
                "absolute_top": is_abs_top,
            },
            "losses": {
                "budget": {
                    "any": is_budget,
                    "top": is_budget_top,
                    "absolute_top": is_budget_abs,
                },
                "rank": {
                    "any": is_rank,
                    "top": is_rank_top,
                    "absolute_top": is_rank_abs,
                },
            },
            "diagnosis": {"code": diagnosis, "message": message},
        }
        if level == "ad_group":
            unit["ad_group"] = {"id": ad_group.get("id"), "name": ad_group.get("name")}
            unit["status"] = ad_group.get("status")
        else:
            unit["status"] = row.get("campaign", {}).get("status")
            unit["channel_type"] = row.get("campaign", {}).get("advertisingChannelType")
        units.append(unit)

    recommendations = _is_account_recommendations(counter, len(units), level)
    return {
        "period_days": days,
        "level": level,
        "summary": {
            "total_units": len(units),
            "diagnosis_counts": counter,
        },
        "units": units,
        "recommendations": recommendations,
    }


def _to_share(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f, 4)


def _diagnose_impression_share(
    *,
    impressions: int,
    is_impr: float | None,
    is_budget: float | None,
    is_rank: float | None,
    level: str,
) -> tuple[str, str]:
    if impressions == 0 and (is_impr is None or is_impr == 0):
        if is_rank is not None and is_rank > 0:
            # Rank-lost even without impressions means eligible auctions but always outranked
            return (
                "rank_limited",
                "Eligible for auctions but always outranked; not serving because bids or "
                "Ad Rank are too low.",
            )
        return (
            "not_serving",
            "No impressions and no reported eligibility in the window.",
        )
    if is_impr is None:
        return (
            "insufficient_data",
            "Google did not report Impression Share for this unit; usually means volume was too low.",
        )
    if is_impr >= 0.9:
        return (
            "healthy",
            f"Impression Share {round(is_impr * 100, 1)}% — very little upside to unlock.",
        )
    budget = is_budget if is_budget is not None else 0.0
    rank = is_rank if is_rank is not None else 0.0
    if level == "ad_group" and is_budget is None:
        # Budget IS is not available at ad_group level in v25.
        if rank > 0.2:
            return (
                "rank_limited",
                f"Losing {round(rank * 100, 1)}% of impressions to Ad Rank at ad_group level.",
            )
        return (
            "insufficient_data",
            "Budget-lost IS is only available at campaign level; check the parent campaign for the budget-vs-rank split.",
        )
    if budget >= 0.2 and rank >= 0.2 and abs(budget - rank) < 0.1:
        return (
            "mixed",
            f"Losing {round(budget * 100, 1)}% to budget and {round(rank * 100, 1)}% to rank — both need attention.",
        )
    if budget > rank and budget >= 0.1:
        return (
            "budget_limited",
            f"Losing {round(budget * 100, 1)}% of impressions to budget vs {round(rank * 100, 1)}% to rank.",
        )
    if rank > budget and rank >= 0.1:
        return (
            "rank_limited",
            f"Losing {round(rank * 100, 1)}% of impressions to Ad Rank vs {round(budget * 100, 1)}% to budget.",
        )
    return (
        "healthy",
        f"Impression Share {round(is_impr * 100, 1)}% with balanced small losses (budget {round(budget * 100, 1)}%, rank {round(rank * 100, 1)}%).",
    )


def _is_account_recommendations(counter: dict, total: int, level: str) -> list[dict]:
    if total == 0:
        return [
            {
                "severity": "info",
                "code": "NO_UNITS",
                "message": "No campaigns or ad groups matched the filter.",
            }
        ]
    out: list[dict] = []
    if counter["budget_limited"]:
        out.append(
            {
                "severity": "high",
                "code": "BUDGET_LIMITED_PRESENT",
                "message": (
                    f"{counter['budget_limited']}/{total} units are budget-limited. Raise budgets or reallocate before touching bids."
                ),
            }
        )
    if counter["rank_limited"]:
        out.append(
            {
                "severity": "high",
                "code": "RANK_LIMITED_PRESENT",
                "message": (
                    f"{counter['rank_limited']}/{total} units are rank-limited. Address Quality Score, bids or creative before adding budget."
                ),
            }
        )
    if counter["mixed"]:
        out.append(
            {
                "severity": "warning",
                "code": "MIXED_LIMITED",
                "message": (
                    f"{counter['mixed']}/{total} units lose comparable share to both budget and rank; requires both fixes."
                ),
            }
        )
    if counter["not_serving"]:
        out.append(
            {
                "severity": "warning",
                "code": "NOT_SERVING_UNITS",
                "message": (
                    f"{counter['not_serving']}/{total} units did not serve in the window; verify they are eligible."
                ),
            }
        )
    if counter["healthy"] == total:
        out.append(
            {
                "severity": "info",
                "code": "ALL_HEALTHY",
                "message": "All units have IS ≥ 90% or balanced small losses.",
            }
        )
    return out


def _summarize_auction_insights(
    *,
    rows: list[dict],
    level: str,
    days: int,
    campaign_id: str | None,
) -> dict:
    """Group auction-insight rows into per-unit competitor tables + head-to-head context.

    Auction Insights come one row per (unit × competitor domain × date-segment). We
    aggregate by (unit_key, domain) so callers get one line per competitor with
    average impression share, overlap, outranking, etc. Pure function so unit
    tests exercise every path.
    """
    units: dict[str, dict] = {}
    metric_paths = (
        ("impression_share", "auctionInsightSearchImpressionShare"),
        ("absolute_top_impression_pct", "auctionInsightSearchAbsoluteTopImpressionPercentage"),
        ("top_impression_pct", "auctionInsightSearchTopImpressionPercentage"),
        ("outranking_share", "auctionInsightSearchOutrankingShare"),
        ("overlap_rate", "auctionInsightSearchOverlapRate"),
        ("position_above_rate", "auctionInsightSearchPositionAboveRate"),
    )

    for row in rows:
        campaign = row.get("campaign", {}) or {}
        ad_group = row.get("adGroup", {}) or {}
        segments = row.get("segments", {}) or {}
        metrics = row.get("metrics", {}) or {}
        domain = segments.get("auctionInsightDomain")
        if not domain:
            continue

        if level == "ad_group":
            unit_id = ad_group.get("id")
            unit_name = ad_group.get("name")
            if not unit_id:
                continue
            key = f"ad_group:{unit_id}"
        else:
            unit_id = campaign.get("id")
            unit_name = campaign.get("name")
            if not unit_id:
                continue
            key = f"campaign:{unit_id}"

        unit = units.setdefault(
            key,
            {
                "campaign": {"id": campaign.get("id"), "name": campaign.get("name")},
                "competitors": {},
                "status": (
                    ad_group.get("status") if level == "ad_group" else campaign.get("status")
                ),
            },
        )
        if level == "ad_group":
            unit["ad_group"] = {"id": ad_group.get("id"), "name": ad_group.get("name")}

        comp = unit["competitors"].setdefault(
            domain,
            {
                "domain": domain,
                "samples": 0,
                "_totals": {k: 0.0 for k, _ in metric_paths},
            },
        )
        comp["samples"] += 1
        for out_key, api_key in metric_paths:
            value = metrics.get(api_key)
            if value is None:
                continue
            try:
                comp["_totals"][out_key] += float(value)
            except (TypeError, ValueError):
                continue

    # Flatten + rank competitors within each unit
    unit_list: list[dict] = []
    for unit in units.values():
        competitors: list[dict] = []
        for domain, comp in unit["competitors"].items():
            samples = comp["samples"] or 1
            averages = {
                key: round(comp["_totals"][key] / samples, 4)
                for key, _ in metric_paths
            }
            competitors.append(
                {
                    "domain": domain,
                    "samples": comp["samples"],
                    "metrics": averages,
                }
            )
        # Rank by average impression share descending, "You" (self, i.e. the domain
        # labelled 'You' or matching customer descriptive name) usually appears first
        # via the SORT — Google returns your own row with `auction_insight_domain = "You"`.
        competitors.sort(
            key=lambda c: c["metrics"].get("impression_share") or 0.0, reverse=True
        )
        head_to_head = _auction_head_to_head(competitors)
        unit_list.append(
            {
                "campaign": unit["campaign"],
                **({"ad_group": unit["ad_group"]} if "ad_group" in unit else {}),
                "status": unit["status"],
                "competitor_count": len(competitors),
                "competitors": competitors,
                "head_to_head": head_to_head,
            }
        )

    unit_list.sort(
        key=lambda u: (
            u["campaign"].get("name") or "",
            (u.get("ad_group") or {}).get("name") or "",
        )
    )

    if not unit_list:
        return {
            "period_days": days,
            "level": level,
            "summary": {"units": 0, "unique_competitors": 0},
            "units": [],
            "recommendations": [
                {
                    "severity": "info",
                    "code": "NO_AUCTION_INSIGHT_DATA",
                    "message": (
                        "No auction-insight rows returned. Common causes: not enough "
                        "impressions in the window, or the filter matched no units."
                    ),
                }
            ],
        }

    unique_domains = {
        c["domain"] for u in unit_list for c in u["competitors"] if c["domain"] != "You"
    }
    recommendations: list[dict] = []
    for unit in unit_list:
        # Threat = any competitor whose outranking_share against us is >= 50%
        threats = [
            c
            for c in unit["competitors"]
            if c["domain"] != "You" and (c["metrics"].get("outranking_share") or 0) >= 0.5
        ]
        if threats:
            names = ", ".join(t["domain"] for t in threats[:3])
            unit_label = unit["campaign"].get("name")
            if unit.get("ad_group"):
                unit_label += f" > {unit['ad_group'].get('name')}"
            recommendations.append(
                {
                    "severity": "high",
                    "code": "OUTRANKED_BY_COMPETITOR",
                    "unit": unit_label,
                    "message": (
                        f"{len(threats)} competitor(s) outrank us in {unit_label}: {names}."
                    ),
                }
            )

    return {
        "period_days": days,
        "level": level,
        "filter": {"campaign_id": campaign_id},
        "summary": {
            "units": len(unit_list),
            "unique_competitors": len(unique_domains),
        },
        "units": unit_list,
        "recommendations": recommendations,
    }


def _auction_head_to_head(competitors: list[dict]) -> dict:
    """Extract the row for 'You' (self) so callers can compare quickly."""
    self_row = next((c for c in competitors if c["domain"] == "You"), None)
    if not self_row:
        return {"you": None, "top_competitor": None}
    top_competitor = next(
        (c for c in competitors if c["domain"] != "You"),
        None,
    )
    return {
        "you": self_row["metrics"],
        "top_competitor": (
            {
                "domain": top_competitor["domain"],
                "metrics": top_competitor["metrics"],
            }
            if top_competitor
            else None
        ),
    }


def _summarize_consent_coverage(
    *,
    customer_id: str,
    current_rows: list[dict],
    previous_rows: list[dict],
    settings_rows: list[dict],
    days: int,
    current_period: tuple[str, str],
    previous_period: tuple[str, str],
) -> dict:
    """Compare primary (`conversions`) vs total (`all_conversions`) per campaign plus tracking settings.

    Kept pure so all alert paths are exercised without hitting Google Ads.
    The Google Ads API does NOT expose per-event Consent Mode signals. The ratio
    `conversions / all_conversions` reflects measurement scope (primary biddable
    actions vs. all recorded actions, including secondary micro-conversions) and
    must NOT be interpreted as proof of Consent Mode failure.
    """
    def _bucket(rows: list[dict]) -> dict[str, dict]:
        buckets: dict[str, dict] = {}
        for row in rows:
            campaign = row.get("campaign", {}) or {}
            metrics = row.get("metrics", {}) or {}
            cid_ = campaign.get("id")
            if not cid_:
                continue
            attributed = float(metrics.get("conversions") or 0)
            total = float(metrics.get("allConversions") or 0)
            attributed_value = float(metrics.get("conversionsValue") or 0)
            total_value = float(metrics.get("allConversionsValue") or 0)
            buckets[cid_] = {
                "name": campaign.get("name"),
                "status": campaign.get("status"),
                "attributed_conversions": attributed,
                "total_conversions": total,
                "attribution_rate": (
                    round(attributed / total, 4) if total > 0 else None
                ),
                "attributed_value": attributed_value,
                "total_value": total_value,
            }
        return buckets

    current = _bucket(current_rows)
    previous = _bucket(previous_rows)

    # ----- customer tracking snapshot -----
    tracking = {
        "accepted_customer_data_terms": None,
        "enhanced_conversions_for_leads_enabled": None,
        "conversion_tracking_status": None,
    }
    customer_name = None
    if settings_rows:
        c = settings_rows[0].get("customer", {}) or {}
        customer_name = c.get("descriptiveName")
        setting = c.get("conversionTrackingSetting", {}) or {}
        tracking["accepted_customer_data_terms"] = setting.get(
            "acceptedCustomerDataTerms"
        )
        tracking["enhanced_conversions_for_leads_enabled"] = setting.get(
            "enhancedConversionsForLeadsEnabled"
        )
        tracking["conversion_tracking_status"] = setting.get(
            "conversionTrackingStatus"
        )

    # ----- per-campaign comparison + alerts -----
    campaigns: list[dict] = []
    alerts: list[dict] = []
    for cid_, cur in current.items():
        prev = previous.get(cid_)
        prev_rate = prev["attribution_rate"] if prev else None
        cur_rate = cur["attribution_rate"]
        rate_delta_pp = None
        if cur_rate is not None and prev_rate is not None:
            rate_delta_pp = round((cur_rate - prev_rate) * 100, 2)

        entry = {
            "campaign_id": cid_,
            "campaign_name": cur["name"],
            "status": cur["status"],
            "current": {
                "attributed_conversions": cur["attributed_conversions"],
                "total_conversions": cur["total_conversions"],
                "attribution_rate": cur_rate,
            },
            "previous": (
                {
                    "attributed_conversions": prev["attributed_conversions"],
                    "total_conversions": prev["total_conversions"],
                    "attribution_rate": prev_rate,
                }
                if prev
                else None
            ),
            "attribution_rate_delta_pp": rate_delta_pp,
        }
        campaigns.append(entry)

        # Per-campaign alerts (measurement-scope diagnostics, not consent proof)
        if cur["total_conversions"] > 0 and cur["attributed_conversions"] == 0:
            alerts.append(
                {
                    "severity": "warning",
                    "code": "ZERO_PRIMARY_WITH_ALL_CONVERSIONS",
                    "campaign_id": cid_,
                    "message": (
                        f"'{cur['name']}' recorded {int(cur['total_conversions'])} all_conversions "
                        "but 0 primary conversions. Check whether only secondary actions are firing "
                        "or whether campaign conversion goals or tag attribution changed."
                    ),
                }
            )
        elif cur_rate is not None and cur_rate < 0.3 and cur["total_conversions"] >= 5:
            alerts.append(
                {
                    "severity": "info",
                    "code": "LOW_PRIMARY_CONVERSION_SHARE",
                    "campaign_id": cid_,
                    "message": (
                        f"'{cur['name']}' primary conversions represent {round(cur_rate * 100, 1)}% "
                        "of all_conversions (<30%). This is a measurement difference that commonly "
                        "reflects secondary conversion actions, not proof of Consent Mode leakage; "
                        "review primary vs. secondary actions before assuming a tag issue."
                    ),
                }
            )
        if rate_delta_pp is not None and rate_delta_pp <= -20:
            alerts.append(
                {
                    "severity": "warning",
                    "code": "PRIMARY_SHARE_DROP",
                    "campaign_id": cid_,
                    "message": (
                        f"'{cur['name']}' conversions / all_conversions ratio fell {abs(rate_delta_pp):.1f}pp "
                        "vs the previous window. Review recent changes in primary/secondary conversion "
                        "actions, campaign goals, tags, or Consent Mode."
                    ),
                }
            )

    # Customer-level alerts
    if tracking["accepted_customer_data_terms"] is not True:
        alerts.append(
            {
                "severity": "warning",
                "code": "CUSTOMER_DATA_TERMS_NOT_ACCEPTED",
                "message": (
                    "Customer Data Terms not accepted. Enhanced Conversions cannot ingest "
                    "user data until accepted at Admin > Data manager."
                ),
            }
        )
    if tracking["conversion_tracking_status"] not in {
        "CONVERSION_TRACKING_MANAGED_BY_SELF",
        "CONVERSION_TRACKING_MANAGED_BY_GOOGLE_ANALYTICS_GA_4",
        "CONVERSION_TRACKING_MANAGED_BY_GOOGLE_ANALYTICS_UA",
        "CONVERSION_TRACKING_MANAGED_BY_THIRD_PARTY_APP",
    } and tracking["conversion_tracking_status"] is not None:
        alerts.append(
            {
                "severity": "info",
                "code": "UNEXPECTED_TRACKING_STATUS",
                "message": (
                    f"conversion_tracking_status='{tracking['conversion_tracking_status']}' "
                    "is unexpected. Verify tracking ownership."
                ),
            }
        )

    total_current = sum(c["current"]["total_conversions"] for c in campaigns)
    total_previous = sum(
        c["previous"]["total_conversions"] if c["previous"] else 0.0 for c in campaigns
    )
    total_attributed_cur = sum(
        c["current"]["attributed_conversions"] for c in campaigns
    )
    account_rate = (
        round(total_attributed_cur / total_current, 4) if total_current > 0 else None
    )
    if account_rate is None:
        alerts.append(
            {
                "severity": "info",
                "code": "NO_DATA_TO_ASSESS",
                "message": (
                    "No conversions recorded in either window; cannot compare primary conversions "
                    "against all_conversions yet."
                ),
            }
        )

    return {
        "customer_name": customer_name,
        "period_days": days,
        "current_period": {"start": current_period[0], "end": current_period[1]},
        "previous_period": {"start": previous_period[0], "end": previous_period[1]},
        "tracking": tracking,
        "campaigns": campaigns,
        "alerts": alerts,
        "summary": {
            "campaigns_evaluated": len(campaigns),
            "total_conversions_current": total_current,
            "total_conversions_previous": total_previous,
            "account_attribution_rate": account_rate,
            "account_primary_to_all_ratio": account_rate,
        },
        "notes": [
            "The Google Ads API does not expose per-event ad_user_data / ad_personalization signals.",
            "The ratio conversions / all_conversions compares primary (biddable) conversions "
            "against all recorded conversions (including secondary actions). Treat it as a "
            "measurement difference, not as proof of Consent Mode status or tag leakage.",
        ],
    }


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
                   ad_group_criterion.quality_info.creative_quality_score,
                   ad_group_criterion.quality_info.post_click_quality_score,
                   ad_group_criterion.quality_info.search_predicted_ctr,
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
    def get_auction_insights(
        customer_id: str = Field(description="Google Ads customer ID"),
        days: int = Field(default=30, description="Lookback window 1-365"),
        level: str = Field(
            default="campaign",
            description="Aggregation level: 'campaign' (default) or 'ad_group'.",
        ),
        campaign_id: Optional[str] = Field(
            default=None,
            description="Optional campaign filter. Required if level='ad_group'.",
        ),
    ) -> str:
        """Return Auction Insights per competitor domain, per campaign/ad_group, with head-to-head context.

        NOTE: The Google Ads API restricts the `auction_insight_*` metric family
        to developer tokens with the appropriate access level. If the token
        lacks access, this tool returns a structured error with next steps
        instead of raising.
        """
        ensure_tool_allowed("get_auction_insights")
        cid = ensure_customer_allowed(customer_id)
        lvl = (level or "").strip().lower()
        if lvl not in {"campaign", "ad_group"}:
            raise ValueError("level must be 'campaign' or 'ad_group'")
        if lvl == "ad_group" and not campaign_id:
            raise ValueError("campaign_id is required when level='ad_group'")

        camp_filter = ""
        if campaign_id:
            camp_filter = f" AND campaign.id = {_id(campaign_id, 'campaign_id')}"

        metric_fields = (
            "metrics.auction_insight_search_impression_share, "
            "metrics.auction_insight_search_absolute_top_impression_percentage, "
            "metrics.auction_insight_search_top_impression_percentage, "
            "metrics.auction_insight_search_outranking_share, "
            "metrics.auction_insight_search_overlap_rate, "
            "metrics.auction_insight_search_position_above_rate"
        )
        if lvl == "campaign":
            query = f"""
                SELECT
                  campaign.id, campaign.name, campaign.status,
                  segments.auction_insight_domain,
                  {metric_fields}
                FROM campaign
                WHERE {_date_clause(days)}
                  AND campaign.status != 'REMOVED'
                  {camp_filter}
                ORDER BY campaign.name,
                         metrics.auction_insight_search_impression_share DESC
            """
        else:
            query = f"""
                SELECT
                  campaign.id, campaign.name,
                  ad_group.id, ad_group.name, ad_group.status,
                  segments.auction_insight_domain,
                  {metric_fields}
                FROM ad_group
                WHERE {_date_clause(days)}
                  AND ad_group.status != 'REMOVED'
                  {camp_filter}
                ORDER BY ad_group.name,
                         metrics.auction_insight_search_impression_share DESC
            """

        try:
            rows = search(cid, query)
        except GoogleAdsError as exc:
            if exc.has_code("authorizationError", "METRIC_ACCESS_DENIED"):
                return format_json({
                    "customer_id": cid,
                    "tool": "get_auction_insights",
                    "fresh": True,
                    "level": lvl,
                    "error": {
                        "code": "METRIC_ACCESS_DENIED",
                        "message": (
                            "The developer token does not have access to the "
                            "auction_insight_* metric family. This is a per-token "
                            "authorization set by Google, independent of the account."
                        ),
                        "how_to_fix": [
                            "Verify your token is at Basic or higher in the API Center.",
                            "Request Auction Insights access via the Google Ads API application form.",
                            "For clients where you cannot get access, export from the Ads UI "
                            "(Insights > Auction Insights) as a workaround.",
                        ],
                        "details": exc.to_dict(),
                    },
                })
            raise

        report = _summarize_auction_insights(
            rows=rows, level=lvl, days=days, campaign_id=campaign_id
        )
        return format_json({
            "customer_id": cid,
            "tool": "get_auction_insights",
            "fresh": True,
            "filter": {
                "level": lvl,
                "campaign_id": campaign_id,
                "days": days,
            },
            **report,
        })

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def check_consent_signal_coverage(
        customer_id: str = Field(description="Google Ads customer ID"),
        days: int = Field(default=30, description="Lookback window 1-365"),
    ) -> str:
        """Audit customer tracking terms and compare `conversions` vs `all_conversions` per campaign.

        Consent signals (`ad_user_data`, `ad_personalization`) fire client-side in the tag; the
        Google Ads API does NOT expose them per event. This tool checks customer-level tracking
        settings and compares `conversions / all_conversions` against the previous same-length
        window as a measurement-scope diagnostic (primary vs. all conversions, including secondary
        actions) — NOT as proof of Consent Mode status.
        """
        ensure_tool_allowed("check_consent_signal_coverage")
        cid = ensure_customer_allowed(customer_id)
        if not 1 <= days <= 365:
            raise ValueError("days must be between 1 and 365")

        end = date.today()
        current_start = end - timedelta(days=days - 1)
        prev_end = current_start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=days - 1)

        def _range(start, end_):
            return (
                f"segments.date BETWEEN '{start.isoformat()}' AND '{end_.isoformat()}'"
            )

        base_query = """
            SELECT campaign.id, campaign.name, campaign.status,
                   metrics.conversions, metrics.all_conversions,
                   metrics.conversions_value, metrics.all_conversions_value
            FROM campaign
            WHERE {clause} AND campaign.status != 'REMOVED'
        """
        current_rows = search(cid, base_query.format(clause=_range(current_start, end)))
        previous_rows = search(cid, base_query.format(clause=_range(prev_start, prev_end)))

        settings_rows = search(
            cid,
            """
            SELECT customer.id, customer.descriptive_name,
                   customer.conversion_tracking_setting.accepted_customer_data_terms,
                   customer.conversion_tracking_setting.enhanced_conversions_for_leads_enabled,
                   customer.conversion_tracking_setting.conversion_tracking_status
            FROM customer
            """,
        )
        report = _summarize_consent_coverage(
            customer_id=cid,
            current_rows=current_rows,
            previous_rows=previous_rows,
            settings_rows=settings_rows,
            days=days,
            current_period=(current_start.isoformat(), end.isoformat()),
            previous_period=(prev_start.isoformat(), prev_end.isoformat()),
        )
        return format_json({
            "customer_id": cid,
            "tool": "check_consent_signal_coverage",
            "fresh": True,
            **report,
        })

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_impression_share_losses(
        customer_id: str = Field(description="Google Ads customer ID"),
        days: int = Field(default=30, description="Lookback window 1-365"),
        level: str = Field(
            default="campaign",
            description="Aggregation level: 'campaign' (default) or 'ad_group'",
        ),
        campaign_id: Optional[str] = Field(
            default=None,
            description="Optional filter — only meaningful for ad_group level",
        ),
    ) -> str:
        """Return Search Impression Share losses per unit with automatic budget-limited / rank-limited diagnosis."""
        ensure_tool_allowed("get_impression_share_losses")
        cid = ensure_customer_allowed(customer_id)
        lvl = (level or "").strip().lower()
        if lvl not in {"campaign", "ad_group"}:
            raise ValueError("level must be 'campaign' or 'ad_group'")

        if lvl == "campaign":
            query = f"""
                SELECT
                  campaign.id, campaign.name, campaign.status,
                  campaign.advertising_channel_type,
                  metrics.impressions, metrics.clicks, metrics.cost_micros,
                  metrics.search_impression_share,
                  metrics.search_budget_lost_impression_share,
                  metrics.search_rank_lost_impression_share,
                  metrics.search_top_impression_share,
                  metrics.search_budget_lost_top_impression_share,
                  metrics.search_rank_lost_top_impression_share,
                  metrics.search_absolute_top_impression_share,
                  metrics.search_budget_lost_absolute_top_impression_share,
                  metrics.search_rank_lost_absolute_top_impression_share
                FROM campaign
                WHERE {_date_clause(days)}
                  AND campaign.status != 'REMOVED'
                ORDER BY metrics.cost_micros DESC
            """
        else:
            filters = [_date_clause(days), "ad_group.status != 'REMOVED'"]
            if campaign_id:
                filters.append(f"campaign.id = {_id(campaign_id, 'campaign_id')}")
            query = f"""
                SELECT
                  campaign.id, campaign.name,
                  ad_group.id, ad_group.name, ad_group.status,
                  metrics.impressions, metrics.clicks,
                  metrics.search_impression_share,
                  metrics.search_rank_lost_impression_share,
                  metrics.search_top_impression_share,
                  metrics.search_rank_lost_top_impression_share,
                  metrics.search_absolute_top_impression_share,
                  metrics.search_rank_lost_absolute_top_impression_share
                FROM ad_group
                WHERE {' AND '.join(filters)}
                ORDER BY metrics.impressions DESC
            """
        rows = search(cid, query)
        report = _summarize_impression_share(rows=rows, level=lvl, days=days)
        return format_json({
            "customer_id": cid,
            "tool": "get_impression_share_losses",
            "fresh": True,
            "filter": {
                "level": lvl,
                "campaign_id": campaign_id,
                "days": days,
            },
            **report,
        })

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
