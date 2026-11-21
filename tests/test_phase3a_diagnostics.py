"""Phase 3A tests: diagnose_delivery, get_budget_pacing, landing pages, demographics, assets/calls, and CLI alerts."""

from __future__ import annotations

import json
from datetime import date

import google_ads_server as server
from agency import cli_alerts, diagnostics_tools as dt
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


def _coverage(currency: str = "EUR") -> dict:
    return {
        "currency_code": currency,
        "time_zone": "Europe/Madrid",
        "date_range": {"start": "2026-07-01", "end": "2026-07-14", "days": 14},
        "queried_at_utc": "2026-07-15T00:00:00+00:00",
        "complete_days_only": True,
        "conversion_lag_warning": None,
        "coverage_warnings": [],
    }


def test_diagnose_delivery_separates_confirmed_hypotheses_and_insufficient():
    report = dt._summarize_delivery_diagnosis(
        customer_id="1234567890",
        customer_obj={"status": "ENABLED"},
        billing_rows=[{"billingSetup": {"id": "1", "status": "PENDING"}}],  # no approved billing!
        account_budget_rows=[
            {
                "accountBudget": {
                    "id": "2",
                    "name": "Q3 Cap",
                    "status": "APPROVED",
                    "approvedSpendingLimitMicros": "100000000",
                    "amountServedMicros": "100000000",
                }
            }
        ],
        campaign_rows=[
            {
                "campaign": {
                    "id": "10",
                    "name": "Core Search",
                    "status": "ENABLED",
                    "primaryStatus": "LIMITED",
                    "primaryStatusReasons": ["BUDGET_CONSTRAINED"],
                },
                "campaignBudget": {"amountMicros": "50000000"},
                "metrics": {
                    "impressions": "0",
                    "clicks": "0",
                    "costMicros": "0",
                    "searchBudgetLostImpressionShare": 0.35,
                    "searchRankLostImpressionShare": 0.52,
                },
            }
        ],
        ad_policy_rows=[
            {
                "campaign": {"id": "10", "name": "Core Search"},
                "adGroup": {"id": "100"},
                "adGroupAd": {
                    "ad": {"id": "999"},
                    "policySummary": {
                        "approvalStatus": "DISAPPROVED",
                        "policyTopicEntries": [{"topic": "DESTINATION_NOT_WORKING"}],
                    },
                },
            }
        ],
        asset_policy_rows=[],
        keyword_policy_rows=[
            {
                "campaign": {"id": "10"},
                "adGroupCriterion": {"keyword": {"text": "blocked term"}, "approvalStatus": "DISAPPROVED"},
            }
        ],
        coverage=_coverage("EUR"),
        query_warnings=["Optional call_view unavailable"],
    )

    confirmed_codes = {c["code"] for c in report["confirmed_causes"]}
    hypothesis_codes = {h["code"] for h in report["hypotheses"]}
    insufficient_codes = {i["code"] for i in report["insufficient_data"]}

    assert {"NO_APPROVED_BILLING_SETUP", "ACCOUNT_BUDGET_EXHAUSTED", "DISAPPROVED_ADS", "DISAPPROVED_KEYWORDS", "BUDGET_CONSTRAINED"} <= confirmed_codes
    assert {"HIGH_RANK_LOST_IMPRESSION_SHARE", "ZERO_IMPRESSIONS_ENABLED_CAMPAIGN"} <= hypothesis_codes
    assert "DIAGNOSTIC_QUERY_UNAVAILABLE" in insufficient_codes


def test_budget_pacing_computes_projection_and_status():
    # July has 31 days; on July 16, 15 complete days have elapsed and 16 remain.
    report = dt._summarize_budget_pacing(
        customer_id="1234567890",
        currency_code="EUR",
        time_zone="Europe/Madrid",
        today_local=date(2026, 7, 16),
        mtd_cost_micros=1_500_000_000,  # 1,500 EUR spent in 15 days (100/day)
        last_7d_cost_micros=980_000_000,  # 140/day recent pace
        last_7d_days=7,
        active_daily_budget_micros=100_000_000,
        monthly_target_budget=3000.0,
        target_source="explicit_argument",
    )
    assert report["calendar"]["days_in_month"] == 31
    assert report["calendar"]["complete_days_elapsed"] == 15
    assert report["calendar"]["days_remaining"] == 16
    assert report["actuals"]["mtd_spend"] == 1500.0
    assert report["actuals"]["avg_daily_spend_last_7d"] == 140.0
    # 1500 + 140 * 16 = 3740 EUR vs 3000 target -> overspending (+24.67%)
    assert report["projection"]["projected_eom_spend"] == 3740.0
    assert report["projection"]["pacing_status"] == "overspending"


def test_phase3a_tools_work_in_demo_mode(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_DEMO_MODE", "true")
    mcp = _FakeMCP()
    dt.register_diagnostics_tools(mcp)

    diag = json.loads(mcp.tools["diagnose_delivery"](customer_id="1111111111", days=14))
    assert diag["currency_code"] == "EUR"
    assert diag["billing"]["approved_setups"] == 1
    assert diag["policy"]["disapproved_assets"] == 1

    pacing = json.loads(
        mcp.tools["get_budget_pacing"](customer_id="1111111111", monthly_target_budget=4000.0)
    )
    assert pacing["currency_code"] == "EUR"
    assert pacing["target"]["monthly_target_budget"] == 4000.0

    lp = json.loads(mcp.tools["get_landing_page_performance"](customer_id="1111111111", days=30))
    assert lp["returned_rows"] == 1

    demo = json.loads(
        mcp.tools["get_demographics_performance"](customer_id="1111111111", dimension="all", days=30)
    )
    assert set(demo["breakdowns"].keys()) == {"age", "gender", "income", "parental_status"}
    assert demo["breakdowns"]["age"]["returned_rows"] == 2

    assets = json.loads(mcp.tools["get_assets_and_calls_performance"](customer_id="2222222222", days=30))
    assert assets["assets"]["returned_rows"] == 2
    assert assets["calls"]["total_calls"] == 1


def test_cli_alerts_supports_demo_profile_and_markdown(monkeypatch, capsys):
    cli_alerts.main(["--demo", "--profile", "leads-audit", "--format", "markdown"])
    out = capsys.readouterr().out
    assert "# Google Ads Alerts" in out
    assert "leads-audit" in out

    cli_alerts.main(["--demo", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["accounts_checked"] == ["1111111111", "2222222222"]
    assert payload["count"] >= 1


def test_diagnostics_module_schemas_pass_multi_ai_linter():
    try:
        configure_tool_catalog(server.mcp, modules="core,diagnostics", read_only=True)
        tools = server.mcp._tool_manager.list_tools()
        names = {t.name for t in tools}
        assert {
            "diagnose_delivery",
            "get_budget_pacing",
            "get_landing_page_performance",
            "get_demographics_performance",
            "get_assets_and_calls_performance",
        } <= names
        for tool in tools:
            defs = tool.parameters.get("$defs") or {}
            assert _collect_untyped_objects(tool.parameters, defs) == [], f"{tool.name} failed schema lint"
    finally:
        configure_tool_catalog(server.mcp, modules=None, preset=None, read_only=True)
