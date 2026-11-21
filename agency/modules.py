"""Modular MCP tool catalog (--modules / --preset) with read-only enforcement.

Design goals:
- Default `core` module stays compact (<=25 tools) so LLMs across OpenAI,
  Anthropic, Gemini, DeepSeek, and Kimi maintain high tool-calling accuracy.
- Domain modules (`measurement`, `search`, `pmax`, `shopping`, `display`,
  `demand-gen`, `mcc`, `operations`) are opt-in per session or preset.
- When `READ_ONLY_MODE=true` (the default), Google Ads write tools in
  `operations` are NEVER exposed in `tools/list`.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from .config import get_settings


CORE_TOOLS: frozenset[str] = frozenset(
    {
        # Server, accounts & flexible GAQL
        "server_status",
        "list_accounts",
        "execute_gaql_query",
        "run_gaql",
        # Core performance & creative read
        "get_campaign_performance",
        "get_ad_performance",
        "get_ad_creatives",
        "get_account_currency",
        "get_image_assets",
        "download_image_asset",
        "get_asset_usage",
        "analyze_image_assets",
        "list_resources",
        # High-value core operational & reporting tools
        "get_budget_utilization",
        "get_bidding_strategies",
        "get_change_history",
        "generate_client_report",
        "generate_agency_dashboard",
        "compare_periods",
        "get_performance_trends",
        "detect_waste",
        "check_alerts",
        "generate_optimization_plan",
        # Customizable analysis profiles
        "list_analysis_profiles",
        "get_analysis_profile",
    }
)

OPERATIONS_TOOLS: frozenset[str] = frozenset(
    {
        # Campaign, budget, ad group, ad, keyword, audience, asset, conversion writes
        "create_campaign_budget",
        "update_campaign_budget",
        "create_search_campaign",
        "create_pmax_campaign",
        "create_display_campaign",
        "update_campaign",
        "pause_campaign",
        "enable_campaign",
        "remove_campaign",
        "update_campaign_geo_targets",
        "update_campaign_schedule",
        "copy_campaign",
        "create_ad_group",
        "update_ad_group",
        "pause_ad_group",
        "enable_ad_group",
        "remove_ad_group",
        "create_responsive_search_ad",
        "create_call_ad",
        "pause_ad",
        "enable_ad",
        "remove_ad",
        "update_ad",
        "add_keywords_to_ad_group",
        "update_keyword_bid",
        "pause_keyword",
        "enable_keyword",
        "remove_keyword",
        "add_negative_keyword",
        "add_negative_keywords_bulk",
        "create_negative_keyword_list",
        "attach_negative_keyword_list_to_campaign",
        "add_audience_to_ad_group",
        "remove_audience_from_ad_group",
        "update_bid_modifier",
        "create_customer_match_list",
        "create_sitelink_extension",
        "create_callout_extension",
        "create_structured_snippet_extension",
        "create_call_extension",
        "create_image_asset",
        "link_asset_to_campaign",
        "unlink_asset_from_campaign",
        "create_pmax_asset_group",
        "apply_recommendation",
        "dismiss_recommendation",
        "create_conversion_action",
        "update_conversion_action",
        "pause_conversion_action",
        "create_campaign_from_template",
        # Specialized write tools
        "add_customer_match_members",
        "remove_customer_match_members",
        "create_demand_gen_campaign",
        "create_demand_gen_ad_group",
        "create_experiment",
        "schedule_experiment",
        "end_experiment",
        "upload_click_conversions",
        "upload_call_conversions",
        "upload_conversion_adjustments",
        "mcc_bulk_apply",
        # Governance write / approval actions
        "approve_operation",
        "reject_operation",
        "revert_operation",
        # Phase 4 guarded conversion-goal, placement, and account-negative writes
        "update_campaign_conversion_goal",
        "set_conversion_action_primary",
        "exclude_placements",
        "add_account_negative_keywords",
    }
)

MODULE_TOOLS: dict[str, frozenset[str]] = {
    "core": CORE_TOOLS,
    "diagnostics": frozenset(
        {
            "diagnose_delivery",
            "get_budget_pacing",
            "get_landing_page_performance",
            "get_demographics_performance",
            "get_assets_and_calls_performance",
        }
    ),
    "measurement": frozenset(
        {
            "audit_conversion_tracking",
            "get_offline_conversion_diagnostics",
            "get_conversion_actions",
            "get_conversion_performance",
            "get_enhanced_conversions_status",
            "check_consent_signal_coverage",
            "get_customer_match_list_stats",
            "get_offline_user_data_job_status",
        }
    ),
    "search": frozenset(
        {
            "get_search_terms_report",
            "get_keyword_performance",
            "get_impression_share_losses",
            "get_ad_strength_diagnostics",
            "get_auction_insights",
            "analyze_auction_insights_csv",
            "get_shared_sets",
            "analyze_search_terms_clusters",
            "suggest_negatives_from_clusters",
            "generate_keyword_ideas",
            "generate_keyword_historical_metrics",
            "generate_keyword_forecast",
            "find_geo_target_constants",
            "list_experiments",
            "get_experiment",
            "get_experiment_results",
        }
    ),
    "pmax": frozenset(
        {
            "get_pmax_diagnostics",
            "analyze_auction_insights_csv",
            "get_geo_performance",
            "get_device_performance",
            "get_time_of_day_performance",
            "get_audience_performance",
            "list_recommendations",
            "get_recommendation_detail",
        }
    ),
    "shopping": frozenset(
        {
            "get_shopping_performance",
            "analyze_auction_insights_csv",
            "get_geo_performance",
            "get_device_performance",
            "list_recommendations",
            "get_recommendation_detail",
        }
    ),
    "display": frozenset(
        {
            "get_placements_and_video_performance",
            "get_audience_performance",
            "get_geo_performance",
            "get_device_performance",
            "get_time_of_day_performance",
        }
    ),
    "demand-gen": frozenset(
        {
            "get_demand_gen_performance",
            "get_placements_and_video_performance",
            "get_audience_performance",
            "get_geo_performance",
            "get_device_performance",
        }
    ),
    "mcc": frozenset(
        {
            "list_client_accounts",
            "get_client_context",
            "switch_context",
            "run_across_clients",
            "generate_agency_dashboard",
            "get_audit_log",
            "get_pending_approvals",
            "list_expiring_approvals",
            "list_pending_client_approvals",
            "export_audit_log",
            "rotate_audit_log",
        }
    ),
    "operations": OPERATIONS_TOOLS,
}

PRESETS: dict[str, tuple[str, ...]] = {
    "core": ("core",),
    "leads": ("core", "diagnostics", "measurement", "search"),
    "ecommerce": ("core", "diagnostics", "measurement", "pmax", "shopping"),
    "agency": ("core", "diagnostics", "measurement", "search", "pmax", "mcc"),
    "full": (
        "core",
        "diagnostics",
        "measurement",
        "search",
        "pmax",
        "shopping",
        "display",
        "demand-gen",
        "mcc",
        "operations",
    ),
}

_SNAPSHOT_ATTR = "_agency_all_tools_snapshot"
_ACTIVE_ATTR = "_agency_active_modules"


def resolve_modules(
    *,
    modules: Iterable[str] | str | None = None,
    preset: str | None = None,
    read_only: bool | None = None,
) -> tuple[list[str], set[str], bool]:
    """Resolve active module names and allowed tool names.

    Always includes `core` and strips `operations` whenever read-only mode is active.
    """
    effective_read_only = get_settings().read_only_mode if read_only is None else read_only
    selected: list[str] = []

    raw_preset = (preset if preset is not None else os.getenv("GOOGLE_ADS_PRESET", "")).strip().lower()
    raw_modules = modules if modules is not None else os.getenv("GOOGLE_ADS_MODULES", "")

    if raw_preset:
        if raw_preset not in PRESETS:
            raise ValueError(
                f"Unknown preset '{raw_preset}'. Choose from: {', '.join(sorted(PRESETS))}"
            )
        selected.extend(PRESETS[raw_preset])

    if raw_modules:
        items = (
            [part.strip().lower() for part in raw_modules.split(",") if part.strip()]
            if isinstance(raw_modules, str)
            else [str(part).strip().lower() for part in raw_modules if str(part).strip()]
        )
        for item in items:
            if item == "all":
                selected.extend(PRESETS["full"])
            elif item in MODULE_TOOLS:
                selected.append(item)
            else:
                raise ValueError(
                    f"Unknown module '{item}'. Choose from: {', '.join(sorted(MODULE_TOOLS))}"
                )

    if not selected:
        selected = ["core"]
    elif "core" not in selected:
        selected.insert(0, "core")

    # Deduplicate preserving order
    ordered = list(dict.fromkeys(selected))
    if effective_read_only:
        ordered = [name for name in ordered if name != "operations"]

    allowed_tools: set[str] = set()
    for module_name in ordered:
        allowed_tools |= MODULE_TOOLS[module_name]
    if effective_read_only:
        allowed_tools -= OPERATIONS_TOOLS

    return ordered, allowed_tools, effective_read_only


def configure_tool_catalog(
    mcp: Any,
    *,
    modules: Iterable[str] | str | None = None,
    preset: str | None = None,
    read_only: bool | None = None,
) -> dict[str, Any]:
    """Filter registered tools on a FastMCP instance according to active modules and read-only mode."""
    tool_manager = getattr(mcp, "_tool_manager", None)
    if tool_manager is None or not hasattr(tool_manager, "_tools"):
        return {"modules": ["core"], "tool_count": 0, "read_only_mode": True}

    snapshot: dict[str, Any] | None = getattr(mcp, _SNAPSHOT_ATTR, None)
    if snapshot is None:
        snapshot = dict(tool_manager._tools)
        setattr(mcp, _SNAPSHOT_ATTR, snapshot)
    else:
        # Capture any tools added after the initial snapshot
        for name, tool_obj in tool_manager._tools.items():
            snapshot.setdefault(name, tool_obj)

    active_modules, allowed_tools, effective_read_only = resolve_modules(
        modules=modules,
        preset=preset,
        read_only=read_only,
    )

    # Restore from snapshot then keep only allowed tools (plus any custom unregistered non-write tools)
    known_catalog_tools = set().union(*MODULE_TOOLS.values())
    filtered: dict[str, Any] = {}
    for name, tool_obj in snapshot.items():
        if name in allowed_tools:
            filtered[name] = tool_obj
        elif name not in known_catalog_tools and not (
            effective_read_only and name in OPERATIONS_TOOLS
        ):
            filtered[name] = tool_obj

    tool_manager._tools = filtered
    info = {
        "modules": active_modules,
        "tool_count": len(filtered),
        "total_catalog_tools": len(snapshot),
        "read_only_mode": effective_read_only,
    }
    setattr(mcp, _ACTIVE_ATTR, info)
    return info


def get_active_catalog_info(mcp: Any) -> dict[str, Any]:
    info = getattr(mcp, _ACTIVE_ATTR, None)
    if isinstance(info, dict):
        return dict(info)
    return {
        "modules": ["core"],
        "tool_count": len(getattr(getattr(mcp, "_tool_manager", None), "_tools", {})),
        "read_only_mode": get_settings().read_only_mode,
    }
