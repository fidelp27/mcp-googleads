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
