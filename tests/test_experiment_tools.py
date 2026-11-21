"""Tests for the pure helpers in agency.experiment_tools.

The full experiment lifecycle is smoke-tested against the real API;
here we cover the arm comparison, resource-name helpers and dispatch
edge cases.
"""
from __future__ import annotations

import pytest

from agency import experiment_tools as et


# ---------- _positive_int + resource helpers ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1", 1), ("42", 42), (7, 7), (" 100 ", 100)],
)
def test_positive_int(raw, expected):
    assert et._positive_int(raw, "field") == expected


@pytest.mark.parametrize("bad", ["", " ", "abc", "-3", "0"])
def test_positive_int_rejects_bad(bad):
    with pytest.raises(ValueError, match="positive integer"):
        et._positive_int(bad, "field")


def test_experiment_resource():
    assert (
        et._experiment_resource("1234567890", "42")
        == "customers/1234567890/experiments/42"
    )


def test_campaign_resource():
    assert (
        et._campaign_resource("1234567890", "11111111111")
        == "customers/1234567890/campaigns/11111111111"
    )


# ---------- _iso_date ----------


def test_iso_date_none_passthrough():
    assert et._iso_date(None, "start_date") is None
    assert et._iso_date("", "start_date") is None


def test_iso_date_valid():
    assert et._iso_date("2026-09-01", "start_date") == "2026-09-01"


@pytest.mark.parametrize("bad", ["2026/09/01", "26-09-01", "09-01-2026", "2026-9-1"])
def test_iso_date_invalid(bad):
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        et._iso_date(bad, "start_date")


# ---------- _compare_arms ----------


def _arm(name, control, split=50, metrics=None, campaigns=None):
    return {
        "name": name,
        "control": control,
        "traffic_split": split,
        "campaigns": campaigns or ["customers/1/campaigns/1"],
        "metrics": metrics
        or {
            "impressions": 1000,
            "clicks": 50,
            "cost_micros": 100_000_000,  # $100
            "conversions": 5.0,
            "conversions_value": 500.0,
        },
    }


def test_compare_arms_returns_deltas():
    control = _arm("control", True, 50)
    treatment = _arm(
        "treatment",
        False,
        50,
        metrics={
            "impressions": 1000,
            "clicks": 100,  # 2x clicks
            "cost_micros": 150_000_000,  # $150
            "conversions": 10.0,  # 2x conv
            "conversions_value": 1000.0,
        },
    )
    report = et._compare_arms([control, treatment])
    assert report["control_arm"] == "control"
    comp = report["treatments"][0]
    assert comp["treatment_arm"] == "treatment"
    assert comp["deltas_pct"]["clicks"] == 100.0
    assert comp["deltas_pct"]["conversions"] == 100.0
    assert comp["deltas_pct"]["cost_micros"] == 50.0
    # CTR = clicks/impressions; control 5%, treatment 10%
    assert comp["rates"]["control_ctr"] == 0.05
    assert comp["rates"]["treatment_ctr"] == 0.1
    # CPA = cost_units / conversions; control $20, treatment $15 → -25%
    assert comp["rates"]["control_cpa"] == 20.0
    assert comp["rates"]["treatment_cpa"] == 15.0
    assert comp["deltas_pct"]["cpa"] == -25.0


def test_compare_arms_warns_without_control():
    only_treatment = _arm("t", False)
    report = et._compare_arms([only_treatment])
    assert "warning" in report


def test_compare_arms_warns_without_treatment():
    only_control = _arm("c", True)
    report = et._compare_arms([only_control])
    assert "warning" in report


def test_compare_arms_handles_zero_denominators():
    control = _arm(
        "control", True, metrics={
            "impressions": 0, "clicks": 0, "cost_micros": 0,
            "conversions": 0.0, "conversions_value": 0.0,
        },
    )
    treatment = _arm(
        "treatment", False, metrics={
            "impressions": 1000, "clicks": 10, "cost_micros": 50_000_000,
            "conversions": 1.0, "conversions_value": 100.0,
        },
    )
    report = et._compare_arms([control, treatment])
    comp = report["treatments"][0]
    assert comp["rates"]["control_ctr"] is None
    assert comp["deltas_pct"]["clicks"] is None  # base was 0
    assert comp["deltas_pct"]["conversions"] is None
    # cpa delta needs both control_cpa and treatment_cpa → control is None so delta None
    assert comp["deltas_pct"]["cpa"] is None


def test_compare_arms_multi_treatment():
    control = _arm("control", True)
    t1 = _arm(
        "t1", False, metrics={
            "impressions": 500, "clicks": 25, "cost_micros": 50_000_000,
            "conversions": 3.0, "conversions_value": 300.0,
        },
    )
    t2 = _arm(
        "t2", False, metrics={
            "impressions": 500, "clicks": 30, "cost_micros": 60_000_000,
            "conversions": 4.0, "conversions_value": 400.0,
        },
    )
    report = et._compare_arms([control, t1, t2])
    assert len(report["treatments"]) == 2
    assert {c["treatment_arm"] for c in report["treatments"]} == {"t1", "t2"}


# ---------- _metrics_for_campaigns edge cases ----------


def test_metrics_for_campaigns_empty_list_returns_zeros(monkeypatch):
    result = et._metrics_for_campaigns("1234567890", [], 30)
    assert result == {
        "impressions": 0,
        "clicks": 0,
        "cost_micros": 0,
        "conversions": 0.0,
        "conversions_value": 0.0,
    }


def test_metrics_for_campaigns_rejects_bad_days(monkeypatch):
    monkeypatch.setattr(et, "search", lambda cid, q: [])
    with pytest.raises(ValueError, match="between 1 and 365"):
        et._metrics_for_campaigns("1234567890", ["customers/1/campaigns/1"], 400)


def test_metrics_for_campaigns_aggregates_rows(monkeypatch):
    def fake_search(cid, query):
        return [
            {
                "metrics": {
                    "impressions": "500", "clicks": "25", "costMicros": "60000000",
                    "conversions": 2.0, "conversionsValue": 100.0,
                }
            },
            {
                "metrics": {
                    "impressions": "500", "clicks": "25", "costMicros": "40000000",
                    "conversions": 3.0, "conversionsValue": 150.0,
                }
            },
        ]

    monkeypatch.setattr(et, "search", fake_search)
    result = et._metrics_for_campaigns("1234567890", ["customers/1/campaigns/1"], 30)
    assert result["impressions"] == 1000
    assert result["clicks"] == 50
    assert result["cost_micros"] == 100_000_000
    assert result["conversions"] == 5.0
    assert result["conversions_value"] == 250.0
