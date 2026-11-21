"""Tests for the Enhanced Conversions status summarizer.

The summarizer is a pure function fed by GAQL rows; we build fixtures
that mirror v25 responses and assert on the shape of the report + alerts.
"""
from __future__ import annotations

import pytest

from agency.read_tools import _summarize_enhanced_conversions, _to_int


CUSTOMER_ROWS_BASIC = [
    {
        "customer": {
            "id": "1234567890",
            "descriptiveName": "Demo Account",
            "conversionTrackingSetting": {
                "conversionTrackingId": "18341119034",
                "conversionTrackingStatus": "CONVERSION_TRACKING_MANAGED_BY_SELF",
                "googleAdsConversionCustomer": "customers/1234567890",
            },
        }
    }
]

ACTION_ROWS_PURCHASE_ONLY = [
    {
        "conversionAction": {
            "resourceName": "customers/1234567890/conversionActions/1000000001",
            "id": "1000000001",
            "name": "Purchase",
            "status": "ENABLED",
            "type": "WEBPAGE",
            "category": "PURCHASE",
            "origin": "WEBSITE",
            "primaryForGoal": True,
            "includeInConversionsMetric": True,
            "countingType": "MANY_PER_CLICK",
            "clickThroughLookbackWindowDays": "30",
            "viewThroughLookbackWindowDays": "1",
            "attributionModelSettings": {
                "attributionModel": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
                "dataDrivenModelStatus": "AVAILABLE",
            },
        }
    },
    {
        "conversionAction": {
            "resourceName": "customers/1234567890/conversionActions/1000000002",
            "id": "1000000002",
            "name": "Purchase (1)",
            "status": "ENABLED",
            "type": "GOOGLE_ANALYTICS_4_CUSTOM",
            "category": "PURCHASE",
            "origin": "WEBSITE",
            "primaryForGoal": True,
            "includeInConversionsMetric": True,
            "countingType": "MANY_PER_CLICK",
            "clickThroughLookbackWindowDays": "90",
            "viewThroughLookbackWindowDays": "1",
            "attributionModelSettings": {
                "attributionModel": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
                "dataDrivenModelStatus": "AVAILABLE",
            },
        }
    },
]


_UNSET = object()


def _call(customer_rows=_UNSET, action_rows=_UNSET, metric_rows=_UNSET, days=30):
    return _summarize_enhanced_conversions(
        customer_id="1234567890",
        customer_rows=CUSTOMER_ROWS_BASIC if customer_rows is _UNSET else customer_rows,
        action_rows=ACTION_ROWS_PURCHASE_ONLY if action_rows is _UNSET else action_rows,
        metric_rows=[] if metric_rows is _UNSET else metric_rows,
        days=days,
    )


# ---------- Shape ----------


def test_report_has_expected_top_level_keys():
    report = _call()
    assert set(report.keys()) == {
        "customer_name",
        "tracking",
        "conversion_actions",
        "alerts",
        "summary",
    }


def test_customer_name_comes_from_customer_row():
    report = _call()
    assert report["customer_name"] == "Demo Account"


def test_tracking_settings_are_copied():
    report = _call()
    assert report["tracking"]["conversion_tracking_id"] == "18341119034"
    assert report["tracking"]["status"] == "CONVERSION_TRACKING_MANAGED_BY_SELF"
    assert (
        report["tracking"]["google_ads_conversion_customer"]
        == "customers/1234567890"
    )
    # Fields not present in the response stay None
    assert report["tracking"]["accepted_customer_data_terms"] is None
    assert report["tracking"]["enhanced_conversions_for_leads_enabled"] is None


def test_actions_flatten_attribution_and_lookback_fields():
    report = _call()
    action = report["conversion_actions"][0]
    assert action["id"] == "1000000001"
    assert action["attribution_model"] == "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN"
    assert action["data_driven_model_status"] == "AVAILABLE"
    assert action["click_through_lookback_days"] == 30
    assert action["view_through_lookback_days"] == 1


def test_actions_default_metrics_when_no_volume():
    report = _call()
    for action in report["conversion_actions"]:
        assert action["metrics"]["conversions"] == 0.0
        assert action["metrics"]["window_days"] == 30


# ---------- Volume rollup ----------


def test_volume_rolls_up_across_rows_by_resource_name():
    metrics_rows = [
        {
            "segments": {
                "conversionAction": "customers/1234567890/conversionActions/1000000001",
                "conversionActionName": "Purchase",
                "conversionActionCategory": "PURCHASE",
            },
            "metrics": {"allConversions": 3, "conversions": 2, "conversionsValue": 60},
        },
        {
            "segments": {
                "conversionAction": "customers/1234567890/conversionActions/1000000001",
                "conversionActionName": "Purchase",
                "conversionActionCategory": "PURCHASE",
            },
            "metrics": {"allConversions": 1, "conversions": 1, "conversionsValue": 20},
        },
    ]
    report = _call(metric_rows=metrics_rows)
    action = next(a for a in report["conversion_actions"] if a["id"] == "1000000001")
    assert action["metrics"]["all_conversions"] == 4.0
    assert action["metrics"]["conversions"] == 3.0
    assert action["metrics"]["conversions_value"] == 80.0


def test_metric_rows_without_resource_are_ignored():
    metrics_rows = [
        {"segments": {}, "metrics": {"conversions": 5}},
        {
            "segments": {
                "conversionAction": "customers/1234567890/conversionActions/1000000001"
            },
            "metrics": {"conversions": 1},
        },
    ]
    report = _call(metric_rows=metrics_rows)
    action = next(a for a in report["conversion_actions"] if a["id"] == "1000000001")
    assert action["metrics"]["conversions"] == 1.0


# ---------- Alerts ----------


def test_alerts_flag_missing_customer_data_terms_as_warning_without_leads():
    report = _call()
    codes = [a["code"] for a in report["alerts"]]
    assert "CUSTOMER_DATA_TERMS_NOT_ACCEPTED" in codes
    ec_alert = next(a for a in report["alerts"] if a["code"] == "CUSTOMER_DATA_TERMS_NOT_ACCEPTED")
    assert ec_alert["severity"] == "warning"


def test_alerts_upgrade_customer_data_terms_to_critical_when_leads_exist():
    lead_actions = ACTION_ROWS_PURCHASE_ONLY + [
        {
            "conversionAction": {
                "resourceName": "customers/1234567890/conversionActions/9999",
                "id": "9999",
                "name": "Lead form",
                "status": "ENABLED",
                "category": "LEAD",
                "primaryForGoal": True,
                "includeInConversionsMetric": True,
                "attributionModelSettings": {},
            }
        }
    ]
    report = _call(action_rows=lead_actions)
    ec = next(a for a in report["alerts"] if a["code"] == "CUSTOMER_DATA_TERMS_NOT_ACCEPTED")
    assert ec["severity"] == "critical"


def test_alerts_flag_ec4l_off_when_lead_actions_present():
    lead_actions = ACTION_ROWS_PURCHASE_ONLY + [
        {
            "conversionAction": {
                "resourceName": "customers/1234567890/conversionActions/9999",
                "id": "9999",
                "name": "Lead form",
                "status": "ENABLED",
                "category": "LEAD",
                "primaryForGoal": False,
                "includeInConversionsMetric": True,
                "attributionModelSettings": {},
            }
        }
    ]
    report = _call(action_rows=lead_actions)
    codes = [a["code"] for a in report["alerts"]]
    assert "EC4L_DISABLED_WITH_LEAD_ACTIONS" in codes


def test_alerts_no_ec4l_alert_when_no_lead_actions():
    report = _call()
    codes = [a["code"] for a in report["alerts"]]
    assert "EC4L_DISABLED_WITH_LEAD_ACTIONS" not in codes


def test_alerts_flag_no_primary_for_goal():
    stripped = [
        {**row, "conversionAction": {**row["conversionAction"], "primaryForGoal": False}}
        for row in ACTION_ROWS_PURCHASE_ONLY
    ]
    report = _call(action_rows=stripped)
    codes = [a["code"] for a in report["alerts"]]
    assert "NO_PRIMARY_FOR_GOAL" in codes


def test_alerts_flag_no_conversions_in_window_when_enabled_actions_exist():
    report = _call()
    codes = [a["code"] for a in report["alerts"]]
    assert "NO_CONVERSIONS_IN_WINDOW" in codes


def test_alerts_skip_no_conversions_when_data_flowing():
    metrics_rows = [
        {
            "segments": {
                "conversionAction": "customers/1234567890/conversionActions/1000000001"
            },
            "metrics": {"conversions": 5},
        }
    ]
    report = _call(metric_rows=metrics_rows)
    codes = [a["code"] for a in report["alerts"]]
    assert "NO_CONVERSIONS_IN_WINDOW" not in codes


def test_alerts_flag_dda_unavailable_on_primary():
    modified = [
        {
            **ACTION_ROWS_PURCHASE_ONLY[0],
            "conversionAction": {
                **ACTION_ROWS_PURCHASE_ONLY[0]["conversionAction"],
                "attributionModelSettings": {
                    "attributionModel": "GOOGLE_SEARCH_ATTRIBUTION_DATA_DRIVEN",
                    "dataDrivenModelStatus": "STALE_DATA",
                },
            },
        }
    ]
    report = _call(action_rows=modified)
    codes = [a["code"] for a in report["alerts"]]
    assert "DDA_NOT_AVAILABLE" in codes


def test_alerts_flag_primary_not_counted():
    modified = [
        {
            **ACTION_ROWS_PURCHASE_ONLY[0],
            "conversionAction": {
                **ACTION_ROWS_PURCHASE_ONLY[0]["conversionAction"],
                "includeInConversionsMetric": False,
            },
        }
    ]
    report = _call(action_rows=modified)
    codes = [a["code"] for a in report["alerts"]]
    assert "PRIMARY_NOT_COUNTED" in codes


# ---------- Summary counters ----------


def test_summary_counts_are_consistent():
    report = _call()
    s = report["summary"]
    assert s["total_actions"] == 2
    assert s["enabled_actions"] == 2
    assert s["primary_for_goal_actions"] == 2
    assert s["actions_with_conversions"] == 0
    assert s["lead_actions_present"] is False
    assert s["customer_data_terms_accepted"] is False
    assert s["enhanced_conversions_for_leads_enabled"] is False
    assert s["window_days"] == 30
    assert s["total_conversions_in_window"] == 0.0


def test_summary_reflects_accepted_terms_when_true():
    customer = [
        {
            **CUSTOMER_ROWS_BASIC[0],
            "customer": {
                **CUSTOMER_ROWS_BASIC[0]["customer"],
                "conversionTrackingSetting": {
                    **CUSTOMER_ROWS_BASIC[0]["customer"]["conversionTrackingSetting"],
                    "acceptedCustomerDataTerms": True,
                    "enhancedConversionsForLeadsEnabled": True,
                },
            },
        }
    ]
    report = _call(customer_rows=customer)
    assert report["summary"]["customer_data_terms_accepted"] is True
    assert report["summary"]["enhanced_conversions_for_leads_enabled"] is True
    codes = [a["code"] for a in report["alerts"]]
    assert "CUSTOMER_DATA_TERMS_NOT_ACCEPTED" not in codes


# ---------- Edge cases ----------


def test_empty_customer_rows_produces_null_tracking():
    report = _call(customer_rows=[])
    assert report["customer_name"] is None
    for value in report["tracking"].values():
        assert value is None


def test_empty_action_rows_produces_no_actions_and_no_primary_alert():
    report = _call(action_rows=[])
    assert report["conversion_actions"] == []
    codes = [a["code"] for a in report["alerts"]]
    assert "NO_PRIMARY_FOR_GOAL" in codes
    assert "NO_CONVERSIONS_IN_WINDOW" not in codes  # gated on enabled actions


# ---------- Helper ----------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30", 30),
        (30, 30),
        (None, None),
        ("", None),
        ("abc", None),
        ({}, None),
    ],
)
def test_to_int(value, expected):
    assert _to_int(value) == expected
