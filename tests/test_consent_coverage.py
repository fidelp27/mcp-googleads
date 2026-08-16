"""Tests for check_consent_signal_coverage pure summarizer.

Attribution rate (conversions/all_conversions) per campaign, comparing
current period vs previous period, is used as a proxy for consent
signal health. The summarizer is pure; tests build fixture rows and
assert every alert path.
"""
from __future__ import annotations

import pytest

from agency.read_tools import _summarize_consent_coverage


def _row(cid: str, name: str, attributed: float, total: float, status: str = "ENABLED"):
    return {
        "campaign": {"id": cid, "name": name, "status": status},
        "metrics": {
            "conversions": attributed,
            "allConversions": total,
            "conversionsValue": attributed * 10,
            "allConversionsValue": total * 10,
        },
    }


def _settings(accepted: bool | None = True, ec4l: bool | None = False, status: str | None = "CONVERSION_TRACKING_MANAGED_BY_SELF"):
    setting: dict = {}
    if status is not None:
        setting["conversionTrackingStatus"] = status
    if accepted is not None:
        setting["acceptedCustomerDataTerms"] = accepted
    if ec4l is not None:
        setting["enhancedConversionsForLeadsEnabled"] = ec4l
    return [{"customer": {"id": "5461642152", "descriptiveName": "Clariola", "conversionTrackingSetting": setting}}]


def _call(current=None, previous=None, settings=None, days=30):
    return _summarize_consent_coverage(
        customer_id="5461642152",
        current_rows=current if current is not None else [],
        previous_rows=previous if previous is not None else [],
        settings_rows=settings if settings is not None else _settings(),
        days=days,
        current_period=("2026-07-18", "2026-08-16"),
        previous_period=("2026-06-18", "2026-07-17"),
    )


# ---------- shape ----------


def test_report_shape():
    report = _call()
    assert set(report.keys()) == {
        "customer_name",
        "period_days",
        "current_period",
        "previous_period",
        "tracking",
        "campaigns",
        "alerts",
        "summary",
        "notes",
    }
    assert report["period_days"] == 30
    assert report["current_period"] == {"start": "2026-07-18", "end": "2026-08-16"}


def test_notes_disclose_api_limitation():
    report = _call()
    assert any("ad_user_data" in n for n in report["notes"])


def test_customer_name_from_settings():
    report = _call()
    assert report["customer_name"] == "Clariola"


def test_tracking_reflects_settings():
    report = _call(settings=_settings(accepted=True, ec4l=True))
    assert report["tracking"]["accepted_customer_data_terms"] is True
    assert report["tracking"]["enhanced_conversions_for_leads_enabled"] is True


# ---------- campaign comparison ----------


def test_campaign_shows_both_periods():
    current = [_row("1", "Camp A", attributed=50, total=100)]
    previous = [_row("1", "Camp A", attributed=80, total=100)]
    report = _call(current=current, previous=previous)
    camp = report["campaigns"][0]
    assert camp["current"]["attribution_rate"] == 0.5
    assert camp["previous"]["attribution_rate"] == 0.8
    assert camp["attribution_rate_delta_pp"] == -30.0


def test_campaign_previous_none_when_missing():
    current = [_row("1", "Camp A", attributed=50, total=100)]
    report = _call(current=current, previous=[])
    camp = report["campaigns"][0]
    assert camp["previous"] is None
    assert camp["attribution_rate_delta_pp"] is None


def test_attribution_rate_none_when_zero_total():
    current = [_row("1", "Camp A", attributed=0, total=0)]
    report = _call(current=current)
    assert report["campaigns"][0]["current"]["attribution_rate"] is None


# ---------- per-campaign alerts ----------


def test_alert_zero_attributed_with_total_conversions():
    current = [_row("1", "Camp A", attributed=0, total=10)]
    report = _call(current=current)
    codes = [a["code"] for a in report["alerts"]]
    assert "ZERO_ATTRIBUTED_WITH_TOTAL" in codes


def test_alert_low_attribution_rate_when_below_30_percent():
    current = [_row("1", "Camp A", attributed=2, total=10)]  # 20%
    report = _call(current=current)
    codes = [a["code"] for a in report["alerts"]]
    assert "LOW_ATTRIBUTION_RATE" in codes


def test_low_attribution_rate_skipped_when_below_min_conv_threshold():
    current = [_row("1", "Camp A", attributed=1, total=4)]
    report = _call(current=current)
    codes = [a["code"] for a in report["alerts"]]
    # Only 4 conversions; low volume, don't cry wolf
    assert "LOW_ATTRIBUTION_RATE" not in codes


def test_alert_attribution_drop_when_delta_ge_20pp():
    current = [_row("1", "Camp A", attributed=60, total=100)]
    previous = [_row("1", "Camp A", attributed=85, total=100)]
    report = _call(current=current, previous=previous)
    codes = [a["code"] for a in report["alerts"]]
    assert "ATTRIBUTION_DROP" in codes


def test_no_attribution_drop_alert_when_stable():
    current = [_row("1", "Camp A", attributed=78, total=100)]
    previous = [_row("1", "Camp A", attributed=80, total=100)]
    report = _call(current=current, previous=previous)
    codes = [a["code"] for a in report["alerts"]]
    assert "ATTRIBUTION_DROP" not in codes


# ---------- customer-level alerts ----------


def test_alert_customer_data_terms_not_accepted():
    settings = _settings(accepted=False)
    current = [_row("1", "Camp A", attributed=50, total=100)]
    report = _call(current=current, settings=settings)
    codes = [a["code"] for a in report["alerts"]]
    assert "CUSTOMER_DATA_TERMS_NOT_ACCEPTED" in codes


def test_no_customer_data_terms_alert_when_accepted():
    settings = _settings(accepted=True)
    current = [_row("1", "Camp A", attributed=50, total=100)]
    report = _call(current=current, settings=settings)
    codes = [a["code"] for a in report["alerts"]]
    assert "CUSTOMER_DATA_TERMS_NOT_ACCEPTED" not in codes


def test_alert_unexpected_tracking_status():
    settings = _settings(status="SOMETHING_WEIRD")
    current = [_row("1", "Camp A", attributed=50, total=100)]
    report = _call(current=current, settings=settings)
    codes = [a["code"] for a in report["alerts"]]
    assert "UNEXPECTED_TRACKING_STATUS" in codes


def test_no_alert_when_tracking_status_null():
    settings = _settings(status=None)
    current = [_row("1", "Camp A", attributed=50, total=100)]
    report = _call(current=current, settings=settings)
    codes = [a["code"] for a in report["alerts"]]
    assert "UNEXPECTED_TRACKING_STATUS" not in codes


# ---------- summary ----------


def test_summary_totals_are_correct():
    current = [
        _row("1", "A", attributed=30, total=50),
        _row("2", "B", attributed=10, total=20),
    ]
    previous = [
        _row("1", "A", attributed=25, total=30),
    ]
    report = _call(current=current, previous=previous)
    s = report["summary"]
    assert s["campaigns_evaluated"] == 2
    assert s["total_conversions_current"] == 70
    assert s["total_conversions_previous"] == 30
    # 40 attributed / 70 total = 0.5714
    assert s["account_attribution_rate"] == 0.5714


def test_summary_no_data_alert_when_no_conversions_anywhere():
    report = _call(current=[], previous=[])
    codes = [a["code"] for a in report["alerts"]]
    assert "NO_DATA_TO_ASSESS" in codes
    assert report["summary"]["account_attribution_rate"] is None


def test_empty_current_but_settings_still_get_alerts():
    settings = _settings(accepted=False)
    report = _call(current=[], settings=settings)
    codes = [a["code"] for a in report["alerts"]]
    assert "CUSTOMER_DATA_TERMS_NOT_ACCEPTED" in codes
    assert "NO_DATA_TO_ASSESS" in codes


# ---------- edge ----------


def test_campaign_without_id_skipped():
    current = [
        {"campaign": {}, "metrics": {"conversions": 10, "allConversions": 20}},
        _row("2", "B", attributed=5, total=10),
    ]
    report = _call(current=current)
    assert report["summary"]["campaigns_evaluated"] == 1
    assert report["campaigns"][0]["campaign_id"] == "2"


def test_settings_empty_gracefully():
    report = _call(current=[_row("1", "A", 0, 0)], settings=[])
    assert report["tracking"] == {
        "accepted_customer_data_terms": None,
        "enhanced_conversions_for_leads_enabled": None,
        "conversion_tracking_status": None,
    }
    assert report["customer_name"] is None
