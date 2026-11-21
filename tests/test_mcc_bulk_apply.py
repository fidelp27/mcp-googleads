"""Tests for the MCC bulk apply helpers.

The end-to-end mutation flow is smoke-tested with the real API; here we
cover the pure prep helpers plus the per-operation builders (with the
Google Ads `search` call monkey-patched).
"""
from __future__ import annotations

import pytest

from agency import mcc_tools


CID_1 = "1234567890"
CID_2 = "1234567890"  # same customer; tests only need it to pass allowlist


@pytest.fixture(autouse=True)
def _allow_customers(monkeypatch):
    monkeypatch.setattr(
        mcc_tools, "ensure_customer_allowed", lambda cid: str(cid).replace("-", "")
    )
    yield


def _patch_search(monkeypatch, rows_by_query):
    """rows_by_query = {substring -> rows}. First match wins."""
    def fake_search(customer_id, query):
        for needle, rows in rows_by_query.items():
            if needle in query:
                return rows
        return []

    monkeypatch.setattr(mcc_tools, "search", fake_search)


# ---------- _prepare_bulk_targets validation ----------


def test_prepare_rejects_unknown_operation():
    with pytest.raises(ValueError, match="operation_name must be one of"):
        mcc_tools._prepare_bulk_targets("nonexistent", {CID_1: {"campaign_id": "1"}})


def test_prepare_rejects_non_dict_targets():
    with pytest.raises(ValueError, match="targets must be a non-empty dict"):
        mcc_tools._prepare_bulk_targets("pause_campaign", None)


def test_prepare_rejects_empty_targets():
    with pytest.raises(ValueError, match="targets must be a non-empty dict"):
        mcc_tools._prepare_bulk_targets("pause_campaign", {})


def test_prepare_rejects_too_many_targets():
    targets = {str(i).zfill(10): {"campaign_id": "1"} for i in range(mcc_tools.MAX_BULK_TARGETS + 1)}
    with pytest.raises(ValueError, match="limited to"):
        mcc_tools._prepare_bulk_targets("pause_campaign", targets)


# ---------- _bulk_pause_campaign builder ----------


def test_pause_campaign_requires_campaign_id():
    with pytest.raises(ValueError, match="'campaign_id'"):
        mcc_tools._bulk_pause_campaign(CID_1, {})


def test_pause_campaign_builds_correct_update(monkeypatch):
    _patch_search(
        monkeypatch,
        {"FROM campaign": [{"campaign": {"status": "ENABLED", "name": "Search LatAm"}}]},
    )
    info = mcc_tools._bulk_pause_campaign(CID_1, {"campaign_id": "123"})
    assert info["service"] == "campaigns"
    op = info["operations"][0]
    assert op["update"]["resourceName"] == f"customers/{CID_1}/campaigns/123"
    assert op["update"]["status"] == "PAUSED"
    assert op["updateMask"] == "status"
    assert info["previous_state"] == {"status": "ENABLED"}
    assert info["target_name"] == "Search LatAm"


def test_pause_campaign_raises_when_campaign_missing(monkeypatch):
    _patch_search(monkeypatch, {"FROM campaign": []})
    with pytest.raises(ValueError, match="not found"):
        mcc_tools._bulk_pause_campaign(CID_1, {"campaign_id": "999"})


def test_pause_campaign_rejects_non_positive_id(monkeypatch):
    _patch_search(monkeypatch, {"FROM campaign": [{"campaign": {"status": "ENABLED"}}]})
    with pytest.raises(ValueError, match="positive integer"):
        mcc_tools._bulk_pause_campaign(CID_1, {"campaign_id": "abc"})
    with pytest.raises(ValueError, match="positive integer"):
        mcc_tools._bulk_pause_campaign(CID_1, {"campaign_id": "0"})


# ---------- _bulk_pause_ad builder ----------


def test_pause_ad_requires_both_ids():
    with pytest.raises(ValueError, match="'ad_group_id' and 'ad_id'"):
        mcc_tools._bulk_pause_ad(CID_1, {"ad_group_id": "1"})


def test_pause_ad_resource_uses_composite_key(monkeypatch):
    _patch_search(
        monkeypatch,
        {"FROM ad_group_ad": [{"adGroupAd": {"status": "ENABLED"}}]},
    )
    info = mcc_tools._bulk_pause_ad(
        CID_1, {"ad_group_id": "200", "ad_id": "800"}
    )
    op = info["operations"][0]
    assert op["update"]["resourceName"] == f"customers/{CID_1}/adGroupAds/200~800"
    assert op["update"]["status"] == "PAUSED"
    assert op["updateMask"] == "status"


def test_pause_ad_missing_raises(monkeypatch):
    _patch_search(monkeypatch, {"FROM ad_group_ad": []})
    with pytest.raises(ValueError, match="not found"):
        mcc_tools._bulk_pause_ad(CID_1, {"ad_group_id": "1", "ad_id": "2"})


# ---------- _bulk_update_budget builder ----------


def _budget_row(amount_micros, name="Prospecting"):
    return {"campaignBudget": {"amountMicros": str(amount_micros), "name": name}}


def test_update_budget_requires_fields():
    with pytest.raises(ValueError, match="'budget_id' and 'amount_micros'"):
        mcc_tools._bulk_update_budget(CID_1, {})


def test_update_budget_rejects_non_integer_amount(monkeypatch):
    _patch_search(monkeypatch, {"FROM campaign_budget": [_budget_row(1_000_000)]})
    with pytest.raises(ValueError, match="must be an integer"):
        mcc_tools._bulk_update_budget(
            CID_1, {"budget_id": "1", "amount_micros": "abc"}
        )


def test_update_budget_rejects_non_positive(monkeypatch):
    _patch_search(monkeypatch, {"FROM campaign_budget": [_budget_row(1_000_000)]})
    with pytest.raises(ValueError, match="must be positive"):
        mcc_tools._bulk_update_budget(
            CID_1, {"budget_id": "1", "amount_micros": 0}
        )


def test_update_budget_builds_mask_and_prev_state(monkeypatch):
    _patch_search(
        monkeypatch,
        {"FROM campaign_budget": [_budget_row(1_000_000, name="Prospecting")]},
    )
    info = mcc_tools._bulk_update_budget(
        CID_1, {"budget_id": "555", "amount_micros": 2_000_000}
    )
    assert info["service"] == "campaignBudgets"
    op = info["operations"][0]
    assert op["update"]["resourceName"] == f"customers/{CID_1}/campaignBudgets/555"
    assert op["update"]["amountMicros"] == "2000000"
    assert op["updateMask"] == "amountMicros"
    assert info["previous_state"] == {
        "amount_micros": 1_000_000,
        "name": "Prospecting",
    }
    assert info["target_name"] == "Prospecting"


def test_update_budget_absolute_cap_enforced(monkeypatch):
    _patch_search(monkeypatch, {"FROM campaign_budget": [_budget_row(1_000_000)]})

    class Settings:
        max_daily_budget_increase_micros = 500_000
        max_budget_increase_pct = 100

    monkeypatch.setattr(mcc_tools, "get_settings", lambda: Settings())
    with pytest.raises(ValueError, match="MAX_DAILY_BUDGET_INCREASE_MICROS"):
        mcc_tools._bulk_update_budget(
            CID_1, {"budget_id": "1", "amount_micros": 2_000_000}  # +1M > 500k cap
        )


def test_update_budget_percentage_cap_enforced(monkeypatch):
    _patch_search(monkeypatch, {"FROM campaign_budget": [_budget_row(1_000_000)]})

    class Settings:
        max_daily_budget_increase_micros = 0  # disable absolute cap
        max_budget_increase_pct = 25

    monkeypatch.setattr(mcc_tools, "get_settings", lambda: Settings())
    with pytest.raises(ValueError, match="MAX_BUDGET_INCREASE_PCT"):
        mcc_tools._bulk_update_budget(
            CID_1, {"budget_id": "1", "amount_micros": 1_500_000}  # +50% > 25% cap
        )


def test_update_budget_decrease_ignores_caps(monkeypatch):
    _patch_search(monkeypatch, {"FROM campaign_budget": [_budget_row(1_000_000)]})

    class Settings:
        max_daily_budget_increase_micros = 100_000
        max_budget_increase_pct = 5

    monkeypatch.setattr(mcc_tools, "get_settings", lambda: Settings())
    # decrease — should pass without hitting caps
    info = mcc_tools._bulk_update_budget(
        CID_1, {"budget_id": "1", "amount_micros": 500_000}
    )
    assert info["operations"][0]["update"]["amountMicros"] == "500000"


# ---------- _prepare_bulk_targets end-to-end with builder ----------


def test_prepare_returns_customer_ordered_items(monkeypatch):
    _patch_search(
        monkeypatch,
        {"FROM campaign": [{"campaign": {"status": "ENABLED", "name": "Any"}}]},
    )
    prepared = mcc_tools._prepare_bulk_targets(
        "pause_campaign",
        {
            "1234567890": {"campaign_id": "1"},
            "9876543210": {"campaign_id": "2"},
        },
    )
    assert len(prepared) == 2
    for item in prepared:
        assert item["service"] == "campaigns"
        assert item["operations"][0]["update"]["status"] == "PAUSED"
        assert item["previous_state"] == {"status": "ENABLED"}


def test_prepare_bubbles_builder_error_with_customer_context(monkeypatch):
    _patch_search(monkeypatch, {"FROM campaign": [{"campaign": {"status": "ENABLED"}}]})
    with pytest.raises(ValueError, match=r"targets\[1234567890\]"):
        mcc_tools._prepare_bulk_targets(
            "pause_campaign", {"1234567890": {}}
        )
