"""Tests for the pure helpers in agency.demand_gen_tools."""
from __future__ import annotations

import pytest

from agency import demand_gen_tools as dg


# ---------- _positive_int ----------


@pytest.mark.parametrize(("raw", "expected"), [("1", 1), ("100", 100), (7, 7)])
def test_positive_int_ok(raw, expected):
    assert dg._positive_int(raw, "field") == expected


@pytest.mark.parametrize("bad", ["", " ", "abc", "0", "-3"])
def test_positive_int_bad(bad):
    with pytest.raises(ValueError):
        dg._positive_int(bad, "field")


def test_campaign_and_budget_resources():
    assert (
        dg._campaign_resource("1234567890", "11111111111")
        == "customers/1234567890/campaigns/11111111111"
    )
    assert (
        dg._budget_resource("1234567890", "999")
        == "customers/1234567890/campaignBudgets/999"
    )


# ---------- _build_bidding_payload ----------


def test_bidding_maximize_conversions_needs_nothing():
    assert dg._build_bidding_payload("MAXIMIZE_CONVERSIONS", None, None) == {
        "maximizeConversions": {}
    }


def test_bidding_target_cpa_requires_cpa():
    with pytest.raises(ValueError, match="target_cpa_micros is required"):
        dg._build_bidding_payload("TARGET_CPA", None, None)


def test_bidding_target_cpa_valid_value():
    payload = dg._build_bidding_payload("TARGET_CPA", 5_000_000, None)
    assert payload == {"targetCpa": {"targetCpaMicros": "5000000"}}


def test_bidding_target_cpa_rejects_non_positive():
    with pytest.raises(ValueError, match="must be positive"):
        dg._build_bidding_payload("TARGET_CPA", 0, None)


def test_bidding_target_cpa_enforces_max_bid_cap(monkeypatch):
    class Settings:
        max_bid_micros = 1_000_000
    monkeypatch.setattr(dg, "get_settings", lambda: Settings())
    with pytest.raises(ValueError, match="MAX_BID_MICROS"):
        dg._build_bidding_payload("TARGET_CPA", 5_000_000, None)


def test_bidding_target_roas_valid():
    payload = dg._build_bidding_payload("TARGET_ROAS", None, 3.5)
    assert payload == {"targetRoas": {"targetRoas": 3.5}}


def test_bidding_target_roas_requires_value():
    with pytest.raises(ValueError, match="target_roas is required"):
        dg._build_bidding_payload("TARGET_ROAS", None, None)


def test_bidding_target_roas_rejects_non_positive():
    with pytest.raises(ValueError, match="must be positive"):
        dg._build_bidding_payload("TARGET_ROAS", None, 0)


def test_bidding_rejects_unknown_strategy():
    with pytest.raises(ValueError, match="must be one of"):
        dg._build_bidding_payload("MAGIC", None, None)


# ---------- _build_channel_controls ----------


def test_channel_all_channels_default():
    assert dg._build_channel_controls("ALL_CHANNELS", None) == {
        "channelStrategy": "ALL_CHANNELS"
    }


def test_channel_selected_requires_list():
    with pytest.raises(ValueError, match="channels list is required"):
        dg._build_channel_controls("SELECTED_CHANNELS", None)


def test_channel_selected_normalizes_names():
    result = dg._build_channel_controls(
        "SELECTED_CHANNELS",
        ["youtube_shorts", "YOUTUBE_IN_FEED", "discover"],
    )
    assert result == {
        "channelStrategy": "SELECTED_CHANNELS",
        "selectedChannels": {
            "youtubeShorts": True,
            "youtubeInFeed": True,
            "discover": True,
        },
    }


def test_channel_selected_rejects_unknown_channel():
    with pytest.raises(ValueError, match="channel 'tiktok'"):
        dg._build_channel_controls("SELECTED_CHANNELS", ["tiktok"])


def test_channel_selected_deduplicates_channels():
    result = dg._build_channel_controls(
        "SELECTED_CHANNELS",
        ["display", "display", "gmail"],
    )
    assert result["selectedChannels"] == {"display": True, "gmail": True}


def test_channel_rejects_unknown_strategy():
    with pytest.raises(ValueError, match="channel_strategy must"):
        dg._build_channel_controls("MYSTERY", None)


# ---------- _summarize_demand_gen ----------


def _camp(cid, name, cost=1_000_000, impressions=100, clicks=10, conversions=1.0, value=10.0):
    return {
        "campaign": {"id": cid, "name": name, "status": "ENABLED"},
        "campaignBudget": {"amountMicros": "5000000"},
        "metrics": {
            "costMicros": str(cost),
            "impressions": str(impressions),
            "clicks": str(clicks),
            "conversions": conversions,
            "conversionsValue": value,
        },
    }


def _ag(cid, name, campaign_id, campaign_name, strategy="ALL_CHANNELS"):
    return {
        "campaign": {"id": campaign_id, "name": campaign_name},
        "adGroup": {
            "id": cid,
            "name": name,
            "status": "ENABLED",
            "demandGenAdGroupSettings": {
                "channelControls": {"channelStrategy": strategy}
            },
        },
        "metrics": {
            "impressions": "50",
            "clicks": "5",
            "costMicros": "500000",
            "conversions": 0.5,
            "conversionsValue": 5.0,
        },
    }


def test_summarize_empty():
    report = dg._summarize_demand_gen(campaign_rows=[], ad_group_rows=[], days=30)
    assert report["summary"]["total_campaigns"] == 0
    assert report["summary"]["total_ad_groups"] == 0
    assert report["summary"]["totals"]["impressions"] == 0


def test_summarize_totals_and_computed_metrics():
    campaigns = [
        _camp("1", "A", cost=100_000_000, impressions=10_000, clicks=500, conversions=25, value=750),
        _camp("2", "B", cost=50_000_000, impressions=5_000, clicks=200, conversions=10, value=300),
    ]
    report = dg._summarize_demand_gen(
        campaign_rows=campaigns, ad_group_rows=[], days=30
    )
    totals = report["summary"]["totals"]
    assert totals["impressions"] == 15_000
    assert totals["clicks"] == 700
    assert totals["cost_micros"] == 150_000_000
    assert totals["conversions"] == 35.0
    assert totals["conversions_value"] == 1050.0
    # per-campaign ctr / cvr / roas
    a = next(c for c in report["campaigns"] if c["id"] == "1")
    assert a["ctr"] == 0.05
    assert a["cvr"] == 0.05
    # ROAS = value * 1M / cost_micros = 750 * 1_000_000 / 100_000_000 = 7.5
    assert a["roas"] == 7.5


def test_summarize_handles_zero_denominators():
    campaigns = [_camp("1", "Zero", cost=0, impressions=0, clicks=0, conversions=0, value=0)]
    report = dg._summarize_demand_gen(
        campaign_rows=campaigns, ad_group_rows=[], days=30
    )
    c = report["campaigns"][0]
    assert c["ctr"] == 0.0
    assert c["cvr"] == 0.0
    assert c["roas"] == 0.0


def test_summarize_ad_groups_carry_channel_strategy():
    ag = _ag("100", "AG main", "1", "A", strategy="SELECTED_CHANNELS")
    report = dg._summarize_demand_gen(campaign_rows=[], ad_group_rows=[ag], days=30)
    assert report["ad_groups"][0]["channel_strategy"] == "SELECTED_CHANNELS"
    assert report["ad_groups"][0]["campaign_id"] == "1"


def test_summarize_ad_group_without_channel_strategy_returns_none():
    row = {
        "campaign": {"id": "1", "name": "A"},
        "adGroup": {"id": "100", "name": "AG", "status": "ENABLED"},
        "metrics": {},
    }
    report = dg._summarize_demand_gen(campaign_rows=[], ad_group_rows=[row], days=30)
    assert report["ad_groups"][0]["channel_strategy"] is None
