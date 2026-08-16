"""Tests for get_impression_share_losses pure summarizer.

Fixtures mirror what v25 returns for campaign and ad_group with the
Search IS metric family. The summarizer classifies each unit into one
diagnosis code and produces account-level recommendations.
"""
from __future__ import annotations

import pytest

from agency.read_tools import (
    _summarize_impression_share,
    _diagnose_impression_share,
    _to_share,
    _is_account_recommendations,
)


def _campaign_row(
    campaign_id: str = "10",
    name: str = "Camp",
    impressions: int = 1000,
    clicks: int = 50,
    cost_micros: int = 100_000,
    is_impr: float | None = 0.5,
    is_budget: float | None = 0.3,
    is_rank: float | None = 0.2,
    is_top: float | None = 0.4,
    is_abs_top: float | None = 0.3,
    is_budget_top: float | None = 0.2,
    is_rank_top: float | None = 0.4,
    is_budget_abs: float | None = 0.2,
    is_rank_abs: float | None = 0.5,
    status: str = "ENABLED",
    channel: str = "SEARCH",
):
    metrics: dict = {
        "impressions": str(impressions),
        "clicks": str(clicks),
        "costMicros": str(cost_micros),
    }

    def put(key, value):
        if value is not None:
            metrics[key] = value

    put("searchImpressionShare", is_impr)
    put("searchBudgetLostImpressionShare", is_budget)
    put("searchRankLostImpressionShare", is_rank)
    put("searchTopImpressionShare", is_top)
    put("searchBudgetLostTopImpressionShare", is_budget_top)
    put("searchRankLostTopImpressionShare", is_rank_top)
    put("searchAbsoluteTopImpressionShare", is_abs_top)
    put("searchBudgetLostAbsoluteTopImpressionShare", is_budget_abs)
    put("searchRankLostAbsoluteTopImpressionShare", is_rank_abs)
    return {
        "campaign": {
            "id": campaign_id,
            "name": name,
            "status": status,
            "advertisingChannelType": channel,
        },
        "metrics": metrics,
    }


def _ad_group_row(
    ad_group_id: str = "200",
    ag_name: str = "AG",
    campaign_id: str = "10",
    campaign_name: str = "Camp",
    impressions: int = 1000,
    clicks: int = 50,
    is_impr: float | None = 0.5,
    is_rank: float | None = 0.3,
    status: str = "ENABLED",
):
    metrics: dict = {
        "impressions": str(impressions),
        "clicks": str(clicks),
    }
    if is_impr is not None:
        metrics["searchImpressionShare"] = is_impr
    if is_rank is not None:
        metrics["searchRankLostImpressionShare"] = is_rank
    return {
        "campaign": {"id": campaign_id, "name": campaign_name},
        "adGroup": {"id": ad_group_id, "name": ag_name, "status": status},
        "metrics": metrics,
    }


# --------- _to_share ---------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("abc", None),
        (0, 0.0),
        (0.5, 0.5),
        ("0.1234567", 0.1235),
        (1, 1.0),
    ],
)
def test_to_share(raw, expected):
    assert _to_share(raw) == expected


# --------- _diagnose (campaign level) ---------


def test_diagnose_healthy_with_high_is():
    code, _ = _diagnose_impression_share(
        impressions=5000, is_impr=0.95, is_budget=0.0, is_rank=0.05, level="campaign"
    )
    assert code == "healthy"


def test_diagnose_budget_limited():
    code, _ = _diagnose_impression_share(
        impressions=500, is_impr=0.3, is_budget=0.5, is_rank=0.2, level="campaign"
    )
    assert code == "budget_limited"


def test_diagnose_rank_limited():
    code, _ = _diagnose_impression_share(
        impressions=500, is_impr=0.3, is_budget=0.1, is_rank=0.6, level="campaign"
    )
    assert code == "rank_limited"


def test_diagnose_mixed_when_both_significant_and_similar():
    code, _ = _diagnose_impression_share(
        impressions=500, is_impr=0.3, is_budget=0.35, is_rank=0.35, level="campaign"
    )
    assert code == "mixed"


def test_diagnose_not_serving_without_impressions_and_no_rank():
    code, _ = _diagnose_impression_share(
        impressions=0, is_impr=0.0, is_budget=0.0, is_rank=0.0, level="campaign"
    )
    assert code == "not_serving"


def test_diagnose_rank_limited_when_zero_impressions_but_rank_lost_positive():
    code, _ = _diagnose_impression_share(
        impressions=0, is_impr=0.0, is_budget=0.0, is_rank=0.9, level="campaign"
    )
    assert code == "rank_limited"


def test_diagnose_insufficient_data_when_impr_share_absent():
    code, _ = _diagnose_impression_share(
        impressions=100, is_impr=None, is_budget=None, is_rank=None, level="campaign"
    )
    assert code == "insufficient_data"


# --------- _diagnose (ad_group level) ---------


def test_diagnose_ad_group_rank_limited_when_rank_high():
    code, _ = _diagnose_impression_share(
        impressions=100, is_impr=0.4, is_budget=None, is_rank=0.5, level="ad_group"
    )
    assert code == "rank_limited"


def test_diagnose_ad_group_low_rank_returns_insufficient_data():
    code, msg = _diagnose_impression_share(
        impressions=100, is_impr=0.4, is_budget=None, is_rank=0.05, level="ad_group"
    )
    assert code == "insufficient_data"
    assert "campaign level" in msg


# --------- top-level summarize (campaign) ---------


def test_summarize_returns_expected_shape():
    report = _summarize_impression_share(rows=[_campaign_row()], level="campaign", days=30)
    assert set(report.keys()) == {
        "period_days",
        "level",
        "summary",
        "units",
        "recommendations",
    }
    assert report["level"] == "campaign"
    assert report["period_days"] == 30


def test_summarize_unit_carries_full_metric_breakdown():
    report = _summarize_impression_share(
        rows=[_campaign_row(is_impr=0.4, is_budget=0.5, is_rank=0.1)],
        level="campaign",
        days=30,
    )
    unit = report["units"][0]
    assert unit["impression_share"]["current"] == 0.4
    assert unit["losses"]["budget"]["any"] == 0.5
    assert unit["losses"]["rank"]["any"] == 0.1
    assert unit["diagnosis"]["code"] == "budget_limited"


def test_summarize_channel_type_present_at_campaign_level():
    report = _summarize_impression_share(
        rows=[_campaign_row(channel="SEARCH")], level="campaign", days=30
    )
    assert report["units"][0]["channel_type"] == "SEARCH"


def test_summarize_ad_group_unit_includes_ad_group_info():
    report = _summarize_impression_share(
        rows=[_ad_group_row(is_impr=0.4, is_rank=0.4)],
        level="ad_group",
        days=30,
    )
    unit = report["units"][0]
    assert unit["ad_group"]["id"] == "200"
    assert "channel_type" not in unit


def test_summarize_diagnosis_counter_matches_units():
    rows = [
        _campaign_row(campaign_id="1", is_impr=0.95, is_budget=0.0, is_rank=0.05),
        _campaign_row(campaign_id="2", is_impr=0.3, is_budget=0.5, is_rank=0.2),
        _campaign_row(campaign_id="3", is_impr=0.3, is_budget=0.1, is_rank=0.6),
        _campaign_row(campaign_id="4", impressions=0, is_impr=0.0, is_budget=0.0, is_rank=0.0),
    ]
    report = _summarize_impression_share(rows=rows, level="campaign", days=30)
    c = report["summary"]["diagnosis_counts"]
    assert c["healthy"] == 1
    assert c["budget_limited"] == 1
    assert c["rank_limited"] == 1
    assert c["not_serving"] == 1


# --------- recommendations ---------


def test_recommendations_include_budget_flag():
    rows = [_campaign_row(is_impr=0.3, is_budget=0.5, is_rank=0.2)]
    report = _summarize_impression_share(rows=rows, level="campaign", days=30)
    codes = [r["code"] for r in report["recommendations"]]
    assert "BUDGET_LIMITED_PRESENT" in codes


def test_recommendations_include_rank_flag():
    rows = [_campaign_row(is_impr=0.3, is_budget=0.1, is_rank=0.6)]
    report = _summarize_impression_share(rows=rows, level="campaign", days=30)
    codes = [r["code"] for r in report["recommendations"]]
    assert "RANK_LIMITED_PRESENT" in codes


def test_recommendations_all_healthy_when_every_unit_healthy():
    rows = [
        _campaign_row(campaign_id="1", is_impr=0.95, is_budget=0.0, is_rank=0.05),
        _campaign_row(campaign_id="2", is_impr=0.92, is_budget=0.02, is_rank=0.06),
    ]
    report = _summarize_impression_share(rows=rows, level="campaign", days=30)
    codes = [r["code"] for r in report["recommendations"]]
    assert "ALL_HEALTHY" in codes


def test_recommendations_empty_rows_returns_no_units_hint():
    report = _summarize_impression_share(rows=[], level="campaign", days=30)
    codes = [r["code"] for r in report["recommendations"]]
    assert "NO_UNITS" in codes


def test_recommendations_flag_mixed_when_multiple_units_mixed():
    rows = [
        _campaign_row(campaign_id="1", is_impr=0.3, is_budget=0.35, is_rank=0.35),
        _campaign_row(campaign_id="2", is_impr=0.4, is_budget=0.3, is_rank=0.3),
    ]
    report = _summarize_impression_share(rows=rows, level="campaign", days=30)
    codes = [r["code"] for r in report["recommendations"]]
    assert "MIXED_LIMITED" in codes


def test_recommendations_flag_not_serving():
    rows = [_campaign_row(impressions=0, is_impr=0.0, is_budget=0.0, is_rank=0.0)]
    report = _summarize_impression_share(rows=rows, level="campaign", days=30)
    codes = [r["code"] for r in report["recommendations"]]
    assert "NOT_SERVING_UNITS" in codes


# --------- integration edges ---------


def test_missing_metric_fields_do_not_crash():
    row = {"campaign": {"id": "1", "name": "empty"}, "metrics": {}}
    report = _summarize_impression_share(rows=[row], level="campaign", days=30)
    assert report["units"][0]["metrics"]["impressions"] == 0
    assert report["units"][0]["diagnosis"]["code"] == "not_serving"


def test_ad_group_level_omits_budget_reporting():
    rows = [_ad_group_row(is_impr=0.4, is_rank=0.5)]
    report = _summarize_impression_share(rows=rows, level="ad_group", days=30)
    unit = report["units"][0]
    # Budget IS not queryable at ad_group; the fields exist but stay None
    assert unit["losses"]["budget"]["any"] is None
    assert unit["losses"]["rank"]["any"] == 0.5
