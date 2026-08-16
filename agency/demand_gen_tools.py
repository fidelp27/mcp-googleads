"""Google Ads Demand Gen tools.

Display standalone is being sunset in favor of Demand Gen. This module
covers the operator loop that actually matters for the migration:
create the Demand Gen campaign, create ad groups with channel controls
(YouTube Shorts / in-feed / in-stream, Discover, Gmail, Display), and
read performance. Ad creation is left to the UI because the payload
surface (four ad subtypes, multi-format assets) is too broad for a
single tool to be genuinely useful in v1.
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.types import ToolAnnotations
from pydantic import Field

from .api import format_json, mutate, search
from .config import (
    client_display_name,
    ensure_customer_allowed,
    ensure_tool_allowed,
    get_settings,
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
WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)

_BIDDING_STRATEGIES = frozenset({"MAXIMIZE_CONVERSIONS", "TARGET_CPA", "TARGET_ROAS"})
_CHANNEL_STRATEGIES = frozenset({"ALL_CHANNELS", "SELECTED_CHANNELS"})
_SELECTABLE_CHANNELS = frozenset(
    {
        "youtube_shorts",
        "youtube_in_feed",
        "youtube_in_stream",
        "discover",
        "gmail",
        "display",
        "maps",
    }
)


def _positive_int(value, field: str) -> int:
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return int(text)


def _campaign_resource(customer_id: str, campaign_id: str) -> str:
    return f"customers/{customer_id}/campaigns/{_positive_int(campaign_id, 'campaign_id')}"


def _budget_resource(customer_id: str, budget_id: str) -> str:
    return (
        f"customers/{customer_id}/campaignBudgets/"
        f"{_positive_int(budget_id, 'budget_id')}"
    )


def _build_bidding_payload(
    strategy: str,
    target_cpa_micros: Optional[int],
    target_roas: Optional[float],
) -> dict[str, Any]:
    clean = (strategy or "").strip().upper()
    if clean not in _BIDDING_STRATEGIES:
        raise ValueError(
            f"bidding_strategy_type must be one of {sorted(_BIDDING_STRATEGIES)}"
        )
    if clean == "MAXIMIZE_CONVERSIONS":
        return {"maximizeConversions": {}}
    if clean == "TARGET_CPA":
        if target_cpa_micros is None:
            raise ValueError("target_cpa_micros is required for TARGET_CPA")
        cpa = int(target_cpa_micros)
        if cpa <= 0:
            raise ValueError("target_cpa_micros must be positive")
        settings = get_settings()
        if settings.max_bid_micros and cpa > settings.max_bid_micros:
            raise ValueError(
                f"target_cpa_micros {cpa} exceeds MAX_BID_MICROS={settings.max_bid_micros}"
            )
        return {"targetCpa": {"targetCpaMicros": str(cpa)}}
    if target_roas is None:
        raise ValueError("target_roas is required for TARGET_ROAS")
    try:
        roas = float(target_roas)
    except (TypeError, ValueError) as exc:
        raise ValueError("target_roas must be numeric") from exc
    if roas <= 0:
        raise ValueError("target_roas must be positive")
    return {"targetRoas": {"targetRoas": roas}}


def _build_channel_controls(
    strategy: str,
    channels: Optional[list[str]],
) -> dict[str, Any]:
    clean = (strategy or "ALL_CHANNELS").strip().upper()
    if clean not in _CHANNEL_STRATEGIES:
        raise ValueError(
            f"channel_strategy must be one of {sorted(_CHANNEL_STRATEGIES)}"
        )
    if clean == "ALL_CHANNELS":
        return {"channelStrategy": "ALL_CHANNELS"}
    if not channels:
        raise ValueError(
            "channels list is required when channel_strategy=SELECTED_CHANNELS"
        )
    normalized: dict[str, bool] = {}
    seen: set[str] = set()
    for raw in channels:
        key = str(raw).strip().lower()
        if key not in _SELECTABLE_CHANNELS:
            raise ValueError(
                f"channel '{raw}' must be one of {sorted(_SELECTABLE_CHANNELS)}"
            )
        if key in seen:
            continue
        seen.add(key)
        # Convert snake_case to lowerCamelCase for the API payload
        parts = key.split("_")
        camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
        normalized[camel] = True
    return {
        "channelStrategy": "SELECTED_CHANNELS",
        "selectedChannels": normalized,
    }


def register_demand_gen_tools(mcp) -> None:
    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_demand_gen_campaign(
        customer_id: str = Field(description="Google Ads customer ID"),
        name: str = Field(description="Campaign name (must be unique in the account)"),
        budget_id: str = Field(
            description="Existing campaign_budget ID; create one first with create_campaign_budget"
        ),
        bidding_strategy_type: str = Field(
            description="MAXIMIZE_CONVERSIONS, TARGET_CPA or TARGET_ROAS"
        ),
        target_cpa_micros: Optional[int] = Field(
            default=None,
            description="Required for TARGET_CPA. In micros (1M = 1 unit of currency)",
        ),
        target_roas: Optional[float] = Field(
            default=None,
            description="Required for TARGET_ROAS. Ratio like 3.5 = 350% ROAS target",
        ),
        dry_run: bool = Field(default=True),
        approval_id: Optional[str] = Field(default=None),
        confirmation_text: Optional[str] = Field(default=None),
    ) -> str:
        """Create a Demand Gen campaign (PAUSED by default)."""
        ensure_tool_allowed("create_demand_gen_campaign", write=True)
        cid = ensure_customer_allowed(customer_id)
        bidding = _build_bidding_payload(
            bidding_strategy_type, target_cpa_micros, target_roas
        )
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("name is required")
        campaign_payload: dict[str, Any] = {
            "name": clean_name,
            "advertisingChannelType": "DEMAND_GEN",
            "status": "PAUSED",
            "campaignBudget": _budget_resource(cid, budget_id),
            **bidding,
        }
        payload = {
            "operation": "create_demand_gen_campaign",
            "name": clean_name,
            "budget_id": str(budget_id),
            "bidding_strategy_type": bidding_strategy_type.upper(),
            "target_cpa_micros": target_cpa_micros,
            "target_roas": target_roas,
        }
        return _dispatch_demand_gen_write(
            tool_name="create_demand_gen_campaign",
            customer_id=cid,
            payload=payload,
            expected_confirmation=clean_name,
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
            executor=lambda: mutate(
                cid, "campaigns", [{"create": campaign_payload}], validate_only=False
            ),
            preview={
                "campaign_payload": campaign_payload,
            },
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_demand_gen_ad_group(
        customer_id: str = Field(description="Google Ads customer ID"),
        campaign_id: str = Field(description="Demand Gen campaign ID"),
        name: str = Field(description="Ad group name"),
        channel_strategy: str = Field(
            default="ALL_CHANNELS",
            description="ALL_CHANNELS (default) or SELECTED_CHANNELS",
        ),
        channels: Optional[list[str]] = Field(
            default=None,
            description=(
                "Required when channel_strategy=SELECTED_CHANNELS. Members: "
                "youtube_shorts, youtube_in_feed, youtube_in_stream, discover, "
                "gmail, display, maps."
            ),
        ),
        cpc_bid_micros: Optional[int] = Field(
            default=None,
            description="Optional default CPC bid at ad-group level (in micros)",
        ),
        dry_run: bool = Field(default=True),
        approval_id: Optional[str] = Field(default=None),
        confirmation_text: Optional[str] = Field(default=None),
    ) -> str:
        """Create a Demand Gen ad group (PAUSED by default) with channel controls."""
        ensure_tool_allowed("create_demand_gen_ad_group", write=True)
        cid = ensure_customer_allowed(customer_id)
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("name is required")
        channel_controls = _build_channel_controls(channel_strategy, channels)
        ad_group_payload: dict[str, Any] = {
            "name": clean_name,
            "campaign": _campaign_resource(cid, campaign_id),
            "status": "PAUSED",
            "demandGenAdGroupSettings": {
                "channelControls": channel_controls,
            },
        }
        if cpc_bid_micros is not None:
            cpc = int(cpc_bid_micros)
            if cpc <= 0:
                raise ValueError("cpc_bid_micros must be positive")
            settings = get_settings()
            if settings.max_bid_micros and cpc > settings.max_bid_micros:
                raise ValueError(
                    f"cpc_bid_micros {cpc} exceeds MAX_BID_MICROS={settings.max_bid_micros}"
                )
            ad_group_payload["cpcBidMicros"] = str(cpc)

        payload = {
            "operation": "create_demand_gen_ad_group",
            "campaign_id": str(campaign_id),
            "name": clean_name,
            "channel_strategy": channel_strategy.upper(),
            "channels": channels,
            "cpc_bid_micros": cpc_bid_micros,
        }
        return _dispatch_demand_gen_write(
            tool_name="create_demand_gen_ad_group",
            customer_id=cid,
            payload=payload,
            expected_confirmation=clean_name,
            dry_run=dry_run,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
            executor=lambda: mutate(
                cid, "adGroups", [{"create": ad_group_payload}], validate_only=False
            ),
            preview={
                "ad_group_payload": ad_group_payload,
            },
        )

    @mcp.tool(annotations=READ_ONLY)
    @audited_tool
    def get_demand_gen_performance(
        customer_id: str = Field(description="Google Ads customer ID"),
        campaign_id: Optional[str] = Field(
            default=None,
            description="Optional Demand Gen campaign ID filter",
        ),
        days: int = Field(default=30, description="Lookback window 1-365"),
    ) -> str:
        """Return performance for Demand Gen campaigns + ad groups over the window.

        Filters by `campaign.advertising_channel_type = 'DEMAND_GEN'` so the
        response is Demand Gen only even if the account also runs Search or
        Performance Max.
        """
        ensure_tool_allowed("get_demand_gen_performance")
        cid = ensure_customer_allowed(customer_id)
        if not 1 <= days <= 365:
            raise ValueError("days must be between 1 and 365")
        from datetime import date, timedelta
        end = date.today()
        start = end - timedelta(days=days - 1)
        date_clause = (
            f"segments.date BETWEEN '{start.isoformat()}' AND '{end.isoformat()}'"
        )
        campaign_filter = ""
        if campaign_id:
            campaign_filter = f" AND campaign.id = {_positive_int(campaign_id, 'campaign_id')}"

        campaigns = search(
            cid,
            f"""
            SELECT campaign.id, campaign.name, campaign.status,
                   campaign.advertising_channel_type,
                   campaign_budget.amount_micros,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM campaign
            WHERE {date_clause}
              AND campaign.advertising_channel_type = 'DEMAND_GEN'
              AND campaign.status != 'REMOVED'
              {campaign_filter}
            ORDER BY metrics.cost_micros DESC
            """,
        )
        ad_groups = search(
            cid,
            f"""
            SELECT campaign.id, campaign.name,
                   ad_group.id, ad_group.name, ad_group.status,
                   ad_group.demand_gen_ad_group_settings.channel_controls.channel_strategy,
                   metrics.impressions, metrics.clicks, metrics.cost_micros,
                   metrics.conversions, metrics.conversions_value
            FROM ad_group
            WHERE {date_clause}
              AND campaign.advertising_channel_type = 'DEMAND_GEN'
              AND ad_group.status != 'REMOVED'
              {campaign_filter}
            ORDER BY metrics.cost_micros DESC
            """,
        )

        report = _summarize_demand_gen(
            campaign_rows=campaigns, ad_group_rows=ad_groups, days=days
        )
        return format_json({
            "customer_id": cid,
            "tool": "get_demand_gen_performance",
            "fresh": True,
            "filter": {"campaign_id": campaign_id, "days": days},
            **report,
        })


def _summarize_demand_gen(
    *, campaign_rows: list[dict], ad_group_rows: list[dict], days: int
) -> dict:
    """Fold GAQL rows into a Demand Gen roll-up + per-unit breakdown."""
    campaigns: list[dict] = []
    totals = _empty_totals(days)
    for row in campaign_rows:
        campaign = row.get("campaign", {}) or {}
        metrics = row.get("metrics", {}) or {}
        budget = row.get("campaignBudget", {}) or {}
        cost_micros = int(metrics.get("costMicros") or 0)
        impressions = int(metrics.get("impressions") or 0)
        clicks = int(metrics.get("clicks") or 0)
        conversions = float(metrics.get("conversions") or 0)
        conv_value = float(metrics.get("conversionsValue") or 0)
        totals["impressions"] += impressions
        totals["clicks"] += clicks
        totals["cost_micros"] += cost_micros
        totals["conversions"] += conversions
        totals["conversions_value"] += conv_value
        campaigns.append(
            {
                "id": campaign.get("id"),
                "name": campaign.get("name"),
                "status": campaign.get("status"),
                "budget_amount_micros": int(budget.get("amountMicros") or 0),
                "impressions": impressions,
                "clicks": clicks,
                "cost_micros": cost_micros,
                "conversions": conversions,
                "conversions_value": conv_value,
                "ctr": round(clicks / impressions, 4) if impressions else 0.0,
                "cvr": round(conversions / clicks, 4) if clicks else 0.0,
                "roas": round(conv_value * 1_000_000 / cost_micros, 4)
                if cost_micros
                else 0.0,
            }
        )

    ad_groups: list[dict] = []
    for row in ad_group_rows:
        ag = row.get("adGroup", {}) or {}
        campaign = row.get("campaign", {}) or {}
        metrics = row.get("metrics", {}) or {}
        channel_strategy = None
        settings = (ag.get("demandGenAdGroupSettings") or {}).get("channelControls") or {}
        channel_strategy = settings.get("channelStrategy")
        ad_groups.append(
            {
                "id": ag.get("id"),
                "name": ag.get("name"),
                "status": ag.get("status"),
                "campaign_id": campaign.get("id"),
                "campaign_name": campaign.get("name"),
                "channel_strategy": channel_strategy,
                "impressions": int(metrics.get("impressions") or 0),
                "clicks": int(metrics.get("clicks") or 0),
                "cost_micros": int(metrics.get("costMicros") or 0),
                "conversions": float(metrics.get("conversions") or 0),
                "conversions_value": float(metrics.get("conversionsValue") or 0),
            }
        )

    return {
        "period_days": days,
        "summary": {
            "total_campaigns": len(campaigns),
            "total_ad_groups": len(ad_groups),
            "totals": totals,
        },
        "campaigns": campaigns,
        "ad_groups": ad_groups,
    }


def _empty_totals(days: int) -> dict:
    return {
        "window_days": days,
        "impressions": 0,
        "clicks": 0,
        "cost_micros": 0,
        "conversions": 0.0,
        "conversions_value": 0.0,
    }


def _dispatch_demand_gen_write(
    *,
    tool_name: str,
    customer_id: str,
    payload: dict[str, Any],
    expected_confirmation: str,
    dry_run: bool,
    approval_id: Optional[str],
    confirmation_text: Optional[str],
    executor,
    preview: dict[str, Any],
) -> str:
    settings = get_settings()
    if not dry_run and settings.read_only_mode:
        raise PermissionError("READ_ONLY_MODE=true blocks Demand Gen mutations")

    if dry_run:
        approval = create_approval(
            tool_name,
            customer_id,
            payload,
            expected_confirmation=expected_confirmation,
        )
        record_audit(
            tool_name,
            customer_id,
            payload,
            "validated",
            preview,
            dry_run=True,
            operation_id=approval["approval_id"],
        )
        return format_json(
            {
                "status": "validated",
                "executed": False,
                "approval_id": approval["approval_id"],
                "expires_at": approval["expires_at"],
                "customer_id": customer_id,
                "client_name": client_display_name(customer_id),
                "tool": tool_name,
                "expected_confirmation": expected_confirmation,
                "preview": preview,
                "next_step": (
                    f"Call {tool_name} again with the same arguments, dry_run=false, "
                    f"this approval_id and confirmation_text='{expected_confirmation}'."
                ),
            }
        )

    if not approval_id:
        raise PermissionError("approval_id is required when dry_run=false")
    validate_approval(
        approval_id,
        tool_name,
        customer_id,
        payload,
        confirmation_text,
    )
    try:
        result = executor()
        audit_id = record_audit(
            tool_name,
            customer_id,
            payload,
            "executed",
            result,
            dry_run=False,
            operation_id=approval_id,
        )
        update_approval(approval_id, status="executed", audit_id=audit_id)
        return format_json(
            {
                "status": "executed",
                "executed": True,
                "approval_id": approval_id,
                "audit_id": audit_id,
                "customer_id": customer_id,
                "client_name": client_display_name(customer_id),
                "result": result,
            }
        )
    except Exception as error:
        record_audit(
            tool_name,
            customer_id,
            payload,
            "error",
            str(error),
            dry_run=False,
            operation_id=approval_id,
        )
        raise
