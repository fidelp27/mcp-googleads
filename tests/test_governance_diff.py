"""Tests for build_diff and build_diff_from_operations helpers in governance."""
from __future__ import annotations

import pytest

from agency.governance import (
    _get_nested,
    _snake_to_camel,
    build_diff,
    build_diff_from_operations,
)


# ---------- _snake_to_camel ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("amount_micros", "amountMicros"),
        ("name", "name"),
        ("bidding_strategy.target_cpa", "biddingStrategy.targetCpa"),
        ("a_b_c_d", "aBCD"),
        ("", ""),
    ],
)
def test_snake_to_camel(raw, expected):
    assert _snake_to_camel(raw) == expected


# ---------- _get_nested ----------


def test_get_nested_walks_dotted_path():
    obj = {"a": {"b": {"c": 42}}}
    assert _get_nested(obj, "a.b.c") == 42


def test_get_nested_returns_none_when_missing():
    obj = {"a": {"b": {}}}
    assert _get_nested(obj, "a.b.c") is None
    assert _get_nested(obj, "x.y.z") is None


def test_get_nested_handles_non_dict():
    assert _get_nested("string", "field") is None
    assert _get_nested(None, "field") is None


def test_get_nested_shallow():
    assert _get_nested({"foo": "bar"}, "foo") == "bar"


# ---------- build_diff without mask ----------


def test_diff_top_level_changes_only():
    before = {"name": "A", "status": "ENABLED", "budget": 100}
    after = {"name": "A", "status": "PAUSED", "budget": 200}
    diff = build_diff(before, after)
    assert diff == [
        {"field": "budget", "old": 100, "new": 200, "changed": True},
        {"field": "status", "old": "ENABLED", "new": "PAUSED", "changed": True},
    ]


def test_diff_no_changes_returns_empty_without_mask():
    before = {"x": 1}
    after = {"x": 1}
    assert build_diff(before, after) == []


def test_diff_added_and_removed_keys():
    before = {"a": 1, "b": 2}
    after = {"b": 2, "c": 3}
    diff = build_diff(before, after)
    fields = {e["field"] for e in diff}
    assert fields == {"a", "c"}


def test_diff_rejects_non_dict_inputs():
    assert build_diff("string", {"x": 1}) == []
    assert build_diff({"x": 1}, None) == []


# ---------- build_diff with mask ----------


def test_diff_mask_reports_all_mask_fields_including_unchanged():
    before = {"amountMicros": "5000000", "name": "Prospecting"}
    after = {"amountMicros": "8000000", "name": "Prospecting"}
    diff = build_diff(before, after, mask_fields=["amount_micros", "name"])
    assert diff == [
        {"field": "amount_micros", "old": "5000000", "new": "8000000", "changed": True},
        {"field": "name", "old": "Prospecting", "new": "Prospecting", "changed": False},
    ]


def test_diff_mask_normalizes_numeric_strings_vs_int():
    before = {"amountMicros": "5000000"}
    after = {"amountMicros": 5000000}
    diff = build_diff(before, after, mask_fields=["amount_micros"])
    assert diff == [{"field": "amount_micros", "old": "5000000", "new": 5000000, "changed": False}]


def test_diff_mask_handles_nested_paths():
    before = {"biddingStrategy": {"targetCpa": {"targetCpaMicros": "5000000"}}}
    after = {"biddingStrategy": {"targetCpa": {"targetCpaMicros": "7000000"}}}
    diff = build_diff(
        before, after, mask_fields=["bidding_strategy.target_cpa.target_cpa_micros"]
    )
    assert diff[0]["changed"] is True
    assert diff[0]["old"] == "5000000"
    assert diff[0]["new"] == "7000000"


def test_diff_mask_missing_key_returns_none_values():
    before = {}
    after = {"amountMicros": "5000000"}
    diff = build_diff(before, after, mask_fields=["amount_micros"])
    # Values are returned as-is (raw) but comparison is normalized.
    # Here previous is None so change is real.
    assert diff == [{"field": "amount_micros", "old": None, "new": "5000000", "changed": True}]


def test_diff_mask_ignores_blank_entries():
    before = {"a": 1}
    after = {"a": 2}
    diff = build_diff(before, after, mask_fields=["a", "", " "])
    assert len(diff) == 1
    assert diff[0]["field"] == "a"


# ---------- build_diff_from_operations ----------


def test_build_diff_from_ops_single_update():
    previous = {"amountMicros": "5000000", "name": "Prospecting"}
    operations = [
        {
            "update": {
                "resourceName": "customers/5461642152/campaignBudgets/999",
                "amountMicros": "8000000",
            },
            "updateMask": "amount_micros",
        }
    ]
    diffs = build_diff_from_operations(previous, operations)
    assert len(diffs) == 1
    entry = diffs[0]
    assert entry["resource"] == "customers/5461642152/campaignBudgets/999"
    assert entry["update_mask"] == ["amount_micros"]
    assert entry["changes"][0]["changed"] is True
    assert entry["changes"][0]["old"] == "5000000"
    assert entry["changes"][0]["new"] == "8000000"


def test_build_diff_from_ops_returns_empty_on_bad_shapes():
    assert build_diff_from_operations(None, []) == []
    assert build_diff_from_operations({}, []) == []
    assert build_diff_from_operations("string", [{}]) == []


def test_build_diff_from_ops_skips_non_update_operations():
    previous = {"name": "A"}
    operations = [
        {"create": {"name": "New"}},
        {"remove": "customers/1/campaigns/1"},
    ]
    assert build_diff_from_operations(previous, operations) == []


def test_build_diff_from_ops_supports_list_previous_states():
    previous = [
        {"amountMicros": "1000000"},
        {"amountMicros": "2000000"},
    ]
    operations = [
        {
            "update": {"resourceName": "b1", "amountMicros": "1500000"},
            "updateMask": "amount_micros",
        },
        {
            "update": {"resourceName": "b2", "amountMicros": "3000000"},
            "updateMask": "amount_micros",
        },
    ]
    diffs = build_diff_from_operations(previous, operations)
    assert len(diffs) == 2
    assert diffs[0]["changes"][0]["old"] == "1000000"
    assert diffs[0]["changes"][0]["new"] == "1500000"
    assert diffs[1]["changes"][0]["old"] == "2000000"
    assert diffs[1]["changes"][0]["new"] == "3000000"


def test_build_diff_from_ops_handles_missing_update_mask():
    previous = {"amountMicros": "5000000"}
    operations = [
        {
            "update": {"resourceName": "b1", "amountMicros": "7000000"},
            # no updateMask
        }
    ]
    diffs = build_diff_from_operations(previous, operations)
    # With empty mask list, build_diff falls back to comparing all keys
    assert len(diffs) == 1
    entry = diffs[0]
    # resourceName differs (previous doesn't have it) and amountMicros differs
    fields = {c["field"] for c in entry["changes"]}
    assert "amountMicros" in fields
