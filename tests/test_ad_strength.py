"""Tests for the Ad Strength diagnostics summarizer.

Fixtures mimic v25 ad_group_ad rows. The summarizer is a pure function
so we exercise all suggestion / recommendation paths without touching
Google Ads.
"""
from __future__ import annotations

import pytest

from agency.read_tools import (
    _summarize_ad_strength,
    _asset_slot_report,
    _ad_suggestions,
    _account_ad_recommendations,
    _AD_STRENGTH_LABELS,
    _AD_STRENGTH_BUCKETS,
)


def _hl(text: str, label: str = "PENDING", pinned: str | None = None, approved: bool = True):
    item: dict = {"text": text, "assetPerformanceLabel": label}
    if pinned:
        item["pinnedField"] = pinned
    if approved:
        item["policySummaryInfo"] = {"approvalStatus": "APPROVED"}
    else:
        item["policySummaryInfo"] = {"approvalStatus": "DISAPPROVED"}
    return item


def _row(
    ad_id: str = "1",
    strength: str = "GOOD",
    headlines: list | None = None,
    descriptions: list | None = None,
    impressions: int = 500,
    clicks: int = 25,
    conversions: float = 2.0,
    campaign_name: str = "Camp",
    ad_group_name: str = "AG",
    campaign_id: str = "100",
    ad_group_id: str = "200",
    path1: str | None = None,
    path2: str | None = None,
    status: str = "ENABLED",
):
    rsa: dict = {
        "headlines": headlines
        if headlines is not None
        else [
            _hl("h1", "GOOD"),
            _hl("h2", "GOOD"),
            _hl("h3", "BEST"),
            _hl("h4", "LEARNING"),
            _hl("h5", "GOOD"),
            _hl("h6", "GOOD"),
            _hl("h7", "GOOD"),
            _hl("h8", "GOOD"),
        ],
        "descriptions": descriptions
        if descriptions is not None
        else [
            _hl("d1", "GOOD"),
            _hl("d2", "GOOD"),
            _hl("d3", "BEST"),
        ],
    }
    if path1:
        rsa["path1"] = path1
    if path2:
        rsa["path2"] = path2
    return {
        "campaign": {"id": campaign_id, "name": campaign_name},
        "adGroup": {"id": ad_group_id, "name": ad_group_name},
        "adGroupAd": {
            "status": status,
            "adStrength": strength,
            "ad": {
                "id": ad_id,
                "type": "RESPONSIVE_SEARCH_AD",
                "responsiveSearchAd": rsa,
            },
        },
        "metrics": {
            "impressions": str(impressions),
            "clicks": str(clicks),
            "conversions": conversions,
        },
    }


# --------- shape ---------


def test_report_has_expected_top_level_keys():
    report = _summarize_ad_strength(rows=[_row()], days=30)
    assert set(report.keys()) == {"period_days", "summary", "ads", "recommendations"}
    assert report["period_days"] == 30


def test_summary_counts_by_strength_bucket():
    rows = [
        _row(ad_id="1", strength="POOR"),
        _row(ad_id="2", strength="AVERAGE"),
        _row(ad_id="3", strength="GOOD"),
        _row(ad_id="4", strength="EXCELLENT"),
        _row(ad_id="5", strength="UNKNOWN"),
    ]
    report = _summarize_ad_strength(rows=rows, days=30)
    assert report["summary"]["by_strength"] == {
        "POOR": 1,
        "AVERAGE": 1,
        "GOOD": 1,
        "EXCELLENT": 1,
        "UNKNOWN": 1,
    }


def test_unknown_strength_falls_back_to_unknown_bucket():
    row = _row(strength="MYSTERIOUS")
    report = _summarize_ad_strength(rows=[row], days=30)
    assert report["ads"][0]["ad_strength"] == "UNKNOWN"
    assert report["summary"]["by_strength"]["UNKNOWN"] == 1


def test_missing_strength_field_treated_as_unknown():
    row = _row()
    del row["adGroupAd"]["adStrength"]
    report = _summarize_ad_strength(rows=[row], days=30)
    assert report["ads"][0]["ad_strength"] == "UNKNOWN"


# --------- asset slot report ---------


def test_asset_slot_counts_labels_and_pins():
    counter = {l: 0 for l in _AD_STRENGTH_LABELS}
    items = [
        _hl("a", "BEST", pinned="HEADLINE_1"),
        _hl("b", "GOOD"),
        _hl("c", "LOW", approved=False),
        _hl("d", "LEARNING"),
        _hl("e", "PENDING"),
    ]
    r = _asset_slot_report(items, counter)
    assert r["count"] == 5
    assert r["pinned_count"] == 1
    assert r["approved_count"] == 4
    assert r["counts_by_label"] == {
        "PENDING": 1,
        "LEARNING": 1,
        "LOW": 1,
        "GOOD": 1,
        "BEST": 1,
        "UNKNOWN": 0,
    }
    assert counter == r["counts_by_label"]
    assert r["items"][0]["pinned_to"] == "HEADLINE_1"
    assert r["items"][2]["approved"] is False


def test_asset_slot_unknown_label_bucket():
    counter = {l: 0 for l in _AD_STRENGTH_LABELS}
    items = [_hl("a", "MYSTERY_LABEL")]
    r = _asset_slot_report(items, counter)
    assert r["counts_by_label"]["UNKNOWN"] == 1


# --------- path display ---------


@pytest.mark.parametrize(
    ("p1", "p2", "expected"),
    [
        (None, None, None),
        ("foo", None, "foo"),
        (None, "bar", "/bar"),
        ("foo", "bar", "foo/bar"),
    ],
)
def test_path_display_composition(p1, p2, expected):
    row = _row(path1=p1, path2=p2)
    report = _summarize_ad_strength(rows=[row], days=30)
    assert report["ads"][0]["path_display"] == expected


# --------- metrics ---------


def test_metrics_ctr_computed():
    row = _row(impressions=1000, clicks=50, conversions=1.0)
    report = _summarize_ad_strength(rows=[row], days=30)
    m = report["ads"][0]["metrics"]
    assert m["impressions"] == 1000
    assert m["clicks"] == 50
    assert m["conversions"] == 1.0
    assert m["ctr"] == 0.05
    assert m["window_days"] == 30


def test_metrics_ctr_zero_when_no_impressions():
    row = _row(impressions=0, clicks=0)
    report = _summarize_ad_strength(rows=[row], days=30)
    assert report["ads"][0]["metrics"]["ctr"] == 0.0


# --------- per-ad suggestions ---------


def test_suggestion_few_headlines():
    row = _row(headlines=[_hl("only", "GOOD")])
    report = _summarize_ad_strength(rows=[row], days=30)
    codes = [s["code"] for s in report["ads"][0]["suggestions"]]
    assert "FEW_HEADLINES" in codes


def test_suggestion_few_descriptions():
    row = _row(descriptions=[_hl("only", "GOOD")])
    report = _summarize_ad_strength(rows=[row], days=30)
    codes = [s["code"] for s in report["ads"][0]["suggestions"]]
    assert "FEW_DESCRIPTIONS" in codes


def test_suggestion_poor_strength():
    report = _summarize_ad_strength(rows=[_row(strength="POOR")], days=30)
    codes = [s["code"] for s in report["ads"][0]["suggestions"]]
    assert "AD_STRENGTH_POOR" in codes


def test_suggestion_average_strength():
    report = _summarize_ad_strength(rows=[_row(strength="AVERAGE")], days=30)
    codes = [s["code"] for s in report["ads"][0]["suggestions"]]
    assert "AD_STRENGTH_AVERAGE" in codes


def test_suggestion_low_assets_flags_replacement():
    headlines = [_hl(f"h{i}", "GOOD") for i in range(8)]
    headlines[0] = _hl("weak", "LOW")
    report = _summarize_ad_strength(rows=[_row(headlines=headlines)], days=30)
    codes = [s["code"] for s in report["ads"][0]["suggestions"]]
    assert "LOW_PERFORMING_ASSETS" in codes


def test_suggestion_no_impressions_produces_info():
    report = _summarize_ad_strength(rows=[_row(impressions=0)], days=30)
    codes = [s["code"] for s in report["ads"][0]["suggestions"]]
    assert "NO_IMPRESSIONS_IN_WINDOW" in codes


def test_suggestion_insufficient_history_for_pending_assets():
    headlines = [_hl(f"h{i}", "PENDING") for i in range(8)]
    descriptions = [_hl(f"d{i}", "GOOD") for i in range(3)]
    row = _row(impressions=50, headlines=headlines, descriptions=descriptions)
    report = _summarize_ad_strength(rows=[row], days=30)
    codes = [s["code"] for s in report["ads"][0]["suggestions"]]
    assert "INSUFFICIENT_SERVING_HISTORY" in codes


def test_suggestion_over_pinned_headlines():
    headlines = [
        _hl(f"h{i}", "GOOD", pinned="HEADLINE_1" if i == 0 else "HEADLINE_2")
        for i in range(8)
    ]
    report = _summarize_ad_strength(rows=[_row(headlines=headlines)], days=30)
    codes = [s["code"] for s in report["ads"][0]["suggestions"]]
    assert "OVER_PINNED_HEADLINES" in codes


def test_suggestion_best_without_pins_nudge():
    headlines = [_hl(f"h{i}", "GOOD") for i in range(8)]
    headlines[0] = _hl("best", "BEST")  # unpinned BEST
    report = _summarize_ad_strength(rows=[_row(headlines=headlines)], days=30)
    codes = [s["code"] for s in report["ads"][0]["suggestions"]]
    assert "BEST_HEADLINES_WITHOUT_PINS" in codes


# --------- account-level recommendations ---------


def test_recommendation_when_no_ads():
    report = _summarize_ad_strength(rows=[], days=30)
    codes = [r["code"] for r in report["recommendations"]]
    assert "NO_ADS_MATCHED_FILTER" in codes


def test_recommendation_poor_share():
    rows = [_row(ad_id="1", strength="POOR"), _row(ad_id="2", strength="GOOD")]
    report = _summarize_ad_strength(rows=rows, days=30)
    codes = [r["code"] for r in report["recommendations"]]
    assert "POOR_STRENGTH_SHARE" in codes


def test_recommendation_majority_average():
    rows = [
        _row(ad_id="1", strength="AVERAGE"),
        _row(ad_id="2", strength="AVERAGE"),
        _row(ad_id="3", strength="GOOD"),
    ]
    report = _summarize_ad_strength(rows=rows, days=30)
    codes = [r["code"] for r in report["recommendations"]]
    assert "MAJORITY_AVERAGE" in codes


def test_recommendation_all_pending_when_no_scored_assets():
    headlines = [_hl(f"h{i}", "PENDING") for i in range(8)]
    descriptions = [_hl(f"d{i}", "PENDING") for i in range(3)]
    row = _row(headlines=headlines, descriptions=descriptions, impressions=0)
    report = _summarize_ad_strength(rows=[row], days=30)
    codes = [r["code"] for r in report["recommendations"]]
    assert "ALL_PENDING" in codes


def test_recommendation_low_assets_across_account():
    rows = [_row(headlines=[_hl(f"h{i}", "LOW") for i in range(8)])]
    report = _summarize_ad_strength(rows=rows, days=30)
    codes = [r["code"] for r in report["recommendations"]]
    assert "LOW_ASSETS_ACROSS_ACCOUNT" in codes


def test_summary_counts_pinned_ads():
    pinned_headlines = [_hl(f"h{i}", "GOOD") for i in range(8)]
    pinned_headlines[0] = _hl("h1", "GOOD", pinned="HEADLINE_1")
    rows = [
        _row(ad_id="1", headlines=pinned_headlines),
        _row(ad_id="2"),
    ]
    report = _summarize_ad_strength(rows=rows, days=30)
    assert report["summary"]["ads_with_pinned_assets"] == 1


# --------- constant sanity ---------


def test_label_and_bucket_constants_are_stable():
    assert set(_AD_STRENGTH_LABELS) == {
        "PENDING",
        "LEARNING",
        "LOW",
        "GOOD",
        "BEST",
        "UNKNOWN",
    }
    assert set(_AD_STRENGTH_BUCKETS) == {"POOR", "AVERAGE", "GOOD", "EXCELLENT", "UNKNOWN"}
