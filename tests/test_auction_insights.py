"""Tests for get_auction_insights pure summarizer.

Fixtures mirror v25 rows from `campaign` or `ad_group` segmented by
`auction_insight_domain`. Every row represents one competitor domain
in one date bucket; the summarizer averages across buckets per
(unit, domain).
"""
from __future__ import annotations

import pytest

from agency.read_tools import (
    _summarize_auction_insights,
    _auction_head_to_head,
)


def _row(
    domain: str,
    campaign_id: str = "10",
    campaign_name: str = "Camp",
    ad_group_id: str | None = None,
    ad_group_name: str | None = None,
    ad_group_status: str | None = None,
    campaign_status: str = "ENABLED",
    imp_share: float | None = 0.5,
    abs_top_pct: float | None = 0.3,
    top_pct: float | None = 0.5,
    outranking_share: float | None = 0.4,
    overlap_rate: float | None = 0.6,
    position_above_rate: float | None = 0.5,
):
    metrics: dict = {}

    def put(k, v):
        if v is not None:
            metrics[k] = v

    put("auctionInsightSearchImpressionShare", imp_share)
    put("auctionInsightSearchAbsoluteTopImpressionPercentage", abs_top_pct)
    put("auctionInsightSearchTopImpressionPercentage", top_pct)
    put("auctionInsightSearchOutrankingShare", outranking_share)
    put("auctionInsightSearchOverlapRate", overlap_rate)
    put("auctionInsightSearchPositionAboveRate", position_above_rate)

    row = {
        "campaign": {"id": campaign_id, "name": campaign_name, "status": campaign_status},
        "segments": {"auctionInsightDomain": domain},
        "metrics": metrics,
    }
    if ad_group_id:
        row["adGroup"] = {
            "id": ad_group_id,
            "name": ad_group_name,
            "status": ad_group_status,
        }
    return row


# ---------- shape ----------


def test_report_top_level_shape():
    rows = [_row("You"), _row("competitor.com", imp_share=0.4)]
    report = _summarize_auction_insights(rows=rows, level="campaign", days=30, campaign_id="10")
    assert set(report.keys()) == {
        "period_days",
        "level",
        "filter",
        "summary",
        "units",
        "recommendations",
    }
    assert report["level"] == "campaign"
    assert report["period_days"] == 30
    assert report["filter"] == {"campaign_id": "10"}


def test_empty_rows_returns_no_data_recommendation():
    report = _summarize_auction_insights(rows=[], level="campaign", days=30, campaign_id=None)
    assert report["summary"]["units"] == 0
    codes = [r["code"] for r in report["recommendations"]]
    assert "NO_AUCTION_INSIGHT_DATA" in codes


def test_row_without_domain_is_ignored():
    rows = [
        {"campaign": {"id": "1", "name": "A"}, "segments": {}, "metrics": {"auctionInsightSearchImpressionShare": 0.5}},
        _row("competitor.com"),
    ]
    report = _summarize_auction_insights(rows=rows, level="campaign", days=30, campaign_id=None)
    assert report["summary"]["units"] == 1
    assert report["units"][0]["competitors"][0]["domain"] == "competitor.com"


def test_row_without_unit_id_is_ignored():
    rows = [
        {"campaign": {}, "segments": {"auctionInsightDomain": "competitor.com"}, "metrics": {}},
    ]
    report = _summarize_auction_insights(rows=rows, level="campaign", days=30, campaign_id=None)
    assert report["summary"]["units"] == 0


# ---------- aggregation ----------


def test_multiple_date_rows_average_per_competitor():
    rows = [
        _row("competitor.com", imp_share=0.4, overlap_rate=0.5),
        _row("competitor.com", imp_share=0.6, overlap_rate=0.7),
        _row("You", imp_share=0.8),
    ]
    report = _summarize_auction_insights(rows=rows, level="campaign", days=30, campaign_id=None)
    unit = report["units"][0]
    comp = next(c for c in unit["competitors"] if c["domain"] == "competitor.com")
    assert comp["samples"] == 2
    assert comp["metrics"]["impression_share"] == 0.5
    assert comp["metrics"]["overlap_rate"] == 0.6


def test_competitors_sorted_by_impression_share_desc():
    rows = [
        _row("low.com", imp_share=0.1),
        _row("high.com", imp_share=0.9),
        _row("mid.com", imp_share=0.5),
    ]
    report = _summarize_auction_insights(rows=rows, level="campaign", days=30, campaign_id=None)
    domains = [c["domain"] for c in report["units"][0]["competitors"]]
    assert domains == ["high.com", "mid.com", "low.com"]


def test_missing_metric_does_not_crash():
    row = _row("competitor.com", imp_share=None, overlap_rate=None)
    report = _summarize_auction_insights(rows=[row], level="campaign", days=30, campaign_id=None)
    comp = report["units"][0]["competitors"][0]
    assert comp["metrics"]["impression_share"] == 0.0
    assert comp["metrics"]["overlap_rate"] == 0.0


def test_unique_competitor_count_excludes_you():
    rows = [
        _row("You"),
        _row("a.com"),
        _row("b.com"),
        _row("a.com"),  # duplicate domain, different date bucket
    ]
    report = _summarize_auction_insights(rows=rows, level="campaign", days=30, campaign_id=None)
    assert report["summary"]["unique_competitors"] == 2


# ---------- unit grouping ----------


def test_multiple_campaigns_form_separate_units():
    rows = [
        _row("You", campaign_id="1", campaign_name="A"),
        _row("You", campaign_id="2", campaign_name="B"),
    ]
    report = _summarize_auction_insights(rows=rows, level="campaign", days=30, campaign_id=None)
    assert report["summary"]["units"] == 2
    names = [u["campaign"]["name"] for u in report["units"]]
    assert names == ["A", "B"]


def test_ad_group_level_uses_ad_group_key():
    rows = [
        _row("You", ad_group_id="200", ad_group_name="AG1", ad_group_status="ENABLED"),
        _row("competitor.com", ad_group_id="200", ad_group_name="AG1", ad_group_status="ENABLED"),
        _row("You", ad_group_id="201", ad_group_name="AG2", ad_group_status="ENABLED"),
    ]
    report = _summarize_auction_insights(rows=rows, level="ad_group", days=30, campaign_id="10")
    assert report["summary"]["units"] == 2
    assert all("ad_group" in u for u in report["units"])


# ---------- head-to-head ----------


def test_head_to_head_contains_you_and_top_competitor():
    rows = [
        _row("You", imp_share=0.7),
        _row("comp.com", imp_share=0.6),
        _row("other.com", imp_share=0.3),
    ]
    report = _summarize_auction_insights(rows=rows, level="campaign", days=30, campaign_id=None)
    h2h = report["units"][0]["head_to_head"]
    assert h2h["you"]["impression_share"] == 0.7
    assert h2h["top_competitor"]["domain"] == "comp.com"


def test_head_to_head_none_when_no_self_row():
    rows = [_row("comp.com", imp_share=0.6)]
    report = _summarize_auction_insights(rows=rows, level="campaign", days=30, campaign_id=None)
    h2h = report["units"][0]["head_to_head"]
    assert h2h["you"] is None
    assert h2h["top_competitor"] is None


def test_head_to_head_when_only_you_present():
    rows = [_row("You", imp_share=0.7)]
    report = _summarize_auction_insights(rows=rows, level="campaign", days=30, campaign_id=None)
    h2h = report["units"][0]["head_to_head"]
    assert h2h["you"]["impression_share"] == 0.7
    assert h2h["top_competitor"] is None


def test_auction_head_to_head_pure_helper():
    competitors = [
        {"domain": "You", "metrics": {"impression_share": 0.7}},
        {"domain": "a.com", "metrics": {"impression_share": 0.5}},
    ]
    result = _auction_head_to_head(competitors)
    assert result["you"] == {"impression_share": 0.7}
    assert result["top_competitor"]["domain"] == "a.com"


# ---------- recommendations ----------


def test_recommendation_outranked_when_competitor_outranks_us():
    rows = [
        _row("You", imp_share=0.3),
        _row("threat.com", imp_share=0.6, outranking_share=0.7),
    ]
    report = _summarize_auction_insights(rows=rows, level="campaign", days=30, campaign_id=None)
    codes = [r["code"] for r in report["recommendations"]]
    assert "OUTRANKED_BY_COMPETITOR" in codes
    threat_rec = next(r for r in report["recommendations"] if r["code"] == "OUTRANKED_BY_COMPETITOR")
    assert "threat.com" in threat_rec["message"]


def test_no_outranked_recommendation_when_all_below_threshold():
    rows = [
        _row("You"),
        _row("comp.com", outranking_share=0.3),
    ]
    report = _summarize_auction_insights(rows=rows, level="campaign", days=30, campaign_id=None)
    codes = [r["code"] for r in report["recommendations"]]
    assert "OUTRANKED_BY_COMPETITOR" not in codes


def test_recommendation_per_unit_shows_unit_label():
    rows = [
        _row("You", campaign_id="1", campaign_name="A"),
        _row("threat.com", campaign_id="1", campaign_name="A", outranking_share=0.8),
        _row("You", campaign_id="2", campaign_name="B"),
    ]
    report = _summarize_auction_insights(rows=rows, level="campaign", days=30, campaign_id=None)
    outranked_recs = [r for r in report["recommendations"] if r["code"] == "OUTRANKED_BY_COMPETITOR"]
    assert len(outranked_recs) == 1
    assert outranked_recs[0]["unit"] == "A"


def test_recommendation_ad_group_label_includes_ad_group_name():
    rows = [
        _row("You", ad_group_id="20", ad_group_name="AG"),
        _row("threat.com", ad_group_id="20", ad_group_name="AG", outranking_share=0.9),
    ]
    report = _summarize_auction_insights(rows=rows, level="ad_group", days=30, campaign_id="10")
    rec = next(r for r in report["recommendations"] if r["code"] == "OUTRANKED_BY_COMPETITOR")
    assert "> AG" in rec["unit"]
