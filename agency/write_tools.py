"""Agency write tools backed by a uniform dry-run and approval workflow."""

from __future__ import annotations

import base64
import json
from datetime import date
from typing import Any, Optional

from mcp.types import ToolAnnotations

from .api import search
from .config import client_display_name, ensure_customer_allowed, get_settings, load_json_config
from .governance import audited_tool
from .mutations import execute_mutation


WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)


def _id(value: str | int, field: str) -> str:
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return text


def _resource(customer_id: str, collection: str, value: str | int) -> str:
    return f"customers/{ensure_customer_allowed(customer_id)}/{collection}/{_id(value, collection)}"


def _update_operation(resource: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    return {"update": resource, "updateMask": ",".join(fields)}


def _write(
    tool: str,
    customer_id: str,
    service: str,
    operations: list[dict[str, Any]],
    dry_run: bool,
    approval_id: str | None,
    confirmation_text: str | None,
    *,
    expected_confirmation: str | None = None,
    previous_state: Any = None,
    idempotency_key: str | None = None,
    batch: bool = False,
) -> str:
    return execute_mutation(
        tool_name=tool,
        customer_id=customer_id,
        service=service,
        operations=operations,
        dry_run=dry_run,
        approval_id=approval_id,
        confirmation_text=confirmation_text,
        expected_confirmation=expected_confirmation,
        previous_state=previous_state,
        idempotency_key=idempotency_key,
        batch=batch,
    )


def _first(customer_id: str, query: str) -> dict[str, Any]:
    rows = search(customer_id, query)
    if not rows:
        raise ValueError("Requested Google Ads resource was not found")
    return rows[0]


def _campaign_context(customer_id: str, campaign_id: str | int) -> dict[str, Any]:
    campaign_id = _id(campaign_id, "campaign_id")
    return _first(
        customer_id,
        f"""
        SELECT campaign.id, campaign.name, campaign.status,
               campaign.advertising_channel_type,
               campaign.bidding_strategy_type,
               campaign.target_cpa.target_cpa_micros,
               campaign.target_roas.target_roas,
               campaign.start_date, campaign.end_date,
               campaign.campaign_budget,
               campaign_budget.id, campaign_budget.name,
               campaign_budget.amount_micros
        FROM campaign
        WHERE campaign.id = {campaign_id}
        LIMIT 1
        """,
    )


def _ad_group_context(customer_id: str, ad_group_id: str | int) -> dict[str, Any]:
    ad_group_id = _id(ad_group_id, "ad_group_id")
    return _first(
        customer_id,
        f"""
        SELECT ad_group.id, ad_group.name, ad_group.status,
               ad_group.cpc_bid_micros, campaign.id, campaign.name
        FROM ad_group
        WHERE ad_group.id = {ad_group_id}
        LIMIT 1
        """,
    )


def _enum(value: str, field: str, allowed: set[str]) -> str:
    normalized = value.strip().upper()
    if normalized not in allowed:
        raise ValueError(f"{field} must be one of: {', '.join(sorted(allowed))}")
    return normalized


def _bidding_fields(strategy: str, target_value: float | None) -> dict[str, Any]:
    strategy = _enum(
        strategy,
        "bidding_strategy",
        {
            "MANUAL_CPC",
            "MAXIMIZE_CONVERSIONS",
            "TARGET_CPA",
            "TARGET_ROAS",
            "MAXIMIZE_CONVERSION_VALUE",
        },
    )
    if strategy == "MANUAL_CPC":
        return {"manualCpc": {"enhancedCpcEnabled": False}}
    if strategy == "MAXIMIZE_CONVERSIONS":
        return {"maximizeConversions": {}}
    if strategy == "MAXIMIZE_CONVERSION_VALUE":
        return {"maximizeConversionValue": {}}
    if target_value is None or target_value <= 0:
        raise ValueError(f"target_value is required for {strategy}")
    if strategy == "TARGET_CPA":
        return {"targetCpa": {"targetCpaMicros": str(int(target_value))}}
    return {"targetRoas": {"targetRoas": float(target_value)}}


def _campaign_create(
    customer_id: str,
    name: str,
    budget_id: str,
    channel_type: str,
    bidding_strategy: str,
    target_value: float | None,
    start_date: str | None,
    end_date: str | None,
    status: str,
    force_enable: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not name.strip():
        raise ValueError("name is required")
    effective_status = "ENABLED" if force_enable and status.upper() == "ENABLED" else "PAUSED"
    if channel_type == "PERFORMANCE_MAX":
        strategy = bidding_strategy.strip().upper()
        if strategy in {"MANUAL_CPC", "TARGET_CPA", "TARGET_ROAS"}:
            if strategy == "MANUAL_CPC":
                raise ValueError("Performance Max does not support MANUAL_CPC")
            if target_value is None or target_value <= 0:
                raise ValueError(f"target_value is required for {strategy}")
            bidding = (
                {"maximizeConversions": {"targetCpaMicros": str(int(target_value))}}
                if strategy == "TARGET_CPA"
                else {"maximizeConversionValue": {"targetRoas": float(target_value)}}
            )
        else:
            bidding = _bidding_fields(strategy, target_value)
    else:
        bidding = _bidding_fields(bidding_strategy, target_value)
    campaign: dict[str, Any] = {
        "name": name.strip(),
        "status": effective_status,
        "advertisingChannelType": channel_type,
        "campaignBudget": _resource(customer_id, "campaignBudgets", budget_id),
        # Required by Google Ads API v25+. Advertisers must declare EU political
        # advertising status; standard commercial campaigns do not contain it.
        "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        **bidding,
    }
    if channel_type in {"SEARCH", "DISPLAY"}:
        campaign["networkSettings"] = {
            "targetGoogleSearch": channel_type == "SEARCH",
            "targetSearchNetwork": channel_type == "SEARCH",
            "targetContentNetwork": channel_type == "DISPLAY",
            "targetPartnerSearchNetwork": False,
        }
    # Google Ads REST expects ISO dates (YYYY-MM-DD). start_date is optional and
    # defaults to the campaign creation date when omitted, so only send it when given.
    if start_date:
        campaign["startDate"] = start_date
    if end_date:
        campaign["endDate"] = end_date
    if extra:
        campaign.update(extra)
    return campaign


def _register_campaign_tools(mcp) -> None:
    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_campaign_budget(
        customer_id: str,
        name: str,
        amount_micros: int,
        delivery_method: str = "STANDARD",
        is_shared: bool = False,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Validate or create a campaign budget with hard safety caps."""
        if amount_micros <= 0:
            raise ValueError("amount_micros must be positive")
        cap = get_settings().max_daily_budget_increase_micros
        if cap and amount_micros > cap:
            raise PermissionError(f"Budget exceeds configured cap of {cap} micros")
        operation = {
            "create": {
                "name": name.strip(),
                "amountMicros": str(amount_micros),
                "deliveryMethod": _enum(
                    delivery_method, "delivery_method", {"STANDARD", "ACCELERATED"}
                ),
                "explicitlyShared": bool(is_shared),
            }
        }
        return _write(
            "create_campaign_budget",
            customer_id,
            "campaignBudgets",
            [operation],
            dry_run,
            approval_id,
            confirmation_text,
            expected_confirmation=name.strip(),
            idempotency_key=idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def update_campaign_budget(
        customer_id: str,
        budget_id: str,
        new_amount_micros: int,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Validate or change a campaign budget with percentage and absolute caps."""
        if new_amount_micros <= 0:
            raise ValueError("new_amount_micros must be positive")
        row = _first(
            customer_id,
            f"""
            SELECT campaign_budget.id, campaign_budget.name,
                   campaign_budget.amount_micros
            FROM campaign_budget
            WHERE campaign_budget.id = {_id(budget_id, 'budget_id')}
            LIMIT 1
            """,
        )
        budget = row["campaignBudget"]
        current = int(budget.get("amountMicros", 0))
        increase = max(0, new_amount_micros - current)
        settings = get_settings()
        pct = (increase / current * 100) if current else 100
        if settings.max_daily_budget_increase_micros and increase > settings.max_daily_budget_increase_micros:
            raise PermissionError("Budget increase exceeds MAX_DAILY_BUDGET_INCREASE_MICROS")
        if increase and pct > settings.max_budget_increase_pct:
            raise PermissionError(
                "Budget increase exceeds MAX_BUDGET_INCREASE_PCT"
            )
        if increase and pct > 50:
            expected = str(budget.get("name") or budget_id)
        else:
            expected = None
        operation = _update_operation(
            {
                "resourceName": _resource(customer_id, "campaignBudgets", budget_id),
                "amountMicros": str(new_amount_micros),
            },
            ["amount_micros"],
        )
        return _write(
            "update_campaign_budget",
            customer_id,
            "campaignBudgets",
            [operation],
            dry_run,
            approval_id,
            confirmation_text,
            expected_confirmation=expected,
            previous_state=budget,
        )

    def create_campaign(
        tool_name: str,
        customer_id: str,
        name: str,
        budget_id: str,
        channel: str,
        bidding_strategy: str,
        target_value: float | None,
        start_date: str | None,
        end_date: str | None,
        geo_targets: list[str],
        language_codes: list[str],
        preset: str | None,
        status: str,
        force_enable: bool,
        dry_run: bool,
        approval_id: str | None,
        confirmation_text: str | None,
        idempotency_key: str | None,
        extra: dict[str, Any] | None = None,
        campaign_assets: list[tuple[str, str]] | None = None,
    ) -> str:
        if preset:
            presets = load_json_config("presets.json", {})
            if preset not in presets:
                raise ValueError(f"Unknown preset: {preset}")
            preset_config = presets[preset]
            geo_targets = geo_targets or list(preset_config.get("geo_targets", []))
            language_codes = language_codes or list(
                preset_config.get("language_codes", [])
            )
        campaign = _campaign_create(
            customer_id,
            name,
            budget_id,
            channel,
            bidding_strategy,
            target_value,
            start_date,
            end_date,
            status,
            force_enable,
            extra,
        )
        customer_id = ensure_customer_allowed(customer_id)
        temporary_campaign = f"customers/{customer_id}/campaigns/-1"
        campaign["resourceName"] = temporary_campaign
        operations: list[dict[str, Any]] = [{"campaignOperation": {"create": campaign}}]
        for location in geo_targets:
            location_id = _id(location, "geo_target")
            operations.append(
                {
                    "campaignCriterionOperation": {
                        "create": {
                            "campaign": temporary_campaign,
                            "location": {
                                "geoTargetConstant": f"geoTargetConstants/{location_id}"
                            },
                            "negative": False,
                        }
                    }
                }
            )
        for language in language_codes:
            language_id = _id(language, "language_code")
            operations.append(
                {
                    "campaignCriterionOperation": {
                        "create": {
                            "campaign": temporary_campaign,
                            "language": {
                                "languageConstant": f"languageConstants/{language_id}"
                            },
                            "negative": False,
                        }
                    }
                }
            )
        for asset_id, field_type in campaign_assets or []:
            operations.append(
                {
                    "campaignAssetOperation": {
                        "create": {
                            "campaign": temporary_campaign,
                            "asset": _resource(customer_id, "assets", asset_id),
                            "fieldType": field_type,
                        }
                    }
                }
            )
        return _write(
            tool_name,
            customer_id,
            "googleAds",
            operations,
            dry_run,
            approval_id,
            confirmation_text,
            expected_confirmation=name.strip(),
            idempotency_key=idempotency_key,
            batch=True,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_search_campaign(
        customer_id: str,
        name: str,
        budget_id: str,
        bidding_strategy: str = "MANUAL_CPC",
        target_value: Optional[float] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        geo_targets: list[str] = [],
        language_codes: list[str] = [],
        preset: Optional[str] = None,
        status: str = "PAUSED",
        force_enable: bool = False,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create a Search campaign, paused unless force_enable is explicitly confirmed."""
        return create_campaign(
            "create_search_campaign", customer_id, name, budget_id, "SEARCH",
            bidding_strategy, target_value, start_date, end_date, geo_targets,
            language_codes, preset, status, force_enable, dry_run, approval_id,
            confirmation_text, idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_pmax_campaign(
        customer_id: str,
        name: str,
        budget_id: str,
        bidding_strategy: str = "MAXIMIZE_CONVERSION_VALUE",
        target_value: Optional[float] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        geo_targets: list[str] = [],
        language_codes: list[str] = [],
        preset: Optional[str] = None,
        final_url_expansion: bool = True,
        feed_id: Optional[str] = None,
        business_name_asset_id: Optional[str] = None,
        logo_asset_id: Optional[str] = None,
        landscape_logo_asset_id: Optional[str] = None,
        status: str = "PAUSED",
        force_enable: bool = False,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create a Performance Max campaign, paused by default."""
        extra: dict[str, Any] = {
            "brandGuidelinesEnabled": True,
            "assetAutomationSettings": [
                {
                    "assetAutomationType": (
                        "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION"
                    ),
                    "assetAutomationStatus": (
                        "OPTED_IN" if final_url_expansion else "OPTED_OUT"
                    ),
                }
            ],
        }
        if not all(
            [business_name_asset_id, logo_asset_id, landscape_logo_asset_id]
        ):
            raise ValueError(
                "Google Ads v25 requires business_name_asset_id, logo_asset_id "
                "and landscape_logo_asset_id for new Performance Max campaigns"
            )
        campaign_assets = [
            (business_name_asset_id, "BUSINESS_NAME"),
            (logo_asset_id, "LOGO"),
            (landscape_logo_asset_id, "LANDSCAPE_LOGO"),
        ]
        if feed_id:
            extra["shoppingSetting"] = {
                "merchantId": _id(feed_id, "feed_id"),
                "campaignPriority": 0,
                "enableLocal": False,
            }
        return create_campaign(
            "create_pmax_campaign", customer_id, name, budget_id,
            "PERFORMANCE_MAX", bidding_strategy, target_value, start_date,
            end_date, geo_targets, language_codes, preset, status, force_enable,
            dry_run, approval_id,
            confirmation_text, idempotency_key, extra, campaign_assets,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_display_campaign(
        customer_id: str,
        name: str,
        budget_id: str,
        bidding_strategy: str = "MAXIMIZE_CONVERSIONS",
        target_value: Optional[float] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        geo_targets: list[str] = [],
        language_codes: list[str] = [],
        preset: Optional[str] = None,
        status: str = "PAUSED",
        force_enable: bool = False,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create a Display campaign, paused by default."""
        return create_campaign(
            "create_display_campaign", customer_id, name, budget_id, "DISPLAY",
            bidding_strategy, target_value, start_date, end_date, geo_targets,
            language_codes, preset, status, force_enable, dry_run, approval_id,
            confirmation_text, idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def update_campaign(
        customer_id: str,
        campaign_id: str,
        updates: dict[str, Any],
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Update allowed campaign fields; bidding changes require exact-name confirmation."""
        allowed = {"name", "status", "end_date", "bidding_strategy", "target_value"}
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"Unsupported campaign updates: {sorted(unknown)}")
        context = _campaign_context(customer_id, campaign_id)
        campaign_before = context["campaign"]
        resource: dict[str, Any] = {
            "resourceName": _resource(customer_id, "campaigns", campaign_id)
        }
        mask: list[str] = []
        if "name" in updates:
            resource["name"] = str(updates["name"]).strip()
            mask.append("name")
        if "status" in updates:
            resource["status"] = _enum(
                str(updates["status"]), "status", {"ENABLED", "PAUSED"}
            )
            mask.append("status")
        if "end_date" in updates:
            resource["endDate"] = str(updates["end_date"]).replace("-", "")
            mask.append("end_date")
        if "bidding_strategy" in updates:
            bidding = _bidding_fields(
                str(updates["bidding_strategy"]), updates.get("target_value")
            )
            resource.update(bidding)
            mask.extend(
                {
                    "manualCpc": "manual_cpc",
                    "maximizeConversions": "maximize_conversions",
                    "maximizeConversionValue": "maximize_conversion_value",
                    "targetCpa": "target_cpa",
                    "targetRoas": "target_roas",
                }[key]
                for key in bidding
            )
        expected = (
            str(campaign_before.get("name"))
            if "bidding_strategy" in updates or updates.get("status") == "ENABLED"
            else None
        )
        return _write(
            "update_campaign", customer_id, "campaigns",
            [_update_operation(resource, mask)], dry_run, approval_id,
            confirmation_text, expected_confirmation=expected,
            previous_state=campaign_before,
        )

    def campaign_status(
        tool: str,
        customer_id: str,
        campaign_id: str,
        status: str,
        dry_run: bool,
        approval_id: str | None,
        confirmation_text: str | None,
        expected: str | None,
    ) -> str:
        context = _campaign_context(customer_id, campaign_id)
        campaign = context["campaign"]
        budget = context.get("campaignBudget", {})
        if expected == "enable":
            expected_text = f"{campaign.get('name')} | {budget.get('amountMicros', 'unknown')}"
        elif expected == "remove":
            expected_text = str(campaign.get("name"))
        else:
            expected_text = None
        operation = _update_operation(
            {
                "resourceName": _resource(customer_id, "campaigns", campaign_id),
                "status": status,
            },
            ["status"],
        )
        return _write(
            tool, customer_id, "campaigns", [operation], dry_run, approval_id,
            confirmation_text, expected_confirmation=expected_text,
            previous_state=campaign,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def pause_campaign(
        customer_id: str,
        campaign_id: str,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Pause a campaign after a validate-only preview."""
        return campaign_status(
            "pause_campaign", customer_id, campaign_id, "PAUSED",
            dry_run, approval_id, confirmation_text, None,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def enable_campaign(
        customer_id: str,
        campaign_id: str,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Enable a campaign after confirming its exact name and daily budget."""
        return campaign_status(
            "enable_campaign", customer_id, campaign_id, "ENABLED",
            dry_run, approval_id, confirmation_text, "enable",
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    @audited_tool
    def remove_campaign(
        customer_id: str,
        campaign_id: str,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Soft-delete a campaign after exact-name confirmation."""
        return campaign_status(
            "remove_campaign", customer_id, campaign_id, "REMOVED",
            dry_run, approval_id, confirmation_text, "remove",
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def update_campaign_geo_targets(
        customer_id: str,
        campaign_id: str,
        add_locations: list[str] = [],
        remove_locations: list[str] = [],
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Add or remove campaign geographic criteria."""
        campaign = _resource(customer_id, "campaigns", campaign_id)
        operations: list[dict[str, Any]] = []
        for location in add_locations:
            operations.append(
                {
                    "create": {
                        "campaign": campaign,
                        "location": {
                            "geoTargetConstant": f"geoTargetConstants/{_id(location, 'location')}"
                        },
                        "negative": False,
                    }
                }
            )
        if remove_locations:
            ids = ", ".join(_id(value, "location") for value in remove_locations)
            rows = search(
                customer_id,
                f"""
                SELECT campaign_criterion.resource_name,
                       campaign_criterion.location.geo_target_constant
                FROM campaign_criterion
                WHERE campaign.id = {_id(campaign_id, 'campaign_id')}
                  AND campaign_criterion.type = LOCATION
                  AND campaign_criterion.criterion_id IN ({ids})
                """,
            )
            operations.extend(
                {"remove": row["campaignCriterion"]["resourceName"]} for row in rows
            )
        if not operations:
            raise ValueError("At least one location must be added or removed")
        return _write(
            "update_campaign_geo_targets", customer_id, "campaignCriteria",
            operations, dry_run, approval_id, confirmation_text,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def update_campaign_schedule(
        customer_id: str,
        campaign_id: str,
        schedule: list[dict[str, Any]],
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Replace a campaign ad schedule with validated time ranges."""
        campaign_id = _id(campaign_id, "campaign_id")
        existing = search(
            customer_id,
            f"""
            SELECT campaign_criterion.resource_name
            FROM campaign_criterion
            WHERE campaign.id = {campaign_id}
              AND campaign_criterion.type = AD_SCHEDULE
            """,
        )
        operations: list[dict[str, Any]] = [
            {"remove": row["campaignCriterion"]["resourceName"]} for row in existing
        ]
        for item in schedule:
            start_hour = int(item["start_hour"])
            end_hour = int(item["end_hour"])
            if not (0 <= start_hour <= 23 and 1 <= end_hour <= 24 and start_hour < end_hour):
                raise ValueError("Schedule hours must form a valid increasing range")
            create: dict[str, Any] = {
                "campaign": _resource(customer_id, "campaigns", campaign_id),
                "adSchedule": {
                    "dayOfWeek": _enum(
                        str(item["day"]),
                        "day",
                        {
                            "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY",
                            "FRIDAY", "SATURDAY", "SUNDAY",
                        },
                    ),
                    "startHour": start_hour,
                    "startMinute": "ZERO",
                    "endHour": end_hour,
                    "endMinute": "ZERO",
                },
            }
            if item.get("bid_modifier") is not None:
                create["bidModifier"] = float(item["bid_modifier"])
            operations.append({"create": create})
        return _write(
            "update_campaign_schedule", customer_id, "campaignCriteria",
            operations, dry_run, approval_id, confirmation_text,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def copy_campaign(
        customer_id: str,
        source_campaign_id: str,
        new_name: str,
        include_ads: bool = True,
        include_keywords: bool = True,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Clone a campaign, ad groups, responsive ads and keywords into PAUSED state."""
        source_id = _id(source_campaign_id, "source_campaign_id")
        source = _campaign_context(customer_id, source_id)
        campaign_data = source["campaign"]
        customer_id = ensure_customer_allowed(customer_id)
        temp_campaign = f"customers/{customer_id}/campaigns/-1"
        create_campaign = {
            "resourceName": temp_campaign,
            "name": new_name.strip(),
            "status": "PAUSED",
            "advertisingChannelType": campaign_data.get("advertisingChannelType", "SEARCH"),
            "campaignBudget": campaign_data["campaignBudget"],
            "startDate": date.today().strftime("%Y%m%d"),
        }
        if campaign_data.get("endDate"):
            create_campaign["endDate"] = campaign_data["endDate"]
        strategy_type = campaign_data.get("biddingStrategyType", "MANUAL_CPC")
        strategy_fields: dict[str, dict[str, Any]] = {
            "MANUAL_CPC": {"manualCpc": {"enhancedCpcEnabled": False}},
            "MAXIMIZE_CONVERSIONS": {"maximizeConversions": {}},
            "MAXIMIZE_CONVERSION_VALUE": {"maximizeConversionValue": {}},
        }
        target_cpa = campaign_data.get("targetCpa", {}).get("targetCpaMicros")
        target_roas = campaign_data.get("targetRoas", {}).get("targetRoas")
        strategy_fields["TARGET_CPA"] = (
            {"targetCpa": {"targetCpaMicros": target_cpa}}
            if target_cpa
            else {"maximizeConversions": {}}
        )
        strategy_fields["TARGET_ROAS"] = (
            {"targetRoas": {"targetRoas": target_roas}}
            if target_roas
            else {"maximizeConversionValue": {}}
        )
        create_campaign.update(strategy_fields.get(strategy_type, {"manualCpc": {}}))
        operations: list[dict[str, Any]] = [
            {"campaignOperation": {"create": create_campaign}}
        ]
        groups = search(
            customer_id,
            f"""
            SELECT ad_group.id, ad_group.name, ad_group.type,
                   ad_group.cpc_bid_micros
            FROM ad_group
            WHERE campaign.id = {source_id} AND ad_group.status != REMOVED
            """,
        )
        group_map: dict[str, str] = {}
        next_temp = -2
        for row in groups:
            group = row["adGroup"]
            temp_group = f"customers/{customer_id}/adGroups/{next_temp}"
            next_temp -= 1
            group_map[str(group["id"])] = temp_group
            create_group = {
                "resourceName": temp_group,
                "campaign": temp_campaign,
                "name": group["name"],
                "status": "PAUSED",
                "type": group.get("type", "SEARCH_STANDARD"),
            }
            if group.get("cpcBidMicros"):
                create_group["cpcBidMicros"] = group["cpcBidMicros"]
            operations.append({"adGroupOperation": {"create": create_group}})
        if include_keywords and group_map:
            keywords = search(
                customer_id,
                f"""
                SELECT ad_group.id, ad_group_criterion.keyword.text,
                       ad_group_criterion.keyword.match_type,
                       ad_group_criterion.cpc_bid_micros
                FROM keyword_view
                WHERE campaign.id = {source_id}
                  AND ad_group_criterion.status != REMOVED
                """,
            )
            for row in keywords:
                criterion = row["adGroupCriterion"]
                create = {
                    "adGroup": group_map[str(row["adGroup"]["id"])],
                    "status": "PAUSED",
                    "keyword": criterion["keyword"],
                }
                if criterion.get("cpcBidMicros"):
                    create["cpcBidMicros"] = criterion["cpcBidMicros"]
                operations.append({"adGroupCriterionOperation": {"create": create}})
        if include_ads and group_map:
            ads = search(
                customer_id,
                f"""
                SELECT ad_group.id, ad_group_ad.ad.final_urls,
                       ad_group_ad.ad.responsive_search_ad.headlines,
                       ad_group_ad.ad.responsive_search_ad.descriptions,
                       ad_group_ad.ad.responsive_search_ad.path1,
                       ad_group_ad.ad.responsive_search_ad.path2
                FROM ad_group_ad
                WHERE campaign.id = {source_id}
                  AND ad_group_ad.status != REMOVED
                  AND ad_group_ad.ad.type = RESPONSIVE_SEARCH_AD
                """,
            )
            for row in ads:
                source_ad = row["adGroupAd"]["ad"]
                replacement_ad: dict[str, Any] = {
                    "finalUrls": source_ad.get("finalUrls", []),
                    "responsiveSearchAd": source_ad["responsiveSearchAd"],
                }
                operations.append(
                    {
                        "adGroupAdOperation": {
                            "create": {
                                "adGroup": group_map[str(row["adGroup"]["id"])],
                                "status": "PAUSED",
                                "ad": replacement_ad,
                            }
                        }
                    }
                )
        return _write(
            "copy_campaign", customer_id, "googleAds", operations, dry_run,
            approval_id, confirmation_text, expected_confirmation=new_name.strip(),
            idempotency_key=idempotency_key, batch=True,
        )


def _register_ad_group_tools(mcp) -> None:
    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_ad_group(
        customer_id: str,
        campaign_id: str,
        name: str,
        cpc_bid_micros: Optional[int] = None,
        type: str = "SEARCH_STANDARD",
        status: str = "PAUSED",
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create an ad group in PAUSED state by default."""
        create: dict[str, Any] = {
            "campaign": _resource(customer_id, "campaigns", campaign_id),
            "name": name.strip(),
            "status": "PAUSED" if status.upper() != "ENABLED" else "PAUSED",
            "type": type.strip().upper(),
        }
        if cpc_bid_micros is not None:
            if cpc_bid_micros <= 0 or cpc_bid_micros > get_settings().max_bid_micros:
                raise PermissionError("cpc_bid_micros is invalid or exceeds MAX_BID_MICROS")
            create["cpcBidMicros"] = str(cpc_bid_micros)
        return _write(
            "create_ad_group", customer_id, "adGroups", [{"create": create}],
            dry_run, approval_id, confirmation_text,
            expected_confirmation=name.strip(), idempotency_key=idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def update_ad_group(
        customer_id: str,
        ad_group_id: str,
        updates: dict[str, Any],
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Update ad group name, status or default CPC bid."""
        allowed = {"name", "status", "cpc_bid_micros"}
        if set(updates) - allowed:
            raise ValueError("updates may only contain name, status and cpc_bid_micros")
        before = _ad_group_context(customer_id, ad_group_id)["adGroup"]
        resource = {"resourceName": _resource(customer_id, "adGroups", ad_group_id)}
        mask: list[str] = []
        if "name" in updates:
            resource["name"] = str(updates["name"]).strip()
            mask.append("name")
        if "status" in updates:
            resource["status"] = _enum(
                str(updates["status"]), "status", {"ENABLED", "PAUSED"}
            )
            mask.append("status")
        if "cpc_bid_micros" in updates:
            bid = int(updates["cpc_bid_micros"])
            if bid <= 0 or bid > get_settings().max_bid_micros:
                raise PermissionError("Bid is invalid or exceeds MAX_BID_MICROS")
            resource["cpcBidMicros"] = str(bid)
            mask.append("cpc_bid_micros")
        expected = str(before.get("name")) if updates.get("status") == "ENABLED" else None
        return _write(
            "update_ad_group", customer_id, "adGroups",
            [_update_operation(resource, mask)], dry_run, approval_id,
            confirmation_text, expected_confirmation=expected,
            previous_state=before,
        )

    def set_status(
        tool: str,
        customer_id: str,
        ad_group_id: str,
        status: str,
        dry_run: bool,
        approval_id: str | None,
        confirmation_text: str | None,
        confirm: bool,
    ) -> str:
        before = _ad_group_context(customer_id, ad_group_id)["adGroup"]
        operation = _update_operation(
            {
                "resourceName": _resource(customer_id, "adGroups", ad_group_id),
                "status": status,
            },
            ["status"],
        )
        return _write(
            tool, customer_id, "adGroups", [operation], dry_run, approval_id,
            confirmation_text,
            expected_confirmation=str(before.get("name")) if confirm else None,
            previous_state=before,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def pause_ad_group(
        customer_id: str, ad_group_id: str, dry_run: bool = True,
        approval_id: Optional[str] = None, confirmation_text: Optional[str] = None,
    ) -> str:
        """Pause an ad group."""
        return set_status(
            "pause_ad_group", customer_id, ad_group_id, "PAUSED",
            dry_run, approval_id, confirmation_text, False,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def enable_ad_group(
        customer_id: str, ad_group_id: str, dry_run: bool = True,
        approval_id: Optional[str] = None, confirmation_text: Optional[str] = None,
    ) -> str:
        """Enable an ad group after name confirmation."""
        return set_status(
            "enable_ad_group", customer_id, ad_group_id, "ENABLED",
            dry_run, approval_id, confirmation_text, True,
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    @audited_tool
    def remove_ad_group(
        customer_id: str, ad_group_id: str, dry_run: bool = True,
        approval_id: Optional[str] = None, confirmation_text: Optional[str] = None,
    ) -> str:
        """Remove an ad group after exact-name confirmation."""
        return set_status(
            "remove_ad_group", customer_id, ad_group_id, "REMOVED",
            dry_run, approval_id, confirmation_text, True,
        )


def _text_asset(value: str | dict[str, Any], max_length: int, field: str) -> dict[str, Any]:
    if isinstance(value, str):
        text = value
        pinned = None
    else:
        text = str(value.get("text", ""))
        pinned = value.get("pinned_field")
    if not text or len(text) > max_length:
        raise ValueError(f"{field} must contain 1 to {max_length} characters")
    asset: dict[str, Any] = {"text": text}
    if pinned:
        asset["pinnedField"] = str(pinned).upper()
    return asset


def _register_ad_tools(mcp) -> None:
    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_responsive_search_ad(
        customer_id: str,
        ad_group_id: str,
        headlines: list[Any],
        descriptions: list[Any],
        final_urls: list[str],
        path1: Optional[str] = None,
        path2: Optional[str] = None,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create an enabled responsive search ad with Google length constraints."""
        if not 3 <= len(headlines) <= 15:
            raise ValueError("headlines must contain between 3 and 15 entries")
        if not 2 <= len(descriptions) <= 4:
            raise ValueError("descriptions must contain between 2 and 4 entries")
        if not final_urls:
            raise ValueError("At least one final URL is required")
        rsa: dict[str, Any] = {
            "headlines": [_text_asset(item, 30, "headline") for item in headlines],
            "descriptions": [
                _text_asset(item, 90, "description") for item in descriptions
            ],
        }
        if path1:
            if len(path1) > 15:
                raise ValueError("path1 cannot exceed 15 characters")
            rsa["path1"] = path1
        if path2:
            if len(path2) > 15:
                raise ValueError("path2 cannot exceed 15 characters")
            rsa["path2"] = path2
        operation = {
            "create": {
                "adGroup": _resource(customer_id, "adGroups", ad_group_id),
                "status": "ENABLED",
                "ad": {"finalUrls": final_urls, "responsiveSearchAd": rsa},
            }
        }
        return _write(
            "create_responsive_search_ad", customer_id, "adGroupAds", [operation],
            dry_run, approval_id, confirmation_text,
            idempotency_key=idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_call_ad(
        customer_id: str,
        ad_group_id: str,
        country_code: str,
        phone_number: str,
        business_name: str,
        headline1: str,
        headline2: str,
        description1: str,
        description2: str,
        final_url: str,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create an enabled call ad."""
        operation = {
            "create": {
                "adGroup": _resource(customer_id, "adGroups", ad_group_id),
                "status": "ENABLED",
                "ad": {
                    "finalUrls": [final_url],
                    "callAd": {
                        "countryCode": country_code.upper(),
                        "phoneNumber": phone_number,
                        "businessName": business_name,
                        "headline1": headline1,
                        "headline2": headline2,
                        "description1": description1,
                        "description2": description2,
                    },
                },
            }
        }
        return _write(
            "create_call_ad", customer_id, "adGroupAds", [operation], dry_run,
            approval_id, confirmation_text, idempotency_key=idempotency_key,
        )

    def ad_status(
        tool: str,
        customer_id: str,
        ad_group_id: str,
        ad_id: str,
        status: str,
        dry_run: bool,
        approval_id: str | None,
        confirmation_text: str | None,
        confirm: bool,
    ) -> str:
        resource_name = (
            f"customers/{ensure_customer_allowed(customer_id)}/adGroupAds/"
            f"{_id(ad_group_id, 'ad_group_id')}~{_id(ad_id, 'ad_id')}"
        )
        operation = _update_operation(
            {"resourceName": resource_name, "status": status}, ["status"]
        )
        return _write(
            tool, customer_id, "adGroupAds", [operation], dry_run, approval_id,
            confirmation_text, expected_confirmation=resource_name if confirm else None,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def pause_ad(
        customer_id: str, ad_group_id: str, ad_id: str, dry_run: bool = True,
        approval_id: Optional[str] = None, confirmation_text: Optional[str] = None,
    ) -> str:
        """Pause an ad."""
        return ad_status(
            "pause_ad", customer_id, ad_group_id, ad_id, "PAUSED",
            dry_run, approval_id, confirmation_text, False,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def enable_ad(
        customer_id: str, ad_group_id: str, ad_id: str, dry_run: bool = True,
        approval_id: Optional[str] = None, confirmation_text: Optional[str] = None,
    ) -> str:
        """Enable an ad."""
        return ad_status(
            "enable_ad", customer_id, ad_group_id, ad_id, "ENABLED",
            dry_run, approval_id, confirmation_text, False,
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    @audited_tool
    def remove_ad(
        customer_id: str, ad_group_id: str, ad_id: str, dry_run: bool = True,
        approval_id: Optional[str] = None, confirmation_text: Optional[str] = None,
    ) -> str:
        """Remove an ad after confirming its exact resource name."""
        return ad_status(
            "remove_ad", customer_id, ad_group_id, ad_id, "REMOVED",
            dry_run, approval_id, confirmation_text, True,
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    @audited_tool
    def update_ad(
        customer_id: str,
        ad_group_id: str,
        old_ad_id: str,
        replacement_ad: dict[str, Any],
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Replace immutable ad content by creating a new ad and removing the old one."""
        old_resource = (
            f"customers/{ensure_customer_allowed(customer_id)}/adGroupAds/"
            f"{_id(ad_group_id, 'ad_group_id')}~{_id(old_ad_id, 'old_ad_id')}"
        )
        operations = [
            {
                "adGroupAdOperation": {
                    "create": {
                        "adGroup": _resource(customer_id, "adGroups", ad_group_id),
                        "status": "ENABLED",
                        "ad": replacement_ad,
                    }
                }
            },
            {"adGroupAdOperation": {"remove": old_resource}},
        ]
        return _write(
            "update_ad", customer_id, "googleAds", operations, dry_run,
            approval_id, confirmation_text, expected_confirmation=old_resource,
            idempotency_key=idempotency_key, batch=True,
        )


def _register_keyword_tools(mcp) -> None:
    @mcp.tool(annotations=WRITE)
    @audited_tool
    def add_keywords_to_ad_group(
        customer_id: str,
        ad_group_id: str,
        keywords: list[dict[str, Any]],
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Add keyword criteria in one idempotent batch."""
        if not keywords:
            raise ValueError("keywords cannot be empty")
        operations: list[dict[str, Any]] = []
        for keyword in keywords:
            text = str(keyword.get("text", "")).strip()
            if not text:
                raise ValueError("Every keyword requires non-empty text")
            create: dict[str, Any] = {
                "adGroup": _resource(customer_id, "adGroups", ad_group_id),
                "status": "ENABLED",
                "keyword": {
                    "text": text,
                    "matchType": _enum(
                        str(keyword.get("match_type", "BROAD")),
                        "match_type",
                        {"EXACT", "PHRASE", "BROAD"},
                    ),
                },
            }
            if keyword.get("cpc_bid_micros") is not None:
                bid = int(keyword["cpc_bid_micros"])
                if bid <= 0 or bid > get_settings().max_bid_micros:
                    raise PermissionError("Keyword bid exceeds MAX_BID_MICROS")
                create["cpcBidMicros"] = str(bid)
            operations.append({"create": create})
        return _write(
            "add_keywords_to_ad_group", customer_id, "adGroupCriteria",
            operations, dry_run, approval_id, confirmation_text,
            idempotency_key=idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def update_keyword_bid(
        customer_id: str,
        ad_group_id: str,
        ad_group_criterion_id: str,
        new_bid_micros: int,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Update a keyword bid and require confirmation above 3x group average."""
        if new_bid_micros <= 0 or new_bid_micros > get_settings().max_bid_micros:
            raise PermissionError("new_bid_micros is invalid or exceeds MAX_BID_MICROS")
        context = _ad_group_context(customer_id, ad_group_id)["adGroup"]
        keyword_rows = search(
            customer_id,
            f"""
            SELECT ad_group_criterion.cpc_bid_micros
            FROM ad_group_criterion
            WHERE ad_group.id = {_id(ad_group_id, 'ad_group_id')}
              AND ad_group_criterion.type = KEYWORD
              AND ad_group_criterion.status != REMOVED
            """,
        )
        bids = [
            int(row.get("adGroupCriterion", {}).get("cpcBidMicros", 0))
            for row in keyword_rows
            if row.get("adGroupCriterion", {}).get("cpcBidMicros")
        ]
        average = sum(bids) / len(bids) if bids else int(
            context.get("cpcBidMicros", 0) or 0
        )
        expected = (
            str(context.get("name"))
            if average and new_bid_micros > average * 3
            else None
        )
        resource_name = (
            f"customers/{ensure_customer_allowed(customer_id)}/adGroupCriteria/"
            f"{_id(ad_group_id, 'ad_group_id')}~"
            f"{_id(ad_group_criterion_id, 'ad_group_criterion_id')}"
        )
        operation = _update_operation(
            {"resourceName": resource_name, "cpcBidMicros": str(new_bid_micros)},
            ["cpc_bid_micros"],
        )
        return _write(
            "update_keyword_bid", customer_id, "adGroupCriteria", [operation],
            dry_run, approval_id, confirmation_text,
            expected_confirmation=expected,
        )

    def keyword_status(
        tool: str,
        customer_id: str,
        ad_group_id: str,
        criterion_id: str,
        status: str,
        dry_run: bool,
        approval_id: str | None,
        confirmation_text: str | None,
        destructive: bool,
    ) -> str:
        resource_name = (
            f"customers/{ensure_customer_allowed(customer_id)}/adGroupCriteria/"
            f"{_id(ad_group_id, 'ad_group_id')}~{_id(criterion_id, 'criterion_id')}"
        )
        operation = _update_operation(
            {"resourceName": resource_name, "status": status}, ["status"]
        )
        return _write(
            tool, customer_id, "adGroupCriteria", [operation], dry_run,
            approval_id, confirmation_text,
            expected_confirmation=resource_name if destructive else None,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def pause_keyword(
        customer_id: str, ad_group_id: str, criterion_id: str,
        dry_run: bool = True, approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Pause a keyword."""
        return keyword_status(
            "pause_keyword", customer_id, ad_group_id, criterion_id, "PAUSED",
            dry_run, approval_id, confirmation_text, False,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def enable_keyword(
        customer_id: str, ad_group_id: str, criterion_id: str,
        dry_run: bool = True, approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Enable a keyword."""
        return keyword_status(
            "enable_keyword", customer_id, ad_group_id, criterion_id, "ENABLED",
            dry_run, approval_id, confirmation_text, False,
        )

    @mcp.tool(annotations=DESTRUCTIVE)
    @audited_tool
    def remove_keyword(
        customer_id: str, ad_group_id: str, criterion_id: str,
        dry_run: bool = True, approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Remove a keyword after exact resource confirmation."""
        return keyword_status(
            "remove_keyword", customer_id, ad_group_id, criterion_id, "REMOVED",
            dry_run, approval_id, confirmation_text, True,
        )

    def negative_operation(
        customer_id: str,
        level: str,
        parent_id: str,
        text: str,
        match_type: str,
    ) -> tuple[str, dict[str, Any]]:
        level = _enum(level, "level", {"CAMPAIGN", "AD_GROUP"})
        keyword = {
            "text": text.strip(),
            "matchType": _enum(match_type, "match_type", {"EXACT", "PHRASE", "BROAD"}),
        }
        if not keyword["text"]:
            raise ValueError("keyword_text cannot be empty")
        if level == "CAMPAIGN":
            return "campaignCriteria", {
                "create": {
                    "campaign": _resource(customer_id, "campaigns", parent_id),
                    "negative": True,
                    "keyword": keyword,
                }
            }
        return "adGroupCriteria", {
            "create": {
                "adGroup": _resource(customer_id, "adGroups", parent_id),
                "negative": True,
                "status": "ENABLED",
                "keyword": keyword,
            }
        }

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def add_negative_keyword(
        customer_id: str,
        level: str,
        parent_id: str,
        keyword_text: str,
        match_type: str = "PHRASE",
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Add one negative keyword at campaign or ad-group level."""
        service, operation = negative_operation(
            customer_id, level, parent_id, keyword_text, match_type
        )
        return _write(
            "add_negative_keyword", customer_id, service, [operation], dry_run,
            approval_id, confirmation_text, idempotency_key=idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def add_negative_keywords_bulk(
        customer_id: str,
        level: str,
        parent_id: str,
        keywords: list[dict[str, str]],
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Add multiple negative keywords in one validated batch."""
        if not keywords:
            raise ValueError("keywords cannot be empty")
        service: str | None = None
        operations: list[dict[str, Any]] = []
        for keyword in keywords:
            current_service, operation = negative_operation(
                customer_id,
                level,
                parent_id,
                keyword.get("text", ""),
                keyword.get("match_type", "PHRASE"),
            )
            if service and service != current_service:
                raise ValueError("All bulk negatives must use the same level")
            service = current_service
            operations.append(operation)
        return _write(
            "add_negative_keywords_bulk", customer_id, service or "campaignCriteria",
            operations, dry_run, approval_id, confirmation_text,
            idempotency_key=idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_negative_keyword_list(
        customer_id: str,
        name: str,
        keywords: list[dict[str, str]] = [],
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create a shared negative keyword list and optional members."""
        customer_id = ensure_customer_allowed(customer_id)
        temporary = f"customers/{customer_id}/sharedSets/-1"
        operations: list[dict[str, Any]] = [
            {
                "sharedSetOperation": {
                    "create": {
                        "resourceName": temporary,
                        "name": name.strip(),
                        "type": "NEGATIVE_KEYWORDS",
                    }
                }
            }
        ]
        for keyword in keywords:
            operations.append(
                {
                    "sharedCriterionOperation": {
                        "create": {
                            "sharedSet": temporary,
                            "keyword": {
                                "text": str(keyword.get("text", "")).strip(),
                                "matchType": _enum(
                                    keyword.get("match_type", "PHRASE"),
                                    "match_type",
                                    {"EXACT", "PHRASE", "BROAD"},
                                ),
                            },
                        }
                    }
                }
            )
        return _write(
            "create_negative_keyword_list", customer_id, "googleAds", operations,
            dry_run, approval_id, confirmation_text,
            expected_confirmation=name.strip(), idempotency_key=idempotency_key,
            batch=True,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def attach_negative_keyword_list_to_campaign(
        customer_id: str,
        shared_set_id: str,
        campaign_id: str,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Attach a shared negative keyword list to a campaign."""
        operation = {
            "create": {
                "campaign": _resource(customer_id, "campaigns", campaign_id),
                "sharedSet": _resource(customer_id, "sharedSets", shared_set_id),
            }
        }
        return _write(
            "attach_negative_keyword_list_to_campaign", customer_id,
            "campaignSharedSets", [operation], dry_run, approval_id,
            confirmation_text,
        )


def _register_audience_tools(mcp) -> None:
    @mcp.tool(annotations=WRITE)
    @audited_tool
    def add_audience_to_ad_group(
        customer_id: str,
        ad_group_id: str,
        audience_id: str,
        mode: str = "OBSERVATION",
        audience_type: str = "USER_LIST",
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Add a user-list or user-interest criterion in targeting or observation mode."""
        mode = _enum(mode, "mode", {"TARGETING", "OBSERVATION"})
        audience_type = _enum(
            audience_type, "audience_type", {"USER_LIST", "USER_INTEREST"}
        )
        customer_id = ensure_customer_allowed(customer_id)
        ad_group = _resource(customer_id, "adGroups", ad_group_id)
        criterion: dict[str, Any] = {"adGroup": ad_group, "status": "ENABLED"}
        if audience_type == "USER_LIST":
            criterion["userList"] = {
                "userList": _resource(customer_id, "userLists", audience_id)
            }
        else:
            criterion["userInterest"] = {
                "userInterestCategory": f"userInterests/{_id(audience_id, 'audience_id')}"
            }
        operations = [
            {"adGroupCriterionOperation": {"create": criterion}},
            {
                "adGroupOperation": {
                    "update": {
                        "resourceName": ad_group,
                        "targetingSetting": {
                            "targetRestrictions": [
                                {
                                    "targetingDimension": "AUDIENCE",
                                    "bidOnly": mode == "OBSERVATION",
                                }
                            ]
                        },
                    },
                    "updateMask": "targeting_setting.target_restrictions",
                }
            },
        ]
        return _write(
            "add_audience_to_ad_group", customer_id, "googleAds", operations,
            dry_run, approval_id, confirmation_text, batch=True,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def remove_audience_from_ad_group(
        customer_id: str,
        ad_group_id: str,
        criterion_id: str,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Remove an audience criterion from an ad group."""
        resource_name = (
            f"customers/{ensure_customer_allowed(customer_id)}/adGroupCriteria/"
            f"{_id(ad_group_id, 'ad_group_id')}~{_id(criterion_id, 'criterion_id')}"
        )
        return _write(
            "remove_audience_from_ad_group", customer_id, "adGroupCriteria",
            [{"remove": resource_name}], dry_run, approval_id, confirmation_text,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def update_bid_modifier(
        customer_id: str,
        dimension_type: str,
        criterion_id: str,
        modifier_pct: float,
        campaign_id: Optional[str] = None,
        ad_group_id: Optional[str] = None,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Adjust an existing device, geo, audience or schedule criterion bid modifier."""
        if not -90 <= modifier_pct <= 900:
            raise ValueError("modifier_pct must be between -90 and 900")
        dimension_type = _enum(
            dimension_type,
            "dimension_type",
            {"DEVICE", "LOCATION", "AUDIENCE", "SCHEDULE"},
        )
        modifier = 1 + (modifier_pct / 100)
        if ad_group_id:
            collection = "adGroupCriteria"
            service = "adGroupCriteria"
            resource_name = (
                f"customers/{ensure_customer_allowed(customer_id)}/{collection}/"
                f"{_id(ad_group_id, 'ad_group_id')}~{_id(criterion_id, 'criterion_id')}"
            )
        elif campaign_id:
            collection = "campaignCriteria"
            service = "campaignCriteria"
            resource_name = (
                f"customers/{ensure_customer_allowed(customer_id)}/{collection}/"
                f"{_id(campaign_id, 'campaign_id')}~{_id(criterion_id, 'criterion_id')}"
            )
        else:
            raise ValueError("campaign_id or ad_group_id is required")
        operation = _update_operation(
            {"resourceName": resource_name, "bidModifier": modifier},
            ["bid_modifier"],
        )
        expected = resource_name if modifier_pct > 100 or modifier_pct < -50 else None
        return _write(
            "update_bid_modifier", customer_id, service, [operation], dry_run,
            approval_id, confirmation_text, expected_confirmation=expected,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_customer_match_list(
        customer_id: str,
        name: str,
        upload_key_type: str,
        member_lifespan_days: int = 540,
        consent_confirmed: bool = False,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create an empty Customer Match list only after consent is confirmed."""
        if not consent_confirmed:
            raise PermissionError(
                "consent_confirmed=true is required for Customer Match data"
            )
        if not 0 <= member_lifespan_days <= 540:
            raise ValueError("member_lifespan_days must be between 0 and 540")
        operation = {
            "create": {
                "name": name.strip(),
                "membershipStatus": "OPEN",
                "membershipLifeSpan": member_lifespan_days,
                "crmBasedUserList": {
                    "uploadKeyType": _enum(
                        upload_key_type,
                        "upload_key_type",
                        {
                            "CONTACT_INFO",
                            "CRM_ID",
                            "MOBILE_ADVERTISING_ID",
                        },
                    )
                },
                "description": "Created through google-ads-mcp with operator consent",
            }
        }
        return _write(
            "create_customer_match_list", customer_id, "userLists", [operation],
            dry_run, approval_id, confirmation_text,
            expected_confirmation=name.strip(), idempotency_key=idempotency_key,
        )


def _register_asset_tools(mcp) -> None:
    def create_asset(
        tool: str,
        customer_id: str,
        name: str,
        asset_payload: dict[str, Any],
        dry_run: bool,
        approval_id: str | None,
        confirmation_text: str | None,
        idempotency_key: str | None,
    ) -> str:
        return _write(
            tool, customer_id, "assets",
            [{"create": {"name": name.strip(), **asset_payload}}],
            dry_run, approval_id, confirmation_text,
            idempotency_key=idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_sitelink_extension(
        customer_id: str,
        name: str,
        link_text: str,
        final_urls: list[str],
        description1: Optional[str] = None,
        description2: Optional[str] = None,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create a sitelink asset."""
        sitelink: dict[str, Any] = {"linkText": link_text}
        if description1:
            sitelink["description1"] = description1
        if description2:
            sitelink["description2"] = description2
        return create_asset(
            "create_sitelink_extension", customer_id, name,
            {"finalUrls": final_urls, "sitelinkAsset": sitelink},
            dry_run, approval_id, confirmation_text, idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_callout_extension(
        customer_id: str,
        name: str,
        callout_text: str,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create a callout asset."""
        return create_asset(
            "create_callout_extension", customer_id, name,
            {"calloutAsset": {"calloutText": callout_text}},
            dry_run, approval_id, confirmation_text, idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_structured_snippet_extension(
        customer_id: str,
        name: str,
        header: str,
        values: list[str],
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create a structured snippet asset."""
        if len(values) < 3:
            raise ValueError("Structured snippets require at least three values")
        return create_asset(
            "create_structured_snippet_extension", customer_id, name,
            {"structuredSnippetAsset": {"header": header, "values": values}},
            dry_run, approval_id, confirmation_text, idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_call_extension(
        customer_id: str,
        name: str,
        country_code: str,
        phone_number: str,
        call_conversion_action_id: Optional[str] = None,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create a call asset."""
        call_asset: dict[str, Any] = {
            "countryCode": country_code.upper(),
            "phoneNumber": phone_number,
        }
        if call_conversion_action_id:
            call_asset["callConversionAction"] = _resource(
                customer_id, "conversionActions", call_conversion_action_id
            )
        return create_asset(
            "create_call_extension", customer_id, name,
            {"callAsset": call_asset}, dry_run, approval_id,
            confirmation_text, idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_image_asset(
        customer_id: str,
        name: str,
        image_url: Optional[str] = None,
        image_base64: Optional[str] = None,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create an image asset from a public URL or base64 content."""
        if bool(image_url) == bool(image_base64):
            raise ValueError("Provide exactly one of image_url or image_base64")
        data = image_base64
        if image_url:
            import requests

            response = requests.get(image_url, timeout=30)
            response.raise_for_status()
            if len(response.content) > 5 * 1024 * 1024:
                raise ValueError("Image exceeds the 5 MB safety limit")
            data = base64.b64encode(response.content).decode("ascii")
        if not data:
            raise ValueError("Image data is empty")
        try:
            base64.b64decode(data, validate=True)
        except ValueError as error:
            raise ValueError("image_base64 is not valid base64") from error
        return create_asset(
            "create_image_asset", customer_id, name,
            {"imageAsset": {"data": data}}, dry_run, approval_id,
            confirmation_text, idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def link_asset_to_campaign(
        customer_id: str,
        campaign_id: str,
        asset_id: str,
        field_type: str,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Link an existing asset to a campaign."""
        operation = {
            "create": {
                "campaign": _resource(customer_id, "campaigns", campaign_id),
                "asset": _resource(customer_id, "assets", asset_id),
                "fieldType": field_type.strip().upper(),
            }
        }
        return _write(
            "link_asset_to_campaign", customer_id, "campaignAssets", [operation],
            dry_run, approval_id, confirmation_text,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def unlink_asset_from_campaign(
        customer_id: str,
        campaign_id: str,
        asset_id: str,
        field_type: str,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Unlink an asset from a campaign."""
        resource_name = (
            f"customers/{ensure_customer_allowed(customer_id)}/campaignAssets/"
            f"{_id(campaign_id, 'campaign_id')}~{_id(asset_id, 'asset_id')}~"
            f"{field_type.strip().upper()}"
        )
        return _write(
            "unlink_asset_from_campaign", customer_id, "campaignAssets",
            [{"remove": resource_name}], dry_run, approval_id, confirmation_text,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_pmax_asset_group(
        customer_id: str,
        campaign_id: str,
        name: str,
        final_urls: list[str],
        headlines: list[str],
        long_headlines: list[str],
        descriptions: list[str],
        business_name: str,
        image_asset_ids: list[str],
        logo_asset_ids: list[str] = [],
        video_asset_ids: list[str] = [],
        link_brand_assets: bool = False,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create a Performance Max asset group and link text/media assets."""
        if not final_urls or not headlines or not descriptions or not image_asset_ids:
            raise ValueError(
                "final_urls, headlines, descriptions and image_asset_ids are required"
            )
        customer_id = ensure_customer_allowed(customer_id)
        temp_group = f"customers/{customer_id}/assetGroups/-1"
        operations: list[dict[str, Any]] = [
            {
                "assetGroupOperation": {
                    "create": {
                        "resourceName": temp_group,
                        "campaign": _resource(customer_id, "campaigns", campaign_id),
                        "name": name.strip(),
                        "finalUrls": final_urls,
                        "status": "PAUSED",
                    }
                }
            }
        ]
        temp_id = -2
        text_specs = (
            [(text, "HEADLINE", 30) for text in headlines]
            + [(text, "LONG_HEADLINE", 90) for text in long_headlines]
            + [(text, "DESCRIPTION", 90) for text in descriptions]
        )
        for text, field_type, max_length in text_specs:
            if not text or len(text) > max_length:
                raise ValueError(f"{field_type} text exceeds {max_length} characters")
            temp_asset = f"customers/{customer_id}/assets/{temp_id}"
            temp_id -= 1
            operations.append(
                {
                    "assetOperation": {
                        "create": {
                            "resourceName": temp_asset,
                            "textAsset": {"text": text},
                        }
                    }
                }
            )
            operations.append(
                {
                    "assetGroupAssetOperation": {
                        "create": {
                            "assetGroup": temp_group,
                            "asset": temp_asset,
                            "fieldType": field_type,
                        }
                    }
                }
            )
        if link_brand_assets:
            if not business_name or len(business_name) > 25:
                raise ValueError("business_name must contain 1 to 25 characters")
            if len(logo_asset_ids) < 2:
                raise ValueError(
                    "link_brand_assets requires square and landscape logo asset IDs"
                )
            business_asset = f"customers/{customer_id}/assets/{temp_id}"
            temp_id -= 1
            operations.extend(
                [
                    {
                        "assetOperation": {
                            "create": {
                                "resourceName": business_asset,
                                "textAsset": {"text": business_name},
                            }
                        }
                    },
                    {
                        "campaignAssetOperation": {
                            "create": {
                                "campaign": _resource(
                                    customer_id, "campaigns", campaign_id
                                ),
                                "asset": business_asset,
                                "fieldType": "BUSINESS_NAME",
                            }
                        }
                    },
                ]
            )
            for index, logo_id in enumerate(logo_asset_ids[:2]):
                operations.append(
                    {
                        "campaignAssetOperation": {
                            "create": {
                                "campaign": _resource(
                                    customer_id, "campaigns", campaign_id
                                ),
                                "asset": _resource(customer_id, "assets", logo_id),
                                "fieldType": (
                                    "LOGO" if index == 0 else "LANDSCAPE_LOGO"
                                ),
                            }
                        }
                    }
                )
        media = (
            [(value, "MARKETING_IMAGE") for value in image_asset_ids]
            + [(value, "YOUTUBE_VIDEO") for value in video_asset_ids]
        )
        for asset_id, field_type in media:
            operations.append(
                {
                    "assetGroupAssetOperation": {
                        "create": {
                            "assetGroup": temp_group,
                            "asset": _resource(customer_id, "assets", asset_id),
                            "fieldType": field_type,
                        }
                    }
                }
            )
        return _write(
            "create_pmax_asset_group", customer_id, "googleAds", operations,
            dry_run, approval_id, confirmation_text,
            expected_confirmation=name.strip(), idempotency_key=idempotency_key,
            batch=True,
        )


def _register_recommendation_tools(mcp) -> None:
    @mcp.tool(annotations=WRITE)
    @audited_tool
    def apply_recommendation(
        customer_id: str,
        recommendation_id: str,
        apply_parameters: Optional[dict[str, Any]] = None,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Apply a Google Ads recommendation after exact resource confirmation."""
        resource_name = (
            recommendation_id
            if recommendation_id.startswith("customers/")
            else (
                f"customers/{ensure_customer_allowed(customer_id)}/recommendations/"
                f"{recommendation_id.strip()}"
            )
        )
        operation: dict[str, Any] = {"resourceName": resource_name}
        if apply_parameters:
            operation.update(apply_parameters)
        return _write(
            "apply_recommendation", customer_id, "recommendations", [operation],
            dry_run, approval_id, confirmation_text,
            expected_confirmation=resource_name,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def dismiss_recommendation(
        customer_id: str,
        recommendation_id: str,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Dismiss an active Google Ads recommendation."""
        resource_name = (
            recommendation_id
            if recommendation_id.startswith("customers/")
            else (
                f"customers/{ensure_customer_allowed(customer_id)}/recommendations/"
                f"{recommendation_id.strip()}"
            )
        )
        return _write(
            "dismiss_recommendation", customer_id, "recommendations",
            [{"resourceName": resource_name}], dry_run, approval_id,
            confirmation_text,
        )


def _register_conversion_tools(mcp) -> None:
    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_conversion_action(
        customer_id: str,
        name: str,
        category: str,
        value_settings: dict[str, Any],
        counting_type: str = "ONE_PER_CLICK",
        click_through_lookback_window_days: int = 30,
        view_through_lookback_window_days: int = 1,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create a website conversion action."""
        operation = {
            "create": {
                "name": name.strip(),
                "category": category.strip().upper(),
                "type": "WEBPAGE",
                "status": "ENABLED",
                "countingType": _enum(
                    counting_type,
                    "counting_type",
                    {"ONE_PER_CLICK", "MANY_PER_CLICK"},
                ),
                "valueSettings": value_settings,
                "clickThroughLookbackWindowDays": click_through_lookback_window_days,
                "viewThroughLookbackWindowDays": view_through_lookback_window_days,
            }
        }
        return _write(
            "create_conversion_action", customer_id, "conversionActions",
            [operation], dry_run, approval_id, confirmation_text,
            expected_confirmation=name.strip(), idempotency_key=idempotency_key,
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def update_conversion_action(
        customer_id: str,
        conversion_action_id: str,
        updates: dict[str, Any],
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Update supported conversion action fields."""
        mapping = {
            "name": "name",
            "status": "status",
            "category": "category",
            "counting_type": "countingType",
            "value_settings": "valueSettings",
            "click_through_lookback_window_days": "clickThroughLookbackWindowDays",
            "view_through_lookback_window_days": "viewThroughLookbackWindowDays",
        }
        unknown = set(updates) - set(mapping)
        if unknown:
            raise ValueError(f"Unsupported conversion updates: {sorted(unknown)}")
        resource = {
            "resourceName": _resource(
                customer_id, "conversionActions", conversion_action_id
            )
        }
        mask: list[str] = []
        for source, target in mapping.items():
            if source in updates:
                resource[target] = updates[source]
                mask.append(source)
        return _write(
            "update_conversion_action", customer_id, "conversionActions",
            [_update_operation(resource, mask)], dry_run, approval_id,
            confirmation_text, expected_confirmation=resource["resourceName"],
        )

    @mcp.tool(annotations=WRITE)
    @audited_tool
    def pause_conversion_action(
        customer_id: str,
        conversion_action_id: str,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
    ) -> str:
        """Disable a conversion action after validation."""
        resource_name = _resource(
            customer_id, "conversionActions", conversion_action_id
        )
        operation = _update_operation(
            {"resourceName": resource_name, "status": "HIDDEN"}, ["status"]
        )
        return _write(
            "pause_conversion_action", customer_id, "conversionActions",
            [operation], dry_run, approval_id, confirmation_text,
            expected_confirmation=resource_name,
        )


def _register_template_tool(mcp) -> None:
    @mcp.tool(annotations=WRITE)
    @audited_tool
    def create_campaign_from_template(
        customer_id: str,
        template_name: str,
        campaign_name: str,
        budget_id: str,
        overrides: Optional[dict[str, Any]] = None,
        dry_run: bool = True,
        approval_id: Optional[str] = None,
        confirmation_text: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Create a paused campaign from a controlled agency template."""
        templates = load_json_config("templates.json", {})
        if template_name not in templates:
            raise ValueError(f"Unknown template: {template_name}")
        config = dict(templates[template_name])
        config.update(overrides or {})
        channel = str(config.get("channel_type", "SEARCH")).upper()
        campaign = _campaign_create(
            customer_id,
            campaign_name,
            budget_id,
            channel,
            config.get("bidding_strategy", "MANUAL_CPC"),
            config.get("target_value"),
            config.get("start_date", date.today().isoformat()),
            config.get("end_date"),
            "PAUSED",
            False,
            config.get("extra"),
        )
        customer_id = ensure_customer_allowed(customer_id)
        temporary_campaign = f"customers/{customer_id}/campaigns/-1"
        campaign["resourceName"] = temporary_campaign
        operations: list[dict[str, Any]] = [
            {"campaignOperation": {"create": campaign}}
        ]
        for location in config.get("geo_targets", []):
            operations.append(
                {
                    "campaignCriterionOperation": {
                        "create": {
                            "campaign": temporary_campaign,
                            "location": {
                                "geoTargetConstant": (
                                    f"geoTargetConstants/{_id(location, 'geo_target')}"
                                )
                            },
                            "negative": False,
                        }
                    }
                }
            )
        for language in config.get("language_codes", []):
            operations.append(
                {
                    "campaignCriterionOperation": {
                        "create": {
                            "campaign": temporary_campaign,
                            "language": {
                                "languageConstant": (
                                    f"languageConstants/{_id(language, 'language_code')}"
                                )
                            },
                            "negative": False,
                        }
                    }
                }
            )
        return _write(
            "create_campaign_from_template", customer_id, "googleAds",
            operations, dry_run, approval_id, confirmation_text,
            expected_confirmation=campaign_name.strip(),
            idempotency_key=idempotency_key,
            batch=True,
        )


def register_write_tools(mcp) -> None:
    _register_campaign_tools(mcp)
    _register_ad_group_tools(mcp)
    _register_ad_tools(mcp)
    _register_keyword_tools(mcp)
    _register_audience_tools(mcp)
    _register_asset_tools(mcp)
    _register_recommendation_tools(mcp)
    _register_conversion_tools(mcp)
    _register_template_tool(mcp)
