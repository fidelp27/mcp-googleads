"""Unified conversion tracking audit and offline conversion upload diagnostics.

Two high-signal read-only tools for the `measurement` module:
- `audit_conversion_tracking`: unifies conversion actions, tracking ownership
  (account vs. MCC), account/campaign/custom conversion goals, Smart Bidding
  strategies, per-action volume/value, and measurement-scope diagnostics.
- `get_offline_conversion_diagnostics`: inspects offline upload summaries
  (sent, accepted, pending, errors without PII), distinguishes ingestion from
  attribution, and detects legacy integrations eligible for Data Manager API.
"""

from __future__ import annotations

from typing import Annotated, Any, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from .api import format_json, search, search as _api_search, search_with_meta
from .config import client_display_name, ensure_customer_allowed, ensure_tool_allowed
from .dates import coverage_metadata, resolve_date_range
from .errors import GoogleAdsError
from .governance import audited_tool


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

_SMART_BIDDING_TYPES = frozenset(
    {
        "MAXIMIZE_CONVERSIONS",
        "MAXIMIZE_CONVERSION_VALUE",
        "TARGET_CPA",
        "TARGET_ROAS",
    }
)

_OFFLINE_ACTION_TYPES = frozenset(
    {
        "UPLOAD_CLICKS",
        "UPLOAD_CALLS",
        "SALESFORCE",
        "STORE_SALES_DIRECT_UPLOAD",
        "STORE_SALES",
    }
)

_DATA_MANAGER_ELIGIBLE_CLIENTS = frozenset(
    {
        "GOOGLE_ADS_API",
        "GOOGLE_ADS_WEB_CLIENT",
        "GOOGLE_ADS_AUTOMATED_RULE",
        "SALESFORCE",
        "ZAPIER",
        "HUBSPOT",
    }
)


def _search_bounded(customer_id: str, query: str) -> tuple[list[dict[str, Any]], bool]:
    """Run bounded search returning `(rows, truncated)` while preserving `measurement_tools.search` monkeypatches."""
    if search is not _api_search:
        return search(customer_id, query), False
    meta = search_with_meta(customer_id, query)
    return meta["rows"], bool(meta["truncated"])


def _safe_search(customer_id: str, query: str) -> tuple[list[dict[str, Any]], str | None]:
    """Run an optional diagnostic GAQL query, returning `(rows, warning)` on unsupported resources."""
    try:
        return search(customer_id, query), None
    except GoogleAdsError as exc:
        return [], f"Optional diagnostic query unavailable ({exc.status or exc.http_status}): {exc.message or exc}"


def _safe_search_bounded(
    customer_id: str,
    query: str,
) -> tuple[list[dict[str, Any]], bool, str | None]:
    """Run an optional bounded GAQL query, returning `(rows, truncated, warning)`."""
    try:
        rows, truncated = _search_bounded(customer_id, query)
        return rows, truncated, None
    except GoogleAdsError as exc:
        return (
            [],
            False,
            f"Optional diagnostic query unavailable ({exc.status or exc.http_status}): {exc.message or exc}",
        )


def _summarize_conversion_audit(
    *,
    customer_id: str,
    customer_rows: list[dict[str, Any]],
    action_rows: list[dict[str, Any]],
    customer_goal_rows: list[dict[str, Any]],
    campaign_goal_rows: list[dict[str, Any]],
    custom_goal_rows: list[dict[str, Any]],
    campaign_custom_goal_rows: list[dict[str, Any]],
    campaign_rows: list[dict[str, Any]],
    action_metric_rows: list[dict[str, Any]],
    coverage: dict[str, Any],
    truncated_queries: list[str] | None = None,
    query_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Pure summarizer turning raw GAQL rows into a unified conversion audit."""
    truncated_set = set(truncated_queries or [])
    warnings_list = list(query_warnings or [])
    config_truncated = sorted(
        truncated_set
        & {
            "conversion_action",
            "customer_conversion_goal",
            "campaign_conversion_goal",
            "custom_conversion_goal",
            "conversion_goal_campaign_config",
        }
    )
    effective_actions_evidence_incomplete = bool(config_truncated or warnings_list)
    action_volume_truncated = "campaign_conversion_action_metrics" in truncated_set
    insufficient_data: list[dict[str, Any]] = []
    # ----- 1. Account & conversion tracking ownership -----
    customer_name = None
    tracking: dict[str, Any] = {
        "conversion_tracking_id": None,
        "status": None,
        "owner_customer": f"customers/{customer_id}",
        "centralized_by_mcc": False,
        "accepted_customer_data_terms": None,
        "enhanced_conversions_for_leads_enabled": None,
    }
    if customer_rows:
        cust = customer_rows[0].get("customer", {}) or {}
        customer_name = cust.get("descriptiveName")
        setting = cust.get("conversionTrackingSetting", {}) or {}
        owner = setting.get("googleAdsConversionCustomer") or f"customers/{customer_id}"
        tracking["conversion_tracking_id"] = setting.get("conversionTrackingId")
        tracking["status"] = setting.get("conversionTrackingStatus")
        tracking["owner_customer"] = owner
        tracking["centralized_by_mcc"] = owner != f"customers/{customer_id}"
        tracking["accepted_customer_data_terms"] = setting.get("acceptedCustomerDataTerms")
        tracking["enhanced_conversions_for_leads_enabled"] = setting.get(
            "enhancedConversionsForLeadsEnabled"
        )

    # ----- 2. Per-action volume roll-up -----
    volume_by_action: dict[str, dict[str, float]] = {}
    for row in action_metric_rows:
        seg = row.get("segments", {}) or {}
        m = row.get("metrics", {}) or {}
        resource = seg.get("conversionAction")
        if not resource:
            continue
        bucket = volume_by_action.setdefault(
            resource,
            {
                "conversions": 0.0,
                "conversions_value": 0.0,
                "all_conversions": 0.0,
                "all_conversions_value": 0.0,
            },
        )
        bucket["conversions"] += float(m.get("conversions") or 0)
        bucket["conversions_value"] += float(m.get("conversionsValue") or 0)
        bucket["all_conversions"] += float(m.get("allConversions") or 0)
        bucket["all_conversions_value"] += float(m.get("allConversionsValue") or 0)

    # ----- 3. Conversion actions catalog -----
    actions: list[dict[str, Any]] = []
    actions_by_resource: dict[str, dict[str, Any]] = {}
    for row in action_rows:
        act = row.get("conversionAction", {}) or {}
        attr = act.get("attributionModelSettings", {}) or {}
        val_settings = act.get("valueSettings", {}) or {}
        resource_name = act.get("resourceName") or (
            f"customers/{customer_id}/conversionActions/{act.get('id')}"
            if act.get("id")
            else None
        )
        vol = volume_by_action.get(resource_name or "", {})
        item = {
            "id": str(act.get("id")) if act.get("id") is not None else None,
            "resource_name": resource_name,
            "name": act.get("name"),
            "status": act.get("status"),
            "category": act.get("category"),
            "origin": act.get("origin"),
            "type": act.get("type"),
            "owner_customer": act.get("ownerCustomer") or tracking["owner_customer"],
            "primary_for_goal": bool(act.get("primaryForGoal")),
            "include_in_conversions_metric": bool(act.get("includeInConversionsMetric")),
            "counting_type": act.get("countingType"),
            "default_value": val_settings.get("defaultValue"),
            "always_use_default_value": val_settings.get("alwaysUseDefaultValue"),
            "attribution_model": attr.get("attributionModel"),
            "data_driven_model_status": attr.get("dataDrivenModelStatus"),
            "click_through_lookback_days": act.get("clickThroughLookbackWindowDays"),
            "view_through_lookback_days": act.get("viewThroughLookbackWindowDays"),
            "metrics": {
                "conversions": round(vol.get("conversions", 0.0), 4),
                "conversions_value": round(vol.get("conversions_value", 0.0), 4),
                "all_conversions": round(vol.get("all_conversions", 0.0), 4),
                "all_conversions_value": round(vol.get("all_conversions_value", 0.0), 4),
            },
        }
        actions.append(item)
        if resource_name:
            actions_by_resource[resource_name] = item

    # ----- 4. Account-default, campaign-level, and custom conversion goals -----
    account_default_goals: dict[tuple[str, str], bool] = {}
    for row in customer_goal_rows:
        g = row.get("customerConversionGoal", {}) or {}
        cat = g.get("category")
        orig = g.get("origin")
        if cat and orig:
            account_default_goals[(str(cat), str(orig))] = bool(g.get("biddable"))

    campaign_goals_map: dict[str, dict[tuple[str, str], bool]] = {}
    for row in campaign_goal_rows:
        camp = row.get("campaign", {}) or {}
        g = row.get("campaignConversionGoal", {}) or {}
        cid_ = str(camp.get("id") or "")
        cat = g.get("category")
        orig = g.get("origin")
        if cid_ and cat and orig:
            campaign_goals_map.setdefault(cid_, {})[(str(cat), str(orig))] = bool(g.get("biddable"))

    custom_goals: dict[str, dict[str, Any]] = {}
    for row in custom_goal_rows:
        cg = row.get("customConversionGoal", {}) or {}
        res = cg.get("resourceName")
        if not res:
            continue
        custom_goals[res] = {
            "resource_name": res,
            "id": str(cg.get("id")) if cg.get("id") is not None else None,
            "name": cg.get("name"),
            "status": cg.get("status"),
            "conversion_actions": list(cg.get("conversionActions") or []),
        }

    campaign_custom_goals: dict[str, list[str]] = {}
    campaign_goal_levels: dict[str, str] = {}
    for row in campaign_custom_goal_rows:
        camp = row.get("campaign", {}) or {}
        cid_ = str(camp.get("id") or "")
        cfg = row.get("conversionGoalCampaignConfig", {}) or {}
        level = cfg.get("goalConfigLevel")
        if cid_ and level:
            campaign_goal_levels[cid_] = str(level).upper()
        cg_ref = (
            cfg.get("customConversionGoal")
            or (row.get("campaignConversionGoal", {}) or {}).get("customConversionGoal")
            or (row.get("customConversionGoal", {}) or {}).get("resourceName")
            or (camp.get("selectiveOptimization", {}) or {}).get("conversionActions")
        )
        if cid_ and isinstance(cg_ref, str) and cg_ref:
            campaign_custom_goals.setdefault(cid_, []).append(cg_ref)

    # ----- 5. Evaluate what each campaign actually optimizes for -----
    alerts: list[dict[str, Any]] = []
    campaigns_report: list[dict[str, Any]] = []

    for row in campaign_rows:
        camp = row.get("campaign", {}) or {}
        m = row.get("metrics", {}) or {}
        cid_ = str(camp.get("id") or "")
        if not cid_:
            continue
        bidding_type = str(camp.get("biddingStrategyType") or "UNKNOWN")
        cost_micros = float(m.get("costMicros") or 0)
        conversions = float(m.get("conversions") or 0)
        all_conversions = float(m.get("allConversions") or 0)
        conv_value = float(m.get("conversionsValue") or 0)
        conv_by_date = float(m.get("conversionsByConversionDate") or 0)

        custom_refs = campaign_custom_goals.get(cid_, [])
        camp_goal_overrides = campaign_goals_map.get(cid_, {})
        goal_level = campaign_goal_levels.get(cid_)

        goal_mode = "account_default"
        effective_actions: list[dict[str, Any]] = []
        secondary_in_custom: list[str] = []

        if custom_refs and goal_level != "CUSTOMER":
            goal_mode = "custom_goal"
            seen_res: set[str] = set()
            for cg_res in custom_refs:
                cg_obj = custom_goals.get(cg_res)
                if not cg_obj:
                    continue
                for act_res in cg_obj["conversion_actions"]:
                    if act_res in seen_res:
                        continue
                    seen_res.add(act_res)
                    act_obj = actions_by_resource.get(act_res)
                    if act_obj and act_obj["status"] == "ENABLED":
                        effective_actions.append(
                            {
                                "id": act_obj["id"],
                                "name": act_obj["name"],
                                "category": act_obj["category"],
                                "origin": act_obj["origin"],
                                "primary_for_goal": act_obj["primary_for_goal"],
                                "via_custom_goal": cg_obj["name"],
                            }
                        )
                        if not act_obj["primary_for_goal"]:
                            secondary_in_custom.append(str(act_obj["name"] or act_obj["id"]))
        else:
            if camp_goal_overrides and goal_level != "CUSTOMER":
                goal_mode = "campaign_standard_override"
                biddable_pairs = {pair for pair, bid in camp_goal_overrides.items() if bid}
            elif account_default_goals:
                biddable_pairs = {pair for pair, bid in account_default_goals.items() if bid}
            else:
                biddable_pairs = None

            for act_obj in actions:
                if act_obj["status"] != "ENABLED" or not act_obj["primary_for_goal"]:
                    continue
                pair = (str(act_obj["category"]), str(act_obj["origin"]))
                if biddable_pairs is None or pair in biddable_pairs:
                    effective_actions.append(
                        {
                            "id": act_obj["id"],
                            "name": act_obj["name"],
                            "category": act_obj["category"],
                            "origin": act_obj["origin"],
                            "primary_for_goal": True,
                            "via_custom_goal": None,
                        }
                    )

        primary_to_all = round(conversions / all_conversions, 4) if all_conversions > 0 else None
        campaigns_report.append(
            {
                "campaign_id": cid_,
                "campaign_name": camp.get("name"),
                "status": camp.get("status"),
                "channel_type": camp.get("advertisingChannelType"),
                "bidding_strategy_type": bidding_type,
                "goal_mode": goal_mode,
                "custom_goals": [
                    custom_goals[r]["name"] for r in custom_refs if r in custom_goals
                ],
                "effective_biddable_actions": effective_actions,
                "metrics": {
                    "cost_micros": cost_micros,
                    "conversions": conversions,
                    "conversions_value": conv_value,
                    "all_conversions": all_conversions,
                    "conversions_by_conversion_date": conv_by_date,
                    "primary_to_all_ratio": primary_to_all,
                },
            }
        )

        # Campaign-level alerts
        is_smart_bidding = bidding_type in _SMART_BIDDING_TYPES
        if camp.get("status") == "ENABLED" and is_smart_bidding and not effective_actions:
            if effective_actions_evidence_incomplete:
                reason_parts: list[str] = []
                if config_truncated:
                    reason_parts.append(f"truncated queries: {', '.join(config_truncated)}")
                if warnings_list:
                    reason_parts.append("one or more optional goal queries failed")
                insufficient_data.append(
                    {
                        "code": "INSUFFICIENT_DATA_EFFECTIVE_ACTIONS",
                        "scope": "campaign",
                        "campaign_id": cid_,
                        "message": (
                            f"Cannot confirm absence of effective conversion actions for Smart Bidding campaign "
                            f"'{camp.get('name')}' ({bidding_type}) because conversion configuration is incomplete "
                            f"({'; '.join(reason_parts)}). A valid action may exist on an omitted page."
                        ),
                    }
                )
            else:
                alerts.append(
                    {
                        "severity": "critical",
                        "code": "SMART_BIDDING_WITHOUT_EFFECTIVE_ACTIONS",
                        "campaign_id": cid_,
                        "message": (
                            f"Enabled Smart Bidding campaign '{camp.get('name')}' ({bidding_type}) has "
                            "0 enabled effective conversion actions guiding bids."
                        ),
                    }
                )
        if camp.get("status") == "ENABLED" and is_smart_bidding and cost_micros > 0 and conversions == 0:
            alerts.append(
                {
                    "severity": "high",
                    "code": "SMART_BIDDING_ZERO_CONVERSIONS",
                    "campaign_id": cid_,
                    "message": (
                        f"Smart Bidding campaign '{camp.get('name')}' spent {cost_micros / 1_000_000:,.2f} "
                        f"{coverage['currency_code']} with 0 primary conversions in the window."
                    ),
                }
            )
        if secondary_in_custom:
            alerts.append(
                {
                    "severity": "info",
                    "code": "CUSTOM_GOAL_INCLUDES_SECONDARY_ACTION",
                    "campaign_id": cid_,
                    "message": (
                        f"Campaign '{camp.get('name')}' uses a custom goal that bids on secondary "
                        f"action(s): {', '.join(secondary_in_custom)}."
                    ),
                }
            )
        if primary_to_all is not None and primary_to_all < 0.3 and all_conversions >= 5:
            alerts.append(
                {
                    "severity": "info",
                    "code": "MEASUREMENT_SCOPE_DIFFERENCE",
                    "campaign_id": cid_,
                    "message": (
                        f"Campaign '{camp.get('name')}' has conversions / all_conversions = "
                        f"{round(primary_to_all * 100, 1)}%. This is a primary-vs-secondary measurement "
                        "difference and not proof of Consent Mode failure."
                    ),
                }
            )

    # ----- 6. Account-level alerts -----
    if tracking["centralized_by_mcc"]:
        alerts.append(
            {
                "severity": "info",
                "code": "MCC_CENTRALIZED_TRACKING",
                "message": (
                    f"Conversion tracking is centralized in manager account {tracking['owner_customer']}. "
                    "Verify primary/secondary action definitions in that manager account."
                ),
            }
        )

    has_lead_or_offline = any(
        a["category"] == "LEAD" or a["type"] in _OFFLINE_ACTION_TYPES for a in actions
    )
    if tracking["accepted_customer_data_terms"] is not True:
        alerts.append(
            {
                "severity": "high" if has_lead_or_offline else "warning",
                "code": "CUSTOMER_DATA_TERMS_NOT_ACCEPTED",
                "message": (
                    "Customer Data Terms are not accepted. Enhanced Conversions and Customer Match "
                    "cannot ingest user data until accepted in Google Ads > Admin > Data manager."
                ),
            }
        )
    if has_lead_or_offline and tracking["enhanced_conversions_for_leads_enabled"] is not True:
        alerts.append(
            {
                "severity": "high",
                "code": "EC4L_DISABLED_WITH_LEAD_ACTIONS",
                "message": (
                    "Lead/offline conversion actions exist, but Enhanced Conversions for Leads is "
                    "disabled at customer level."
                ),
            }
        )

    # Detect potential double-counting (multiple enabled primary actions in the same category)
    primaries_by_cat: dict[str, list[str]] = {}
    zero_volume_unverified = False
    for act in actions:
        if act["status"] == "ENABLED" and act["primary_for_goal"]:
            cat = str(act["category"] or "UNKNOWN")
            primaries_by_cat.setdefault(cat, []).append(str(act["name"] or act["id"]))
            if act["metrics"]["conversions"] == 0 and act["metrics"]["all_conversions"] == 0:
                if action_volume_truncated:
                    zero_volume_unverified = True
                else:
                    alerts.append(
                        {
                            "severity": "warning",
                            "code": "PRIMARY_ACTION_NO_VOLUME",
                            "message": (
                                f"Enabled primary action '{act['name']}' recorded 0 conversions in the window."
                            ),
                        }
                    )
    if zero_volume_unverified:
        insufficient_data.append(
            {
                "code": "INSUFFICIENT_DATA_ACTION_VOLUME",
                "scope": "conversion_action_metrics",
                "message": (
                    "Per-action conversion metrics query was truncated; cannot confirm zero-volume alerts "
                    "for enabled primary conversion actions."
                ),
            }
        )
    for cat, names in primaries_by_cat.items():
        if len(names) >= 2:
            alerts.append(
                {
                    "severity": "warning",
                    "code": "MULTIPLE_PRIMARY_ACTIONS_SAME_CATEGORY",
                    "message": (
                        f"Category '{cat}' has {len(names)} enabled primary actions ({', '.join(names)}). "
                        "Verify they do not double-count the same conversion event."
                    ),
                }
            )

    return {
        "customer_id": customer_id,
        "customer_name": customer_name or client_display_name(customer_id),
        **coverage,
        "tracking": tracking,
        "account_default_goals": [
            {"category": cat, "origin": orig, "biddable": bid}
            for (cat, orig), bid in sorted(account_default_goals.items())
        ],
        "custom_conversion_goals": list(custom_goals.values()),
        "conversion_actions": actions,
        "campaigns": campaigns_report,
        "alerts": alerts,
        "insufficient_data": insufficient_data,
        "summary": {
            "total_actions": len(actions),
            "enabled_primary_actions": sum(
                1 for a in actions if a["status"] == "ENABLED" and a["primary_for_goal"]
            ),
            "secondary_actions": sum(
                1 for a in actions if a["status"] == "ENABLED" and not a["primary_for_goal"]
            ),
            "custom_goals_count": len(custom_goals),
            "campaigns_evaluated": len(campaigns_report),
            "centralized_by_mcc": tracking["centralized_by_mcc"],
            "alert_count": len(alerts),
        },
        "notes": [
            "Effective biddable actions combine customer_conversion_goal, campaign_conversion_goal, "
            "custom_conversion_goal, and conversion_action.primary_for_goal.",
            "The ratio conversions / all_conversions reflects primary vs. all recorded conversions "
            "and is never interpreted as proof of Consent Mode status.",
        ],
    }


def _summarize_offline_diagnostics(
    *,
    customer_id: str,
    customer_rows: list[dict[str, Any]],
    offline_action_rows: list[dict[str, Any]],
    client_summary_rows: list[dict[str, Any]],
    action_summary_rows: list[dict[str, Any]],
    warnings: list[str],
    truncated_queries: list[str] | None = None,
) -> dict[str, Any]:
    """Pure summarizer for offline conversion ingestion diagnostics and Data Manager eligibility."""
    truncated_set = set(truncated_queries or [])
    insufficient_data: list[dict[str, Any]] = []
    terms_accepted = None
    ec4l_enabled = None
    if customer_rows:
        setting = (customer_rows[0].get("customer", {}) or {}).get("conversionTrackingSetting", {}) or {}
        terms_accepted = setting.get("acceptedCustomerDataTerms")
        ec4l_enabled = setting.get("enhancedConversionsForLeadsEnabled")

    client_summaries: list[dict[str, Any]] = []
    eligible_legacy_clients: list[str] = []
    total_sent = 0
    total_accepted = 0
    total_pending = 0
    latest_upload: str | None = None

    for row in client_summary_rows:
        s = row.get("offlineConversionUploadClientSummary", {}) or {}
        client_name = str(s.get("client") or "UNKNOWN")
        sent = int(s.get("totalEventCount") or 0)
        accepted = int(s.get("successfulEventCount") or 0)
        pending = int(s.get("pendingEventCount") or 0)
        failed = max(0, sent - accepted - pending)
        last_dt = s.get("lastUploadDateTime")
        if last_dt and (latest_upload is None or str(last_dt) > latest_upload):
            latest_upload = str(last_dt)
        total_sent += sent
        total_accepted += accepted
        total_pending += pending
        if client_name in _DATA_MANAGER_ELIGIBLE_CLIENTS:
            eligible_legacy_clients.append(client_name)

        raw_alerts = s.get("alerts") or []
        sanitized_alerts = [
            {
                "error": alert.get("error"),
                "error_percentage": alert.get("errorPercentage"),
            }
            for alert in raw_alerts
            if isinstance(alert, dict)
        ]
        client_summaries.append(
            {
                "client": client_name,
                "status": s.get("status"),
                "last_upload_date_time": last_dt,
                "sent_events": sent,
                "accepted_events": accepted,
                "pending_events": pending,
                "failed_events": failed,
                "acceptance_rate": round(accepted / sent, 4) if sent > 0 else None,
                "alerts": sanitized_alerts,
            }
        )

    action_summaries: list[dict[str, Any]] = []
    for row in action_summary_rows:
        s = row.get("offlineConversionUploadConversionActionSummary", {}) or {}
        sent = int(s.get("totalEventCount") or 0)
        accepted = int(s.get("successfulEventCount") or 0)
        pending = int(s.get("pendingEventCount") or 0)
        failed = max(0, sent - accepted - pending)
        raw_alerts = s.get("alerts") or []
        action_summaries.append(
            {
                "conversion_action_id": str(s.get("conversionActionId"))
                if s.get("conversionActionId") is not None
                else None,
                "conversion_action_name": s.get("conversionActionName"),
                "client": s.get("client"),
                "status": s.get("status"),
                "last_upload_date_time": s.get("lastUploadDateTime"),
                "sent_events": sent,
                "accepted_events": accepted,
                "pending_events": pending,
                "failed_events": failed,
                "acceptance_rate": round(accepted / sent, 4) if sent > 0 else None,
                "alerts": [
                    {
                        "error": alert.get("error"),
                        "error_percentage": alert.get("errorPercentage"),
                    }
                    for alert in raw_alerts
                    if isinstance(alert, dict)
                ],
            }
        )

    offline_actions = [
        {
            "id": str((r.get("conversionAction") or {}).get("id")),
            "name": (r.get("conversionAction") or {}).get("name"),
            "type": (r.get("conversionAction") or {}).get("type"),
            "status": (r.get("conversionAction") or {}).get("status"),
            "primary_for_goal": bool((r.get("conversionAction") or {}).get("primaryForGoal")),
        }
        for r in offline_action_rows
        if (r.get("conversionAction") or {}).get("type") in _OFFLINE_ACTION_TYPES
    ]

    legacy_upload_actions = [
        a["name"] for a in offline_actions if a["type"] in {"UPLOAD_CLICKS", "UPLOAD_CALLS", "SALESFORCE"}
    ]
    data_manager_eligible = bool(eligible_legacy_clients or legacy_upload_actions)

    alerts: list[dict[str, Any]] = []
    total_failed = max(0, total_sent - total_accepted - total_pending)
    if total_sent > 0 and total_failed / total_sent >= 0.1:
        alerts.append(
            {
                "severity": "high",
                "code": "HIGH_OFFLINE_UPLOAD_ERROR_RATE",
                "message": (
                    f"{total_failed} of {total_sent} uploaded events ({round(total_failed / total_sent * 100, 1)}%) "
                    "were not accepted. Inspect per-client error codes (zero PII logged)."
                ),
            }
        )
    summary_incomplete = bool(
        warnings
        or (
            truncated_set
            & {
                "offline_conversion_upload_client_summary",
                "offline_conversion_upload_conversion_action_summary",
            }
        )
    )
    if offline_actions and not client_summaries and not action_summaries:
        if summary_incomplete:
            insufficient_data.append(
                {
                    "code": "INSUFFICIENT_DATA_OFFLINE_UPLOADS",
                    "scope": "offline_upload_summaries",
                    "message": (
                        "Cannot confirm absence of recent offline uploads because one or more upload "
                        "summary queries were unavailable or truncated."
                    ),
                }
            )
        else:
            alerts.append(
                {
                    "severity": "info",
                    "code": "NO_RECENT_OFFLINE_UPLOADS",
                    "message": (
                        f"Account has {len(offline_actions)} offline conversion action(s), but no recent "
                        "upload summary records were returned."
                    ),
                }
            )
    if data_manager_eligible and terms_accepted is not True:
        alerts.append(
            {
                "severity": "high",
                "code": "DATA_MANAGER_PREREQ_TERMS_MISSING",
                "message": (
                    "Account has legacy offline upload actions eligible for Data Manager API, "
                    "but Customer Data Terms are not accepted yet."
                ),
            }
        )

    return {
        "customer_id": customer_id,
        "client_name": client_display_name(customer_id),
        "privacy_guarantee": "Summaries use internal IDs and aggregate counts only; no PII is logged or returned.",
        "offline_conversion_actions": offline_actions,
        "client_summaries": client_summaries,
        "action_summaries": action_summaries,
        "ingestion_summary": {
            "last_upload_date_time": latest_upload,
            "sent_events": total_sent,
            "accepted_events": total_accepted,
            "pending_events": total_pending,
            "failed_events": total_failed,
            "acceptance_rate": round(total_accepted / total_sent, 4) if total_sent > 0 else None,
            "note": (
                "Acceptance measures API ingestion only; attribution to ad clicks appears separately "
                "in campaign conversion metrics after processing and attribution windows."
            ),
        },
        "data_manager_eligibility": {
            "eligible_for_upgrade": data_manager_eligible,
            "legacy_clients_detected": sorted(set(eligible_legacy_clients)),
            "legacy_actions_detected": legacy_upload_actions,
            "customer_data_terms_accepted": terms_accepted is True,
            "enhanced_conversions_for_leads_enabled": ec4l_enabled is True,
            "upgrade_docs": "https://developers.google.com/data-manager/api/devguides/events/google-ads/offline/upgrade",
        },
        "alerts": alerts,
        "insufficient_data": insufficient_data,
        "coverage_complete": (not bool(warnings)) and (not bool(truncated_set)),
        "warnings": warnings,
    }


def register_measurement_tools(mcp) -> None:
    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def audit_conversion_tracking(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
        days: Annotated[int, Field(description="Lookback window (1 to 365 days)")] = 30,
        complete_days_only: Annotated[
            bool,
            Field(description="Exclude the current incomplete day in the account time zone"),
        ] = True,
        campaign_id: Annotated[
            Optional[str],
            Field(description="Optional campaign ID to filter the campaign goal analysis"),
        ] = None,
    ) -> str:
        """Unified conversion audit: actions, MCC ownership, account/campaign/custom goals, bidding strategies, and alerts."""
        ensure_tool_allowed("audit_conversion_tracking")
        cid = ensure_customer_allowed(customer_id)
        clean_camp: str | None = None
        if campaign_id is not None:
            text = str(campaign_id).strip()
            if not text.isdigit() or int(text) <= 0:
                raise ValueError("campaign_id must be a positive integer")
            clean_camp = text

        customer_rows = search(
            cid,
            """
            SELECT customer.id, customer.descriptive_name,
                   customer.currency_code, customer.time_zone,
                   customer.conversion_tracking_setting.conversion_tracking_id,
                   customer.conversion_tracking_setting.conversion_tracking_status,
                   customer.conversion_tracking_setting.google_ads_conversion_customer,
                   customer.conversion_tracking_setting.accepted_customer_data_terms,
                   customer.conversion_tracking_setting.enhanced_conversions_for_leads_enabled
            FROM customer
            LIMIT 1
            """,
        )
        cust_obj = (customer_rows[0].get("customer") or {}) if customer_rows else {}
        currency_code = str(cust_obj.get("currencyCode") or "UNKNOWN").upper()
        time_zone = cust_obj.get("timeZone")
        window = resolve_date_range(
            days=days,
            time_zone=time_zone,
            complete_days_only=complete_days_only,
        )
        coverage = coverage_metadata(currency_code=currency_code, window=window)

        truncated_queries: list[str] = []
        action_rows, actions_truncated = _search_bounded(
            cid,
            """
            SELECT conversion_action.resource_name, conversion_action.id,
                   conversion_action.name, conversion_action.status,
                   conversion_action.category, conversion_action.origin,
                   conversion_action.type, conversion_action.owner_customer,
                   conversion_action.primary_for_goal,
                   conversion_action.include_in_conversions_metric,
                   conversion_action.counting_type,
                   conversion_action.value_settings.default_value,
                   conversion_action.value_settings.always_use_default_value,
                   conversion_action.attribution_model_settings.attribution_model,
                   conversion_action.attribution_model_settings.data_driven_model_status,
                   conversion_action.click_through_lookback_window_days,
                   conversion_action.view_through_lookback_window_days
            FROM conversion_action
            WHERE conversion_action.status != 'REMOVED'
            ORDER BY conversion_action.name
            """,
        )
        if actions_truncated:
            truncated_queries.append("conversion_action")

        warnings: list[str] = []
        customer_goal_rows, customer_goals_truncated, warn = _safe_search_bounded(
            cid,
            """
            SELECT customer_conversion_goal.category,
                   customer_conversion_goal.origin,
                   customer_conversion_goal.biddable
            FROM customer_conversion_goal
            """,
        )
        if customer_goals_truncated:
            truncated_queries.append("customer_conversion_goal")
        if warn:
            warnings.append(warn)

        camp_filter = f" AND campaign.id = {clean_camp}" if clean_camp else ""
        campaign_goal_rows, campaign_goals_truncated, warn = _safe_search_bounded(
            cid,
            f"""
            SELECT campaign.id, campaign_conversion_goal.category,
                   campaign_conversion_goal.origin,
                   campaign_conversion_goal.biddable
            FROM campaign_conversion_goal
            WHERE campaign.status != 'REMOVED'{camp_filter}
            """,
        )
        if campaign_goals_truncated:
            truncated_queries.append("campaign_conversion_goal")
        if warn:
            warnings.append(warn)

        custom_goal_rows, custom_goals_truncated, warn = _safe_search_bounded(
            cid,
            """
            SELECT custom_conversion_goal.resource_name,
                   custom_conversion_goal.id,
                   custom_conversion_goal.name,
                   custom_conversion_goal.status,
                   custom_conversion_goal.conversion_actions
            FROM custom_conversion_goal
            WHERE custom_conversion_goal.status != 'REMOVED'
            """,
        )
        if custom_goals_truncated:
            truncated_queries.append("custom_conversion_goal")
        if warn:
            warnings.append(warn)

        campaign_custom_goal_rows, camp_cfg_truncated, warn = _safe_search_bounded(
            cid,
            f"""
            SELECT campaign.id,
                   conversion_goal_campaign_config.goal_config_level,
                   conversion_goal_campaign_config.custom_conversion_goal
            FROM conversion_goal_campaign_config
            WHERE campaign.status != 'REMOVED'{camp_filter}
            """,
        )
        if camp_cfg_truncated:
            truncated_queries.append("conversion_goal_campaign_config")
        if warn:
            warnings.append(warn)

        campaign_rows, camp_truncated = _search_bounded(
            cid,
            f"""
            SELECT campaign.id, campaign.name, campaign.status,
                   campaign.advertising_channel_type,
                   campaign.bidding_strategy_type,
                   metrics.cost_micros, metrics.conversions,
                   metrics.conversions_value, metrics.all_conversions,
                   metrics.conversions_by_conversion_date
            FROM campaign
            WHERE {window.gaql_clause}
              AND campaign.status != 'REMOVED'{camp_filter}
            ORDER BY metrics.cost_micros DESC
            """,
        )
        if camp_truncated:
            truncated_queries.append("campaign")

        action_metric_rows, action_metrics_truncated = _search_bounded(
            cid,
            f"""
            SELECT segments.conversion_action,
                   metrics.conversions, metrics.conversions_value,
                   metrics.all_conversions, metrics.all_conversions_value
            FROM campaign
            WHERE {window.gaql_clause}{camp_filter}
            """,
        )
        if action_metrics_truncated:
            truncated_queries.append("campaign_conversion_action_metrics")

        any_truncated = bool(truncated_queries)
        report = _summarize_conversion_audit(
            customer_id=cid,
            customer_rows=customer_rows,
            action_rows=action_rows,
            customer_goal_rows=customer_goal_rows,
            campaign_goal_rows=campaign_goal_rows,
            custom_goal_rows=custom_goal_rows,
            campaign_custom_goal_rows=campaign_custom_goal_rows,
            campaign_rows=campaign_rows,
            action_metric_rows=action_metric_rows,
            coverage=coverage,
            truncated_queries=truncated_queries,
            query_warnings=warnings,
        )
        report["truncated"] = any_truncated
        report["totals_are_partial"] = any_truncated
        report["truncated_queries"] = sorted(set(truncated_queries))
        report["coverage_complete"] = (not bool(warnings)) and (not any_truncated)
        if any_truncated:
            report["truncation_warning"] = (
                f"One or more conversion audit queries reached GOOGLE_ADS_MAX_SEARCH_ROWS "
                f"({', '.join(report['truncated_queries'])}); conversion configuration or metrics may be incomplete."
            )
        if warnings:
            report["diagnostic_warnings"] = warnings
        return format_json(report)

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_offline_conversion_diagnostics(
        customer_id: Annotated[str, Field(description="Google Ads 10-digit customer ID")],
    ) -> str:
        """Inspect offline conversion upload summaries (sent, accepted, pending, errors without PII) and Data Manager API eligibility."""
        ensure_tool_allowed("get_offline_conversion_diagnostics")
        cid = ensure_customer_allowed(customer_id)
        warnings: list[str] = []
        truncated_queries: list[str] = []

        customer_rows = search(
            cid,
            """
            SELECT customer.id,
                   customer.conversion_tracking_setting.accepted_customer_data_terms,
                   customer.conversion_tracking_setting.enhanced_conversions_for_leads_enabled
            FROM customer
            LIMIT 1
            """,
        )
        offline_action_rows, offline_actions_truncated = _search_bounded(
            cid,
            """
            SELECT conversion_action.id, conversion_action.name,
                   conversion_action.type, conversion_action.status,
                   conversion_action.primary_for_goal
            FROM conversion_action
            WHERE conversion_action.status != 'REMOVED'
            """,
        )
        if offline_actions_truncated:
            truncated_queries.append("conversion_action")

        client_summary_rows, client_sum_truncated, warn = _safe_search_bounded(
            cid,
            """
            SELECT offline_conversion_upload_client_summary.client,
                   offline_conversion_upload_client_summary.status,
                   offline_conversion_upload_client_summary.total_event_count,
                   offline_conversion_upload_client_summary.successful_event_count,
                   offline_conversion_upload_client_summary.pending_event_count,
                   offline_conversion_upload_client_summary.last_upload_date_time,
                   offline_conversion_upload_client_summary.alerts
            FROM offline_conversion_upload_client_summary
            """,
        )
        if client_sum_truncated:
            truncated_queries.append("offline_conversion_upload_client_summary")
        if warn:
            warnings.append(warn)

        action_summary_rows, action_sum_truncated, warn = _safe_search_bounded(
            cid,
            """
            SELECT offline_conversion_upload_conversion_action_summary.conversion_action_id,
                   offline_conversion_upload_conversion_action_summary.conversion_action_name,
                   offline_conversion_upload_conversion_action_summary.client,
                   offline_conversion_upload_conversion_action_summary.status,
                   offline_conversion_upload_conversion_action_summary.total_event_count,
                   offline_conversion_upload_conversion_action_summary.successful_event_count,
                   offline_conversion_upload_conversion_action_summary.pending_event_count,
                   offline_conversion_upload_conversion_action_summary.last_upload_date_time,
                   offline_conversion_upload_conversion_action_summary.alerts
            FROM offline_conversion_upload_conversion_action_summary
            """,
        )
        if action_sum_truncated:
            truncated_queries.append("offline_conversion_upload_conversion_action_summary")
        if warn:
            warnings.append(warn)

        any_truncated = bool(truncated_queries)
        report = _summarize_offline_diagnostics(
            customer_id=cid,
            customer_rows=customer_rows,
            offline_action_rows=offline_action_rows,
            client_summary_rows=client_summary_rows,
            action_summary_rows=action_summary_rows,
            warnings=warnings,
            truncated_queries=truncated_queries,
        )
        report["truncated"] = any_truncated
        report["totals_are_partial"] = any_truncated
        report["truncated_queries"] = sorted(set(truncated_queries))
        report["coverage_complete"] = (not bool(warnings)) and (not any_truncated)
        if any_truncated:
            report["truncation_warning"] = (
                f"One or more offline conversion queries reached GOOGLE_ADS_MAX_SEARCH_ROWS "
                f"({', '.join(report['truncated_queries'])}); upload diagnostics may be incomplete."
            )
        return format_json(report)
