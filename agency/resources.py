"""Embedded MCP resources and prompts for agency operators."""

from __future__ import annotations

from .api import format_json
from .config import get_active_customer_id, get_settings


KNOWN_TOOLS = set(
    """
server_status list_accounts execute_gaql_query get_campaign_performance
get_ad_performance run_gaql get_ad_creatives get_account_currency
get_image_assets download_image_asset get_asset_usage analyze_image_assets
list_resources get_search_terms_report get_keyword_performance
get_geo_performance get_device_performance get_time_of_day_performance
get_audience_performance get_conversion_actions get_conversion_performance
get_change_history get_bidding_strategies get_budget_utilization get_shared_sets
get_enhanced_conversions_status get_ad_strength_diagnostics
get_impression_share_losses check_consent_signal_coverage
get_auction_insights mcc_bulk_apply
add_customer_match_members remove_customer_match_members
get_customer_match_list_stats get_offline_user_data_job_status
list_experiments get_experiment get_experiment_results
create_experiment schedule_experiment end_experiment
create_demand_gen_campaign create_demand_gen_ad_group
get_demand_gen_performance
upload_click_conversions upload_call_conversions
upload_conversion_adjustments
analyze_search_terms_clusters suggest_negatives_from_clusters
list_expiring_approvals export_audit_log rotate_audit_log
create_campaign_budget update_campaign_budget create_search_campaign
create_pmax_campaign create_display_campaign update_campaign pause_campaign
enable_campaign remove_campaign copy_campaign update_campaign_geo_targets
update_campaign_schedule create_ad_group update_ad_group pause_ad_group
enable_ad_group remove_ad_group create_responsive_search_ad
create_pmax_asset_group create_call_ad update_ad pause_ad enable_ad remove_ad
add_keywords_to_ad_group update_keyword_bid pause_keyword enable_keyword
remove_keyword add_negative_keyword add_negative_keywords_bulk
create_negative_keyword_list attach_negative_keyword_list_to_campaign
add_audience_to_ad_group remove_audience_from_ad_group update_bid_modifier
create_customer_match_list create_sitelink_extension create_callout_extension
create_structured_snippet_extension create_call_extension create_image_asset
link_asset_to_campaign unlink_asset_from_campaign list_recommendations
get_recommendation_detail apply_recommendation dismiss_recommendation
create_conversion_action update_conversion_action pause_conversion_action
generate_client_report generate_agency_dashboard compare_periods get_performance_trends detect_waste
generate_optimization_plan list_client_accounts get_client_context
switch_context run_across_clients get_audit_log get_pending_approvals
revert_operation create_campaign_from_template check_alerts
list_pending_client_approvals approve_operation reject_operation
generate_keyword_ideas generate_keyword_historical_metrics
generate_keyword_forecast find_geo_target_constants
list_analysis_profiles get_analysis_profile
audit_conversion_tracking get_offline_conversion_diagnostics
diagnose_delivery get_budget_pacing get_landing_page_performance
get_demographics_performance get_assets_and_calls_performance
get_pmax_diagnostics get_shopping_performance
get_placements_and_video_performance analyze_auction_insights_csv
update_campaign_conversion_goal set_conversion_action_primary
exclude_placements add_account_negative_keywords
""".split()
)


def _tool_domain(tool_name: str) -> str:
    """Return the high-level domain label for a tool.

    The only on-disk runbook shipped with the package is `guardrails`;
    other domain labels are informational (write vs. read, channel) and
    do not resolve to `agency://docs/<domain>`.
    """
    if any(token in tool_name for token in ("audit", "approval", "revert", "context")):
        return "guardrails"
    if any(token in tool_name for token in ("campaign", "budget", "ad_group", "ad_")):
        return "campaigns"
    if any(token in tool_name for token in ("keyword", "search_term", "waste")):
        return "search"
    if any(token in tool_name for token in ("audience", "asset", "extension", "modifier")):
        return "audiences-assets"
    if any(token in tool_name for token in ("conversion", "recommendation")):
        return "measurement"
    if any(token in tool_name for token in ("report", "dashboard", "client", "period", "alert")):
        return "reporting"
    return "agency"


def register_resources(mcp) -> None:
    @mcp.resource("agency://tools/{tool_name}")
    def tool_documentation(tool_name: str) -> str:
        """Return operational guidance for every exposed tool."""
        if tool_name not in KNOWN_TOOLS:
            raise ValueError(f"Unknown tool: {tool_name}")
        domain = _tool_domain(tool_name)
        write_like = tool_name.startswith(
            (
                "create_", "update_", "pause_", "enable_", "remove_", "add_",
                "attach_", "link_", "unlink_", "apply_", "dismiss_", "copy_",
                "approve_", "reject_", "revert_",
            )
        )
        return format_json(
            {
                "tool_name": tool_name,
                "domain": domain,
                "workflow": (
                    [
                        "Gather fresh evidence with read-only tools.",
                        "Call with dry_run=true.",
                        "Review validateOnly response and client identity.",
                        "Obtain any required confirmation or client approval.",
                        "Repeat the unchanged payload with dry_run=false and approval_id.",
                        "Verify state and retain audit_id.",
                    ]
                    if write_like
                    else [
                        "Confirm customer context.",
                        "Run the tool with a bounded date range.",
                        "Treat returned facts separately from recommendations.",
                    ]
                ),
                "common_errors": [
                    "customer_id is not whitelisted",
                    "READ_ONLY_MODE blocks mutations",
                    "approval expired or payload changed",
                    "Google Ads permission or access-level restriction",
                ],
                "official_docs": "https://developers.google.com/google-ads/api/docs/start",
            }
        )

    @mcp.resource("agency://governance/status")
    def governance_status() -> str:
        """Describe active safety controls without exposing secrets."""
        settings = get_settings()
        return format_json(
            {
                "operator": settings.operator_name,
                "read_only_mode": settings.read_only_mode,
                "allowed_customer_count": len(settings.allowed_customer_ids),
                "blocked_tools": sorted(settings.blocked_tools),
                "max_daily_budget_increase_micros": (
                    settings.max_daily_budget_increase_micros
                ),
                "max_budget_increase_pct": settings.max_budget_increase_pct,
                "max_bid_micros": settings.max_bid_micros,
                "client_approval_required": settings.client_approval_required,
                "active_customer_id": get_active_customer_id(),
            }
        )

    @mcp.prompt()
    def safe_campaign_change(request: str, customer_id: str) -> str:
        """Guide an operator through evidence, preview, approval and execution."""
        return f"""
You are preparing a Google Ads change for customer {customer_id}.
Requested outcome: {request}

Follow this sequence:
1. Run server_status and get_client_context.
2. Gather fresh evidence with read-only tools.
3. Explain expected business impact, risk and rollback.
4. Call the selected write tool with dry_run=true.
5. Show the client name, exact payload, caps and validation response.
6. Never invent approval. Ask the operator for the exact confirmation shown.
7. Execute only with the same payload and approval_id.
8. Return the audit_id and verify the resulting state with a read tool.
"""

    @mcp.prompt()
    def monthly_client_review(customer_id: str, period: str = "month") -> str:
        """Guide a consistent agency reporting and optimization review."""
        return f"""
For customer {customer_id}, generate a {period} client report, compare it with the
previous equivalent period, detect waste, check alerts and build an optimization
plan. Clearly separate facts from recommendations. Do not execute any write tool.
"""
