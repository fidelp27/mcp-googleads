"""Phase 2 tests: unified conversion tracking audit, offline upload diagnostics, and Data Manager eligibility."""

from __future__ import annotations

import json

import google_ads_server as server
from agency import measurement_tools as mt
from agency.modules import configure_tool_catalog
from tests.test_phase1_catalog_profiles import _collect_untyped_objects


class _FakeMCP:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, **kwargs):
        def dec(fn):
            self.tools[fn.__name__] = fn
            return fn

        return dec


def _coverage(currency: str = "USD") -> dict:
    return {
        "currency_code": currency,
        "time_zone": "America/New_York",
        "date_range": {"start": "2026-06-01", "end": "2026-06-30", "days": 30},
        "queried_at_utc": "2026-07-01T00:00:00+00:00",
        "complete_days_only": True,
        "conversion_lag_warning": None,
        "coverage_warnings": [],
    }


def test_audit_handles_standard_custom_with_secondary_and_mcc_ownership():
    customer_rows = [
        {
            "customer": {
                "id": "1234567890",
                "descriptiveName": "Client MCC Child",
                "conversionTrackingSetting": {
                    "conversionTrackingId": "900111",
                    "conversionTrackingStatus": "CONVERSION_TRACKING_MANAGED_BY_SELF",
                    "googleAdsConversionCustomer": "customers/9999999999",  # MCC-owned!
                    "acceptedCustomerDataTerms": True,
                    "enhancedConversionsForLeadsEnabled": True,
                },
            }
        }
    ]
    action_rows = [
        {
            "conversionAction": {
                "resourceName": "customers/1234567890/conversionActions/1",
                "id": "1",
                "name": "Primary Lead",
                "status": "ENABLED",
                "category": "LEAD",
                "origin": "WEBSITE",
                "type": "WEBPAGE",
                "primaryForGoal": True,
                "includeInConversionsMetric": True,
            }
        },
        {
            "conversionAction": {
                "resourceName": "customers/1234567890/conversionActions/2",
                "id": "2",
                "name": "CRM SQL Secondary",
                "status": "ENABLED",
                "category": "QUALIFIED_LEAD",
                "origin": "CALL_FROM_ADS",
                "type": "UPLOAD_CLICKS",
                "primaryForGoal": False,
                "includeInConversionsMetric": False,
            }
        },
    ]
    customer_goals = [
        {"customerConversionGoal": {"category": "LEAD", "origin": "WEBSITE", "biddable": True}},
        {"customerConversionGoal": {"category": "QUALIFIED_LEAD", "origin": "CALL_FROM_ADS", "biddable": False}},
    ]
    custom_goals = [
        {
            "customConversionGoal": {
                "resourceName": "customers/1234567890/customConversionGoals/50",
                "id": "50",
                "name": "SQL Custom Goal",
                "status": "ENABLED",
                "conversionActions": [
                    "customers/1234567890/conversionActions/1",
                    "customers/1234567890/conversionActions/2",
                ],
            }
        }
    ]
    campaign_custom_goals = [
        {
            "campaign": {"id": "20"},
            "customConversionGoal": {
                "resourceName": "customers/1234567890/customConversionGoals/50"
            },
        }
    ]
    campaign_rows = [
        {
            "campaign": {
                "id": "10",
                "name": "Standard Account Goal Camp",
                "status": "ENABLED",
                "advertisingChannelType": "SEARCH",
                "biddingStrategyType": "TARGET_CPA",
            },
            "metrics": {"costMicros": "50000000", "conversions": 10.0, "allConversions": 12.0, "conversionsValue": 1000.0},
        },
        {
            "campaign": {
                "id": "20",
                "name": "Custom Goal With Secondary",
                "status": "ENABLED",
                "advertisingChannelType": "SEARCH",
                "biddingStrategyType": "MAXIMIZE_CONVERSIONS",
            },
            "metrics": {"costMicros": "80000000", "conversions": 8.0, "allConversions": 10.0, "conversionsValue": 2000.0},
        },
    ]
    action_metrics = [
        {
            "segments": {"conversionAction": "customers/1234567890/conversionActions/1"},
            "metrics": {"conversions": 15.0, "conversionsValue": 2500.0, "allConversions": 16.0, "allConversionsValue": 2600.0},
        }
    ]

    report = mt._summarize_conversion_audit(
        customer_id="1234567890",
        customer_rows=customer_rows,
        action_rows=action_rows,
        customer_goal_rows=customer_goals,
        campaign_goal_rows=[],
        custom_goal_rows=custom_goals,
        campaign_custom_goal_rows=campaign_custom_goals,
        campaign_rows=campaign_rows,
        action_metric_rows=action_metrics,
        coverage=_coverage("USD"),
    )

    assert report["tracking"]["centralized_by_mcc"] is True
    assert report["tracking"]["owner_customer"] == "customers/9999999999"

    camp_std = next(c for c in report["campaigns"] if c["campaign_id"] == "10")
    assert camp_std["goal_mode"] == "account_default"
    assert [a["name"] for a in camp_std["effective_biddable_actions"]] == ["Primary Lead"]

    camp_custom = next(c for c in report["campaigns"] if c["campaign_id"] == "20")
    assert camp_custom["goal_mode"] == "custom_goal"
    assert {a["name"] for a in camp_custom["effective_biddable_actions"]} == {
        "Primary Lead",
        "CRM SQL Secondary",
    }

    alert_codes = {a["code"] for a in report["alerts"]}
    assert "MCC_CENTRALIZED_TRACKING" in alert_codes
    assert "CUSTOM_GOAL_INCLUDES_SECONDARY_ACTION" in alert_codes


def test_audit_detects_smart_bidding_without_actions_and_double_counting_risk():
    action_rows = [
        {
            "conversionAction": {
                "resourceName": "customers/1234567890/conversionActions/1",
                "id": "1",
                "name": "Purchase (GA4)",
                "status": "ENABLED",
                "category": "PURCHASE",
                "origin": "GOOGLE_ANALYTICS_4",
                "type": "GOOGLE_ANALYTICS_4_CUSTOM",
                "primaryForGoal": True,
                "includeInConversionsMetric": True,
            }
        },
        {
            "conversionAction": {
                "resourceName": "customers/1234567890/conversionActions/2",
                "id": "2",
                "name": "Purchase (Ads Tag)",
                "status": "ENABLED",
                "category": "PURCHASE",
                "origin": "WEBSITE",
                "type": "WEBPAGE",
                "primaryForGoal": True,
                "includeInConversionsMetric": True,
            }
        },
    ]
    # Campaign overrides goals to non-biddable -> 0 effective actions!
    campaign_goals = [
        {
            "campaign": {"id": "99"},
            "campaignConversionGoal": {"category": "PURCHASE", "origin": "WEBSITE", "biddable": False},
        }
    ]
    campaign_rows = [
        {
            "campaign": {
                "id": "99",
                "name": "Broken Smart Bidding",
                "status": "ENABLED",
                "advertisingChannelType": "SEARCH",
                "biddingStrategyType": "TARGET_ROAS",
            },
            "metrics": {"costMicros": "100000000", "conversions": 0.0, "allConversions": 20.0, "conversionsValue": 0.0},
        }
    ]

    report = mt._summarize_conversion_audit(
        customer_id="1234567890",
        customer_rows=[],
        action_rows=action_rows,
        customer_goal_rows=[],
        campaign_goal_rows=campaign_goals,
        custom_goal_rows=[],
        campaign_custom_goal_rows=[],
        campaign_rows=campaign_rows,
        action_metric_rows=[],
        coverage=_coverage("EUR"),
    )
    codes = {a["code"] for a in report["alerts"]}
    assert "SMART_BIDDING_WITHOUT_EFFECTIVE_ACTIONS" in codes
    assert "SMART_BIDDING_ZERO_CONVERSIONS" in codes
    assert "MULTIPLE_PRIMARY_ACTIONS_SAME_CATEGORY" in codes
    assert "MEASUREMENT_SCOPE_DIFFERENCE" in codes


def test_offline_diagnostics_and_data_manager_eligibility_in_demo_mode(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_DEMO_MODE", "true")
    mcp = _FakeMCP()
    mt.register_measurement_tools(mcp)

    audit = json.loads(mcp.tools["audit_conversion_tracking"](customer_id="2222222222", days=30))
    assert audit["currency_code"] == "USD"
    assert audit["time_zone"] == "America/New_York"
    assert audit["summary"]["custom_goals_count"] == 1
    camp_201 = next(c for c in audit["campaigns"] if c["campaign_id"] == "201")
    assert camp_201["goal_mode"] == "custom_goal"

    diag = json.loads(mcp.tools["get_offline_conversion_diagnostics"](customer_id="2222222222"))
    ing = diag["ingestion_summary"]
    assert ing["sent_events"] == 120
    assert ing["accepted_events"] == 102
    assert ing["pending_events"] == 4
    assert ing["failed_events"] == 14
    assert ing["last_upload_date_time"] == "2026-11-19 18:30:00"
    assert diag["data_manager_eligibility"]["eligible_for_upgrade"] is True
    assert "GOOGLE_ADS_API" in diag["data_manager_eligibility"]["legacy_clients_detected"]
    alert_codes = {a["code"] for a in diag["alerts"]}
    assert "HIGH_OFFLINE_UPLOAD_ERROR_RATE" in alert_codes
    assert "DATA_MANAGER_PREREQ_TERMS_MISSING" in alert_codes


def test_measurement_module_schemas_pass_multi_ai_linter():
    try:
        configure_tool_catalog(server.mcp, modules="core,measurement", read_only=True)
        tools = server.mcp._tool_manager.list_tools()
        names = {t.name for t in tools}
        assert {"audit_conversion_tracking", "get_offline_conversion_diagnostics"} <= names
        for tool in tools:
            schema = tool.parameters
            defs = schema.get("$defs") or {}
            assert _collect_untyped_objects(schema, defs) == [], f"{tool.name} failed multi-AI schema lint"
    finally:
        configure_tool_catalog(server.mcp, modules=None, preset=None, read_only=True)
