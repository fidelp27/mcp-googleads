"""Channel-specific diagnostics: Performance Max, Shopping, Display/Demand Gen placements & video, and Auction Insights CSV fallback.

Read-only tools mapped to their respective domain modules:
- `get_pmax_diagnostics` (`pmax`): asset groups, asset labels & policy, signals,
  listing group filters, search term insights, and PMax placements.
- `get_shopping_performance` (`shopping`): product/brand/category/product-group
  ROAS and zero-conversion spend detection.
- `get_placements_and_video_performance` (`display`, `demand-gen`): placement
  brand safety / mobile-app waste + video view rate & quartile funnel.
- `analyze_auction_insights_csv` (`search`, `pmax`, `shopping`): parses Google
  Ads UI CSV exports (EN/ES) when the API token lacks `auction_insight_*` access.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Annotated, Any, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from .api import format_json, search, search as _api_search, search_with_meta
from .config import client_display_name, ensure_customer_allowed, ensure_tool_allowed, resolve_worker_file
from .dates import coverage_metadata, resolve_date_range
from .diagnostics_tools import _account_meta, _clean_campaign_id, _safe_search, _safe_search as _dt_safe_search
from .errors import GoogleAdsError
from .governance import audited_tool


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
LOCAL_READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

_PMAX_INSIGHT_MAX_CAMPAIGNS = 10
_PMAX_INSIGHT_PER_CAMPAIGN_LIMIT = 25
_PMAX_INSIGHT_GLOBAL_LIMIT = 50
_PMAX_PLACEMENTS_LIMIT = 100


def _search_bounded(customer_id: str, query: str) -> tuple[list[dict[str, Any]], bool]:
    """Run bounded search returning `(rows, truncated)` while preserving `channels_tools.search` monkeypatches."""
    if search is not _api_search:
        return search(customer_id, query), False
    meta = search_with_meta(customer_id, query)
    return meta["rows"], bool(meta["truncated"])


def _safe_search_bounded(
    customer_id: str,
    query: str,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Run optional bounded search returning `(rows, truncated, warning)` while preserving `_safe_search` monkeypatches."""
    if _safe_search is not _dt_safe_search:
        rows, warn = _safe_search(customer_id, query)
        return rows, False, warn
    try:
        rows, truncated = _search_bounded(customer_id, query)
        return rows, truncated, None
    except GoogleAdsError as exc:
        return (
            [],
            False,
            f"Optional diagnostic query unavailable ({exc.status or exc.http_status}): {exc.message or exc}",
        )


# ---------- Pure summarizers ----------


def _summarize_pmax_diagnostics(
    *,
    customer_id: str,
    asset_group_rows: list[dict[str, Any]],
    asset_rows: list[dict[str, Any]],
    signal_rows: list[dict[str, Any]],
    listing_filter_rows: list[dict[str, Any]],
    search_insight_rows: list[dict[str, Any]],
    placement_rows: list[dict[str, Any]],
    coverage: dict[str, Any],
    warnings: list[str],
    insights_coverage: dict[str, Any] | None = None,
    placements_coverage: dict[str, Any] | None = None,
    truncated_queries: list[str] | None = None,
) -> dict[str, Any]:
    assets_by_group: dict[str, list[dict[str, Any]]] = {}
    for row in asset_rows:
        ag_id = str((row.get("assetGroup") or {}).get("id") or "")
        aga = row.get("assetGroupAsset") or {}
        asset = row.get("asset") or {}
        policy = aga.get("policySummary") or asset.get("policySummary") or {}
        assets_by_group.setdefault(ag_id, []).append(
            {
                "asset_id": str(asset.get("id") or ""),
                "asset_name": asset.get("name"),
                "asset_type": asset.get("type"),
                "field_type": aga.get("fieldType"),
                "status": aga.get("status"),
                "primary_status": str(aga.get("primaryStatus") or "UNKNOWN").upper(),
                "primary_status_reasons": list(aga.get("primaryStatusReasons") or []),
                "performance_label": str(aga.get("performanceLabel") or "UNKNOWN").upper(),
                "approval_status": str(policy.get("approvalStatus") or "UNKNOWN").upper(),
            }
        )

    signals_by_group: dict[str, dict[str, list[str]]] = {}
    for row in signal_rows:
        ag_id = str((row.get("assetGroup") or {}).get("id") or "")
        sig = row.get("assetGroupSignal") or {}
        bucket = signals_by_group.setdefault(ag_id, {"audiences": [], "search_themes": []})
        aud = (sig.get("audience") or {}).get("audience")
        theme = (sig.get("searchTheme") or {}).get("text")
        if aud:
            bucket["audiences"].append(str(aud))
        if theme:
            bucket["search_themes"].append(str(theme))

    filters_by_group: dict[str, int] = {}
    for row in listing_filter_rows:
        ag_id = str((row.get("assetGroup") or {}).get("id") or "")
        if ag_id:
            filters_by_group[ag_id] = filters_by_group.get(ag_id, 0) + 1

    groups_report: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []

    for row in asset_group_rows:
        camp = row.get("campaign") or {}
        ag = row.get("assetGroup") or {}
        m = row.get("metrics") or {}
        ag_id = str(ag.get("id") or "")
        ag_name = ag.get("name") or ag_id
        strength = str(ag.get("adStrength") or "UNKNOWN").upper()
        impressions = int(m.get("impressions") or 0)
        clicks = int(m.get("clicks") or 0)
        cost_micros = int(m.get("costMicros") or 0)
        conversions = float(m.get("conversions") or 0)
        conv_value = float(m.get("conversionsValue") or 0)
        roas = round(conv_value / (cost_micros / 1_000_000), 2) if cost_micros > 0 else 0.0

        group_assets = assets_by_group.get(ag_id, [])
        low_assets = [a for a in group_assets if a["performance_label"] == "LOW"]
        limited_assets = [
            a for a in group_assets if a["primary_status"] in {"LIMITED", "NOT_ELIGIBLE"}
        ]
        disapproved_assets = [a for a in group_assets if a["approval_status"] == "DISAPPROVED"]
        sig_info = signals_by_group.get(ag_id, {"audiences": [], "search_themes": []})

        groups_report.append(
            {
                "campaign_id": str(camp.get("id") or ""),
                "campaign_name": camp.get("name"),
                "asset_group_id": ag_id,
                "asset_group_name": ag_name,
                "status": ag.get("status"),
                "ad_strength": strength,
                "primary_status": ag.get("primaryStatus"),
                "primary_status_reasons": list(ag.get("primaryStatusReasons") or []),
                "metrics": {
                    "impressions": impressions,
                    "clicks": clicks,
                    "cost_micros": cost_micros,
                    "conversions": conversions,
                    "conversions_value": conv_value,
                    "roas": roas,
                },
                "assets_summary": {
                    "total_assets": len(group_assets),
                    "limited_or_ineligible_count": len(limited_assets),
                    "low_performing_count": len(low_assets),
                    "disapproved_count": len(disapproved_assets),
                    "limited_assets": limited_assets[:10],
                    "low_assets": low_assets[:10],
                    "disapproved_assets": disapproved_assets[:10],
                },
                "signals": sig_info,
                "listing_group_filter_count": filters_by_group.get(ag_id, 0),
            }
        )

        if strength == "POOR":
            alerts.append(
                {
                    "severity": "warning",
                    "code": "PMAX_POOR_AD_STRENGTH",
                    "asset_group_id": ag_id,
                    "message": f"Asset group '{ag_name}' has POOR ad strength; expand headline, image, and video variety.",
                }
            )
        if disapproved_assets:
            alerts.append(
                {
                    "severity": "high",
                    "code": "PMAX_DISAPPROVED_ASSETS",
                    "asset_group_id": ag_id,
                    "message": f"Asset group '{ag_name}' has {len(disapproved_assets)} DISAPPROVED asset(s).",
                }
            )
        if limited_assets:
            alerts.append(
                {
                    "severity": "warning",
                    "code": "PMAX_LIMITED_OR_INELIGIBLE_ASSETS",
                    "asset_group_id": ag_id,
                    "message": (
                        f"Asset group '{ag_name}' has {len(limited_assets)} asset(s) with primary_status "
                        "LIMITED or NOT_ELIGIBLE; inspect primary_status_reasons and rotate creative."
                    ),
                }
            )
        if low_assets:
            alerts.append(
                {
                    "severity": "info",
                    "code": "PMAX_LOW_PERFORMING_ASSETS",
                    "asset_group_id": ag_id,
                    "message": f"Asset group '{ag_name}' has {len(low_assets)} asset(s) rated LOW; consider rotating creative.",
                }
            )
        if cost_micros > 0 and conversions == 0:
            alerts.append(
                {
                    "severity": "high",
                    "code": "PMAX_ASSET_GROUP_ZERO_CONVERSIONS_SPEND",
                    "asset_group_id": ag_id,
                    "message": (
                        f"Asset group '{ag_name}' spent {cost_micros / 1_000_000:,.2f} "
                        f"{coverage['currency_code']} with 0 conversions."
                    ),
                }
            )

    search_themes = [
        {
            "campaign_id": str(
                (r.get("campaignSearchTermInsight") or {}).get("campaignId")
                or (r.get("campaign") or {}).get("id")
                or ""
            ),
            "category_label": (r.get("campaignSearchTermInsight") or {}).get("categoryLabel"),
            "impressions": int((r.get("metrics") or {}).get("impressions") or 0),
            "clicks": int((r.get("metrics") or {}).get("clicks") or 0),
            "conversions": float((r.get("metrics") or {}).get("conversions") or 0),
            "conversions_value": float((r.get("metrics") or {}).get("conversionsValue") or 0),
        }
        for r in search_insight_rows
    ]

    placements = [
        {
            "display_name": (r.get("performanceMaxPlacementView") or {}).get("displayName"),
            "placement": (r.get("performanceMaxPlacementView") or {}).get("placement"),
            "placement_type": (r.get("performanceMaxPlacementView") or {}).get("placementType"),
            "impressions": int((r.get("metrics") or {}).get("impressions") or 0),
        }
        for r in placement_rows
    ]
    total_placement_imps = sum(p["impressions"] for p in placements)
    app_placement_imps = sum(
        p["impressions"] for p in placements if str(p["placement_type"] or "").upper() == "MOBILE_APPLICATION"
    )
    if total_placement_imps > 0 and app_placement_imps / total_placement_imps >= 0.4:
        alerts.append(
            {
                "severity": "warning",
                "code": "PMAX_HIGH_MOBILE_APP_IMPRESSIONS",
                "message": (
                    f"{round(app_placement_imps / total_placement_imps * 100, 1)}% of PMax placement impressions "
                    "served on MOBILE_APPLICATION inventory. Review account-level placement exclusions."
                ),
            }
        )

    effective_insights_cov = insights_coverage or {
        "eligible_campaigns_count": len(
            {
                str((r.get("campaign") or {}).get("id") or "").strip()
                for r in asset_group_rows
                if str((r.get("campaign") or {}).get("id") or "").strip()
            }
        ),
        "queried_campaigns_count": len(
            {
                str((r.get("campaign") or {}).get("id") or "").strip()
                for r in asset_group_rows
                if str((r.get("campaign") or {}).get("id") or "").strip()
            }
        ),
        "omitted_campaigns_count": 0,
        "campaign_limit": _PMAX_INSIGHT_MAX_CAMPAIGNS,
        "per_campaign_limit": _PMAX_INSIGHT_PER_CAMPAIGN_LIMIT,
        "global_limit": _PMAX_INSIGHT_GLOBAL_LIMIT,
        "capped_by_campaign_limit": False,
        "capped_by_per_campaign_limit": False,
        "per_campaign_capped_ids": [],
        "capped_by_global_limit": False,
        "combined_rows_before_global_cap": len(search_themes),
        "returned_rows": len(search_themes),
        "failed_campaigns_count": 0,
        "status": "complete" if search_themes else "empty",
    }
    effective_placements_cov = placements_coverage or {
        "limit": _PMAX_PLACEMENTS_LIMIT,
        "returned_rows": len(placements),
        "capped_by_limit": len(placements) >= _PMAX_PLACEMENTS_LIMIT,
        "status": (
            "sampled"
            if len(placements) >= _PMAX_PLACEMENTS_LIMIT
            else ("complete" if placements else "empty")
        ),
    }

    truncated_list = sorted(set(truncated_queries or []))
    sampling_warnings: list[str] = []
    if truncated_list:
        sampling_warnings.append(
            f"Google Ads search row cap (GOOGLE_ADS_MAX_SEARCH_ROWS) truncated query/queries: "
            f"{', '.join(truncated_list)}. Narrow the date range or pass campaign_id for complete PMax coverage."
        )
    if effective_insights_cov.get("capped_by_campaign_limit"):
        sampling_warnings.append(
            f"Search term insights queried top {effective_insights_cov['queried_campaigns_count']} of "
            f"{effective_insights_cov['eligible_campaigns_count']} eligible PMax campaigns by spend "
            f"({effective_insights_cov['omitted_campaigns_count']} omitted); pass campaign_id for full per-campaign coverage."
        )
    if effective_insights_cov.get("capped_by_per_campaign_limit"):
        sampling_warnings.append(
            f"One or more campaigns reached the per-campaign limit of {_PMAX_INSIGHT_PER_CAMPAIGN_LIMIT} "
            f"search term insight categories (campaign_ids={effective_insights_cov.get('per_campaign_capped_ids')})."
        )
    if effective_insights_cov.get("capped_by_global_limit"):
        sampling_warnings.append(
            f"Combined search term insights ({effective_insights_cov['combined_rows_before_global_cap']} rows) "
            f"were trimmed to top {_PMAX_INSIGHT_GLOBAL_LIMIT} by impressions."
        )
    if effective_placements_cov.get("capped_by_limit"):
        sampling_warnings.append(
            f"PMax placements reached the limit of {_PMAX_PLACEMENTS_LIMIT} rows ordered by impressions."
        )

    any_truncated = bool(truncated_list)
    any_sampled = bool(sampling_warnings)
    if warnings:
        coverage_status = "partial_error"
    elif any_sampled:
        coverage_status = "sampled"
    elif not groups_report and not search_themes and not placements:
        coverage_status = "empty"
    else:
        coverage_status = "complete"

    payload: dict[str, Any] = {
        "customer_id": customer_id,
        "client_name": client_display_name(customer_id),
        "tool": "get_pmax_diagnostics",
        **coverage,
        "asset_groups": groups_report,
        "search_term_insights": search_themes,
        "placements": placements,
        "alerts": alerts,
        "summary": {
            "asset_groups_count": len(groups_report),
            "search_categories_count": len(search_themes),
            "placements_count": len(placements),
            "alert_count": len(alerts),
        },
        "truncated": any_truncated,
        "totals_are_partial": any_truncated,
        "truncated_queries": truncated_list,
        "coverage_status": coverage_status,
        "coverage_complete": (not bool(warnings)) and (not any_sampled),
        "search_term_insights_coverage": effective_insights_cov,
        "placements_coverage": effective_placements_cov,
        "sampling_warnings": sampling_warnings,
        "warnings": warnings,
    }
    if any_truncated:
        payload["truncation_warning"] = (
            f"One or more PMax queries reached GOOGLE_ADS_MAX_SEARCH_ROWS ({', '.join(truncated_list)}); "
            "asset groups, assets, or derived insights may be incomplete."
        )
    return payload


def _summarize_shopping_performance(
    *,
    customer_id: str,
    group_by: str,
    rows: list[dict[str, Any]],
    coverage: dict[str, Any],
    min_waste_spend: float,
    truncated: bool,
) -> dict[str, Any]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        seg = row.get("segments") or {}
        pg = row.get("productGroupView") or {}
        m = row.get("metrics") or {}
        if group_by == "product":
            key = str(seg.get("productItemId") or "UNKNOWN")
            label = seg.get("productTitle") or key
        elif group_by == "brand":
            key = str(seg.get("productBrand") or "UNSPECIFIED")
            label = key
        elif group_by == "category":
            key = str(seg.get("productTypeL1") or "UNSPECIFIED")
            label = key
        else:
            key = str(pg.get("resourceName") or "UNKNOWN")
            label = key

        b = buckets.setdefault(
            key,
            {
                "key": key,
                "label": label,
                "impressions": 0,
                "clicks": 0,
                "cost_micros": 0,
                "conversions": 0.0,
                "conversions_value": 0.0,
            },
        )
        b["impressions"] += int(m.get("impressions") or 0)
        b["clicks"] += int(m.get("clicks") or 0)
        b["cost_micros"] += int(m.get("costMicros") or 0)
        b["conversions"] += float(m.get("conversions") or 0)
        b["conversions_value"] += float(m.get("conversionsValue") or 0)

    items: list[dict[str, Any]] = []
    for b in buckets.values():
        spend = b["cost_micros"] / 1_000_000
        b["spend"] = round(spend, 2)
        b["ctr"] = round(b["clicks"] / b["impressions"], 4) if b["impressions"] else 0.0
        b["roas"] = round(b["conversions_value"] / spend, 2) if spend > 0 else 0.0
        b["cpa"] = round(spend / b["conversions"], 2) if b["conversions"] > 0 else None
        items.append(b)

    items.sort(key=lambda x: x["cost_micros"], reverse=True)
    winners = sorted(
        [i for i in items if i["conversions"] > 0],
        key=lambda x: x["conversions_value"],
        reverse=True,
    )[:20]
    wasted = [
        i
        for i in items
        if i["conversions"] == 0 and i["spend"] >= min_waste_spend and i["spend"] > 0
    ]
    total_spend = round(sum(i["spend"] for i in items), 2)
    total_wasted = round(sum(i["spend"] for i in wasted), 2)
    total_value = round(sum(i["conversions_value"] for i in items), 2)

    return {
        "customer_id": customer_id,
        "client_name": client_display_name(customer_id),
        "tool": "get_shopping_performance",
        "group_by": group_by,
        **coverage,
        "truncated": truncated,
        "totals_are_partial": truncated,
        **(
            {
                "truncation_warning": (
                    "Shopping query reached GOOGLE_ADS_MAX_SEARCH_ROWS; summary totals reflect only "
                    "the returned subset. Narrow the date range or filter by campaign_id."
                )
            }
            if truncated
            else {}
        ),
        "summary": {
            "items_evaluated": len(items),
            "total_spend": total_spend,
            "total_conversions_value": total_value,
            "blended_roas": round(total_value / total_spend, 2) if total_spend > 0 else 0.0,
            "wasted_items_count": len(wasted),
            "total_wasted_spend": total_wasted,
            "wasted_spend_share_pct": round(total_wasted / total_spend * 100, 1) if total_spend > 0 else 0.0,
        },
        "top_winners": winners,
        "wasted_products": wasted[:30],
        "items": items[:100],
    }


def _summarize_placements_and_video(
    *,
    customer_id: str,
    placement_rows: list[dict[str, Any]],
    video_rows: list[dict[str, Any]],
    coverage: dict[str, Any],
    truncated: bool,
    warnings: list[str],
) -> dict[str, Any]:
    placements: list[dict[str, Any]] = []
    exclusion_candidates: list[dict[str, Any]] = []

    for row in placement_rows:
        pv = row.get("detailPlacementView") or row.get("groupPlacementView") or {}
        camp = row.get("campaign") or {}
        m = row.get("metrics") or {}
        cost_micros = int(m.get("costMicros") or 0)
        conversions = float(m.get("conversions") or 0)
        impressions = int(m.get("impressions") or 0)
        clicks = int(m.get("clicks") or 0)
        ptype = str(pv.get("placementType") or "UNKNOWN").upper()
        item = {
            "campaign_id": str(camp.get("id") or ""),
            "campaign_name": camp.get("name"),
            "display_name": pv.get("displayName"),
            "placement": pv.get("placement"),
            "placement_type": ptype,
            "target_url": pv.get("targetUrl"),
            "impressions": impressions,
            "clicks": clicks,
            "cost_micros": cost_micros,
            "spend": round(cost_micros / 1_000_000, 2),
            "conversions": conversions,
            "conversions_value": float(m.get("conversionsValue") or 0),
        }
        placements.append(item)
        if conversions == 0 and (ptype == "MOBILE_APPLICATION" or cost_micros > 0):
            exclusion_candidates.append(item)

    video_campaigns: list[dict[str, Any]] = []
    for row in video_rows:
        camp = row.get("campaign") or {}
        m = row.get("metrics") or {}
        views = int(m.get("videoViews") or 0)
        if views <= 0 and int(m.get("impressions") or 0) <= 0:
            continue
        video_campaigns.append(
            {
                "campaign_id": str(camp.get("id") or ""),
                "campaign_name": camp.get("name"),
                "channel_type": camp.get("advertisingChannelType"),
                "impressions": int(m.get("impressions") or 0),
                "video_views": views,
                "video_view_rate": round(float(m.get("videoViewRate") or 0), 4),
                "average_cpv_micros": int(float(m.get("averageCpv") or 0)),
                "quartile_rates": {
                    "p25": round(float(m.get("videoQuartileP25Rate") or 0), 4),
                    "p50": round(float(m.get("videoQuartileP50Rate") or 0), 4),
                    "p75": round(float(m.get("videoQuartileP75Rate") or 0), 4),
                    "p100": round(float(m.get("videoQuartileP100Rate") or 0), 4),
                },
                "cost_micros": int(m.get("costMicros") or 0),
                "conversions": float(m.get("conversions") or 0),
            }
        )

    exclusion_candidates.sort(key=lambda x: x["cost_micros"], reverse=True)
    return {
        "customer_id": customer_id,
        "client_name": client_display_name(customer_id),
        "tool": "get_placements_and_video_performance",
        **coverage,
        "truncated": truncated,
        "totals_are_partial": truncated,
        "coverage_complete": not bool(warnings) and not truncated,
        "placements_count": len(placements),
        "exclusion_candidates_count": len(exclusion_candidates),
        "exclusion_candidates": exclusion_candidates[:30],
        "placements": placements[:100],
        "video_campaigns": video_campaigns,
        "warnings": warnings,
    }


def _parse_pct(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text or text in {"--", "N/A", "-"}:
        return None
    below = text.startswith("<")
    cleaned = text.lstrip("<> ").rstrip("%").replace(",", ".").strip()
    try:
        val = float(cleaned) / 100.0
        return round(val * 0.5, 4) if below else round(val, 4)
    except ValueError:
        return None


_AUCTION_COL_ALIASES: dict[str, tuple[str, ...]] = {
    "domain": ("display url domain", "dominio de la url visible", "domain", "dominio"),
    "impression_share": ("impression share", "cuota de impresiones"),
    "overlap_rate": ("overlap rate", "porcentaje de superposición", "porcentaje de superposicion"),
    "position_above_rate": ("position above rate", "porcentaje de posición superior", "porcentaje de posicion superior"),
    "top_of_page_rate": ("top of page rate", "porcentaje de la parte superior de la página", "porcentaje de la parte superior de la pagina"),
    "abs_top_of_page_rate": ("abs. top of page rate", "porcentaje de la parte superior absoluta de la página", "porcentaje de la parte superior absoluta de la pagina"),
    "outranking_share": ("outranking share", "cuota de ranking superior"),
}


def parse_auction_insights_csv_text(
    csv_text: str,
    *,
    own_domain_label: str = "You",
) -> dict[str, Any]:
    """Parse Google Ads UI Auction Insights CSV (English or Spanish) into structured analytics."""
    lines = [line for line in csv_text.splitlines() if line.strip()]
    header_idx = None
    for idx, line in enumerate(lines[:10]):
        low = line.lower()
        if any(alias in low for alias in _AUCTION_COL_ALIASES["domain"]):
            header_idx = idx
            break
    if header_idx is None:
        raise ValueError(
            "Could not find Auction Insights header row ('Display URL domain' or 'Dominio de la URL visible')."
        )

    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    if not reader.fieldnames:
        raise ValueError("CSV file has no columns.")

    col_map: dict[str, str] = {}
    for col in reader.fieldnames:
        norm = col.strip().lower()
        for canonical, aliases in _AUCTION_COL_ALIASES.items():
            if norm in aliases and canonical not in col_map:
                col_map[canonical] = col

    if "domain" not in col_map:
        raise ValueError("Missing domain column in Auction Insights CSV.")

    own_labels = {own_domain_label.strip().lower(), "you", "tú", "tu"}
    you_row: dict[str, Any] | None = None
    competitors: list[dict[str, Any]] = []

    for row in reader:
        domain = (row.get(col_map["domain"]) or "").strip()
        if not domain or domain.lower().startswith("total"):
            continue
        entry = {
            "domain": domain,
            "impression_share": _parse_pct(row.get(col_map.get("impression_share", ""))),
            "overlap_rate": _parse_pct(row.get(col_map.get("overlap_rate", ""))),
            "position_above_rate": _parse_pct(row.get(col_map.get("position_above_rate", ""))),
            "top_of_page_rate": _parse_pct(row.get(col_map.get("top_of_page_rate", ""))),
            "abs_top_of_page_rate": _parse_pct(row.get(col_map.get("abs_top_of_page_rate", ""))),
            "outranking_share": _parse_pct(row.get(col_map.get("outranking_share", ""))),
        }
        if domain.lower() in own_labels:
            you_row = entry
        else:
            competitors.append(entry)

    competitors.sort(key=lambda x: x["impression_share"] or 0.0, reverse=True)
    top_pressure = [
        c for c in competitors if (c["overlap_rate"] or 0.0) >= 0.35 and (c["position_above_rate"] or 0.0) >= 0.5
    ]

    return {
        "source": "google_ads_ui_csv_export",
        "provenance_note": (
            "Metrics parsed from a local Google Ads UI CSV export (workaround when the API "
            "developer token/project lacks auction_insight_* access)."
        ),
        "you": you_row,
        "competitor_count": len(competitors),
        "top_competitor": competitors[0] if competitors else None,
        "high_pressure_competitors": top_pressure,
        "competitors": competitors,
    }


def register_channel_tools(mcp) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_pmax_diagnostics(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
        campaign_id: Annotated[Optional[str], Field(description="Optional Performance Max campaign ID")] = None,
        days: Annotated[int, Field(description="Lookback window (1 to 365 days)")] = 30,
        complete_days_only: Annotated[
            bool, Field(description="Exclude the current incomplete day in the account time zone")
        ] = True,
    ) -> str:
        """Audit Performance Max asset groups, asset labels/approvals, signals, listing filters, search themes, and placements."""
        ensure_tool_allowed("get_pmax_diagnostics")
        cid = ensure_customer_allowed(customer_id)
        clean_camp = _clean_campaign_id(campaign_id)
        currency_code, time_zone, _ = _account_meta(cid)
        window = resolve_date_range(days=days, time_zone=time_zone, complete_days_only=complete_days_only)
        coverage = coverage_metadata(currency_code=currency_code, window=window)
        camp_filter = f" AND campaign.id = {clean_camp}" if clean_camp else ""
        warnings: list[str] = []

        truncated_queries: list[str] = []

        ag_rows, ag_truncated = _search_bounded(
            cid,
            f"""
            SELECT campaign.id, campaign.name,
                   asset_group.id, asset_group.name, asset_group.status,
                   asset_group.ad_strength, asset_group.primary_status,
                   asset_group.primary_status_reasons,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM asset_group
            WHERE {window.gaql_clause}
              AND asset_group.status != 'REMOVED'{camp_filter}
            ORDER BY metrics.cost_micros DESC
            """,
        )
        if ag_truncated:
            truncated_queries.append("asset_group")

        asset_rows, asset_truncated, warn = _safe_search_bounded(
            cid,
            f"""
            SELECT asset_group.id,
                   asset_group_asset.field_type, asset_group_asset.status,
                   asset_group_asset.primary_status,
                   asset_group_asset.primary_status_reasons,
                   asset_group_asset.policy_summary.approval_status,
                   asset.id, asset.name, asset.type
            FROM asset_group_asset
            WHERE asset_group_asset.status != 'REMOVED'{camp_filter}
            """,
        )
        if asset_truncated:
            truncated_queries.append("asset_group_asset")
        if warn:
            warnings.append(warn)

        signal_rows, signal_truncated, warn = _safe_search_bounded(
            cid,
            f"""
            SELECT asset_group.id,
                   asset_group_signal.audience.audience,
                   asset_group_signal.search_theme.text
            FROM asset_group_signal
            WHERE asset_group.status != 'REMOVED'{camp_filter}
            """,
        )
        if signal_truncated:
            truncated_queries.append("asset_group_signal")
        if warn:
            warnings.append(warn)

        filter_rows, filter_truncated, warn = _safe_search_bounded(
            cid,
            f"""
            SELECT asset_group.id, asset_group_listing_group_filter.type,
                   asset_group_listing_group_filter.listing_source
            FROM asset_group_listing_group_filter
            WHERE asset_group.status != 'REMOVED'{camp_filter}
            """,
        )
        if filter_truncated:
            truncated_queries.append("asset_group_listing_group_filter")
        if warn:
            warnings.append(warn)

        # Google Ads API requires filtering campaign_search_term_insight by a single campaign_id.
        eligible_pmax_campaign_ids: list[str] = []
        if clean_camp:
            eligible_pmax_campaign_ids = [clean_camp]
        else:
            seen_cids: set[str] = set()
            for row in ag_rows:
                raw_cid = str((row.get("campaign") or {}).get("id") or "").strip()
                if raw_cid.isdigit() and raw_cid not in seen_cids:
                    seen_cids.add(raw_cid)
                    eligible_pmax_campaign_ids.append(raw_cid)
        queried_pmax_campaign_ids = eligible_pmax_campaign_ids[:_PMAX_INSIGHT_MAX_CAMPAIGNS]

        insight_rows: list[dict[str, Any]] = []
        per_campaign_capped_ids: list[str] = []
        failed_campaigns_count = 0
        insights_api_truncated = False
        for pmax_cid in queried_pmax_campaign_ids:
            rows_part, part_truncated, warn = _safe_search_bounded(
                cid,
                f"""
                SELECT campaign_search_term_insight.campaign_id,
                       campaign_search_term_insight.category_label,
                       metrics.impressions, metrics.clicks,
                       metrics.conversions, metrics.conversions_value
                FROM campaign_search_term_insight
                WHERE campaign_search_term_insight.campaign_id = {pmax_cid}
                  AND {window.gaql_clause}
                ORDER BY metrics.impressions DESC
                LIMIT {_PMAX_INSIGHT_PER_CAMPAIGN_LIMIT}
                """,
            )
            if part_truncated:
                insights_api_truncated = True
                truncated_queries.append("campaign_search_term_insight")
            if warn:
                warnings.append(warn)
                failed_campaigns_count += 1
            else:
                if len(rows_part) >= _PMAX_INSIGHT_PER_CAMPAIGN_LIMIT or part_truncated:
                    per_campaign_capped_ids.append(pmax_cid)
                insight_rows.extend(rows_part)
        insight_rows.sort(
            key=lambda r: int((r.get("metrics") or {}).get("impressions") or 0),
            reverse=True,
        )
        combined_rows_before_global_cap = len(insight_rows)
        capped_by_global_limit = combined_rows_before_global_cap > _PMAX_INSIGHT_GLOBAL_LIMIT
        insight_rows = insight_rows[:_PMAX_INSIGHT_GLOBAL_LIMIT]

        capped_by_campaign_limit = len(eligible_pmax_campaign_ids) > len(queried_pmax_campaign_ids)
        capped_by_per_campaign_limit = bool(per_campaign_capped_ids)
        if failed_campaigns_count > 0:
            insights_status = "partial_error"
        elif (
            capped_by_campaign_limit
            or capped_by_per_campaign_limit
            or capped_by_global_limit
            or insights_api_truncated
            or ag_truncated
        ):
            insights_status = "sampled"
        elif not insight_rows:
            insights_status = "empty"
        else:
            insights_status = "complete"

        insights_coverage = {
            "eligible_campaigns_count": len(eligible_pmax_campaign_ids),
            "queried_campaigns_count": len(queried_pmax_campaign_ids),
            "omitted_campaigns_count": max(
                0, len(eligible_pmax_campaign_ids) - len(queried_pmax_campaign_ids)
            ),
            "campaign_limit": _PMAX_INSIGHT_MAX_CAMPAIGNS,
            "per_campaign_limit": _PMAX_INSIGHT_PER_CAMPAIGN_LIMIT,
            "global_limit": _PMAX_INSIGHT_GLOBAL_LIMIT,
            "capped_by_campaign_limit": capped_by_campaign_limit,
            "capped_by_per_campaign_limit": capped_by_per_campaign_limit,
            "per_campaign_capped_ids": per_campaign_capped_ids,
            "capped_by_global_limit": capped_by_global_limit,
            "capped_by_api_row_limit": insights_api_truncated or ag_truncated,
            "combined_rows_before_global_cap": combined_rows_before_global_cap,
            "returned_rows": len(insight_rows),
            "failed_campaigns_count": failed_campaigns_count,
            "status": insights_status,
        }

        placement_rows, placements_truncated, warn = _safe_search_bounded(
            cid,
            f"""
            SELECT performance_max_placement_view.display_name,
                   performance_max_placement_view.placement,
                   performance_max_placement_view.placement_type,
                   metrics.impressions
            FROM performance_max_placement_view
            WHERE {window.gaql_clause}{camp_filter}
            ORDER BY metrics.impressions DESC
            LIMIT {_PMAX_PLACEMENTS_LIMIT}
            """,
        )
        if placements_truncated:
            truncated_queries.append("performance_max_placement_view")
        placements_failed = bool(warn)
        if warn:
            warnings.append(warn)
        placements_capped = len(placement_rows) >= _PMAX_PLACEMENTS_LIMIT or placements_truncated
        placements_coverage = {
            "limit": _PMAX_PLACEMENTS_LIMIT,
            "returned_rows": len(placement_rows),
            "capped_by_limit": placements_capped,
            "capped_by_api_row_limit": placements_truncated,
            "status": (
                "partial_error"
                if placements_failed
                else ("sampled" if placements_capped else ("complete" if placement_rows else "empty"))
            ),
        }

        report = _summarize_pmax_diagnostics(
            customer_id=cid,
            asset_group_rows=ag_rows,
            asset_rows=asset_rows,
            signal_rows=signal_rows,
            listing_filter_rows=filter_rows,
            search_insight_rows=insight_rows,
            placement_rows=placement_rows,
            coverage=coverage,
            warnings=warnings,
            insights_coverage=insights_coverage,
            placements_coverage=placements_coverage,
            truncated_queries=truncated_queries,
        )
        return format_json(report)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_shopping_performance(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
        group_by: Annotated[
            str,
            Field(description="Grouping level: 'product', 'brand', 'category', or 'product_group'"),
        ] = "product",
        campaign_id: Annotated[Optional[str], Field(description="Optional Shopping/PMax campaign ID")] = None,
        days: Annotated[int, Field(description="Lookback window (1 to 365 days)")] = 30,
        complete_days_only: Annotated[
            bool, Field(description="Exclude the current incomplete day in the account time zone")
        ] = True,
        min_waste_spend: Annotated[
            float, Field(description="Minimum zero-conversion spend (in account currency) to flag as wasted")
        ] = 0.0,
    ) -> str:
        """Analyze Shopping/PMax product, brand, category, or product-group ROAS and zero-conversion spend."""
        ensure_tool_allowed("get_shopping_performance")
        cid = ensure_customer_allowed(customer_id)
        grp = (group_by or "product").strip().lower()
        if grp not in {"product", "brand", "category", "product_group"}:
            raise ValueError("group_by must be 'product', 'brand', 'category', or 'product_group'")
        clean_camp = _clean_campaign_id(campaign_id)
        currency_code, time_zone, _ = _account_meta(cid)
        window = resolve_date_range(days=days, time_zone=time_zone, complete_days_only=complete_days_only)
        coverage = coverage_metadata(currency_code=currency_code, window=window)
        camp_filter = f" AND campaign.id = {clean_camp}" if clean_camp else ""

        if grp == "product_group":
            query = f"""
                SELECT product_group_view.resource_name, campaign.id,
                       metrics.impressions, metrics.clicks, metrics.cost_micros,
                       metrics.conversions, metrics.conversions_value
                FROM product_group_view
                WHERE {window.gaql_clause}{camp_filter}
                ORDER BY metrics.cost_micros DESC
            """
        else:
            dim_fields = {
                "product": "segments.product_item_id, segments.product_title",
                "brand": "segments.product_brand",
                "category": "segments.product_type_l1",
            }[grp]
            query = f"""
                SELECT {dim_fields},
                       metrics.impressions, metrics.clicks, metrics.cost_micros,
                       metrics.conversions, metrics.conversions_value
                FROM shopping_performance_view
                WHERE {window.gaql_clause}{camp_filter}
                ORDER BY metrics.cost_micros DESC
            """

        meta = search_with_meta(cid, query)
        report = _summarize_shopping_performance(
            customer_id=cid,
            group_by=grp,
            rows=meta["rows"],
            coverage=coverage,
            min_waste_spend=max(0.0, float(min_waste_spend)),
            truncated=meta["truncated"],
        )
        return format_json(report)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_placements_and_video_performance(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
        campaign_id: Annotated[Optional[str], Field(description="Optional Display/Demand Gen/Video campaign ID")] = None,
        days: Annotated[int, Field(description="Lookback window (1 to 365 days)")] = 30,
        complete_days_only: Annotated[
            bool, Field(description="Exclude the current incomplete day in the account time zone")
        ] = True,
        include_video: Annotated[
            bool, Field(description="Include video view rate and quartile completion metrics")
        ] = True,
    ) -> str:
        """Audit Display/Demand Gen placements (spotting mobile-app/zero-conversion waste) and video quartile metrics."""
        ensure_tool_allowed("get_placements_and_video_performance")
        cid = ensure_customer_allowed(customer_id)
        clean_camp = _clean_campaign_id(campaign_id)
        currency_code, time_zone, _ = _account_meta(cid)
        window = resolve_date_range(days=days, time_zone=time_zone, complete_days_only=complete_days_only)
        coverage = coverage_metadata(currency_code=currency_code, window=window)
        camp_filter = f" AND campaign.id = {clean_camp}" if clean_camp else ""
        warnings: list[str] = []

        meta = search_with_meta(
            cid,
            f"""
            SELECT campaign.id, campaign.name,
                   detail_placement_view.display_name,
                   detail_placement_view.placement,
                   detail_placement_view.placement_type,
                   detail_placement_view.target_url,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM detail_placement_view
            WHERE {window.gaql_clause}{camp_filter}
            ORDER BY metrics.cost_micros DESC
            """,
        )

        video_rows: list[dict[str, Any]] = []
        if include_video:
            video_rows, warn = _safe_search(
                cid,
                f"""
                SELECT campaign.id, campaign.name, campaign.advertising_channel_type,
                       metrics.impressions, metrics.video_views, metrics.video_view_rate,
                       metrics.average_cpv,
                       metrics.video_quartile_p25_rate,
                       metrics.video_quartile_p50_rate,
                       metrics.video_quartile_p75_rate,
                       metrics.video_quartile_p100_rate,
                       metrics.cost_micros, metrics.conversions
                FROM campaign
                WHERE {window.gaql_clause}
                  AND campaign.status != 'REMOVED'{camp_filter}
                ORDER BY metrics.cost_micros DESC
                """,
            )
            if warn:
                warnings.append(warn)

        report = _summarize_placements_and_video(
            customer_id=cid,
            placement_rows=meta["rows"],
            video_rows=video_rows,
            coverage=coverage,
            truncated=meta["truncated"],
            warnings=warnings,
        )
        return format_json(report)

    @mcp.tool(annotations=LOCAL_READ)
    @audited_tool
    def analyze_auction_insights_csv(
        csv_path: Annotated[
            Optional[str],
            Field(description="Local path to a Google Ads UI Auction Insights CSV export (EN or ES)"),
        ] = None,
        own_domain_label: Annotated[
            str,
            Field(description="Label representing your own account row (default 'You'; also matches 'Tú')"),
        ] = "You",
        csv_content: Annotated[
            Optional[str],
            Field(description="Inline CSV text pasted directly from Google Ads UI (useful in remote MCP mode)"),
        ] = None,
    ) -> str:
        """Parse and analyze an Auction Insights CSV exported from Google Ads UI as fallback when API access is restricted."""
        ensure_tool_allowed("analyze_auction_insights_csv")
        if csv_content is not None and csv_content.strip():
            if len(csv_content.encode("utf-8")) > 2_000_000:
                raise ValueError("csv_content exceeds maximum size of 2 MB.")
            report = parse_auction_insights_csv_text(csv_content, own_domain_label=own_domain_label)
            report["file"] = "inline_csv"
            return format_json(report)
        if not csv_path or not str(csv_path).strip():
            raise ValueError("Provide either csv_path or csv_content.")
        path = resolve_worker_file(Path(csv_path).expanduser()).resolve()
        if not path.is_file():
            raise ValueError(f"Auction Insights CSV file not found: {path}")
        text = path.read_text(encoding="utf-8-sig")
        report = parse_auction_insights_csv_text(text, own_domain_label=own_domain_label)
        report["file"] = path.name
        return format_json(report)
