"""Tests for the pure builders in agency.offline_conversions_tools."""
from __future__ import annotations

import hashlib

import pytest

from agency import offline_conversions_tools as oc


CID = "1234567890"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _consent(user="GRANTED", pers="GRANTED"):
    return {"ad_user_data": user, "ad_personalization": pers}


# ---------- _positive_int / _conversion_action_resource ----------


def test_conversion_action_resource_composed_correctly():
    assert (
        oc._conversion_action_resource(CID, "42")
        == f"customers/{CID}/conversionActions/42"
    )


@pytest.mark.parametrize("bad", ["", " ", "abc", "0", "-1"])
def test_conversion_action_resource_rejects_bad(bad):
    with pytest.raises(ValueError):
        oc._conversion_action_resource(CID, bad)


# ---------- _validate_datetime / _validate_currency / _optional_positive_float ----------


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-16 12:00:00",
        "2026-08-16T12:00:00",
        "2026-08-16 12:00:00-03:00",
        "2026-08-16T12:00:00+0000",
        "2026-08-16T12:00:00Z",
    ],
)
def test_validate_datetime_ok(value):
    assert oc._validate_datetime(value, "field") == value


@pytest.mark.parametrize("bad", ["2026-08-16", "16-08-2026 12:00:00", "just now", "2026/08/16 12:00:00"])
def test_validate_datetime_rejects(bad):
    with pytest.raises(ValueError):
        oc._validate_datetime(bad, "field")


@pytest.mark.parametrize("value", [None, "usd", "USD", "eur"])
def test_validate_currency_ok(value):
    result = oc._validate_currency(value)
    assert result is None or result == value.upper()


@pytest.mark.parametrize("bad", ["US", "USDS", "123"])
def test_validate_currency_rejects(bad):
    with pytest.raises(ValueError):
        oc._validate_currency(bad)


@pytest.mark.parametrize(("value", "expected"), [(None, None), ("3.5", 3.5), (10, 10.0), (0, 0.0)])
def test_optional_positive_float_ok(value, expected):
    assert oc._optional_positive_float(value, "field") == expected


def test_optional_positive_float_rejects_negative():
    with pytest.raises(ValueError, match="non-negative"):
        oc._optional_positive_float(-1, "field")


def test_optional_positive_float_rejects_non_numeric():
    with pytest.raises(ValueError, match="numeric"):
        oc._optional_positive_float("abc", "field")


# ---------- _build_click_conversion ----------


def test_click_conversion_with_gclid_and_consent():
    entry = oc._build_click_conversion(
        CID,
        {
            "conversion_action_id": "1000000001",
            "conversion_date_time": "2026-08-16 12:00:00-03:00",
            "conversion_value": 100.0,
            "currency_code": "usd",
            "gclid": "  Cj0KCQjw...  ",
            "consent": _consent(),
        },
    )
    assert entry["conversionAction"] == f"customers/{CID}/conversionActions/1000000001"
    assert entry["conversionDateTime"] == "2026-08-16 12:00:00-03:00"
    assert entry["conversionValue"] == 100.0
    assert entry["currencyCode"] == "USD"
    assert entry["gclid"] == "Cj0KCQjw..."
    assert entry["consent"] == {"adUserData": "GRANTED", "adPersonalization": "GRANTED"}


def test_click_conversion_requires_click_id_or_user_identifiers():
    with pytest.raises(ValueError, match="click ID .+ or user_identifiers"):
        oc._build_click_conversion(
            CID,
            {
                "conversion_action_id": "1",
                "conversion_date_time": "2026-08-16 12:00:00",
                "consent": _consent(),
            },
        )


def test_click_conversion_accepts_user_identifiers_for_ec4l():
    entry = oc._build_click_conversion(
        CID,
        {
            "conversion_action_id": "1",
            "conversion_date_time": "2026-08-16 12:00:00",
            "user_identifiers": [{"email": "user@example.com"}],
            "consent": _consent(),
        },
    )
    assert entry["userIdentifiers"] == [
        {"hashedEmail": _sha256("user@example.com")}
    ]


def test_click_conversion_accepts_multiple_click_ids():
    entry = oc._build_click_conversion(
        CID,
        {
            "conversion_action_id": "1",
            "conversion_date_time": "2026-08-16 12:00:00",
            "gbraid": "gbraid-value",
            "wbraid": "wbraid-value",
            "consent": _consent(),
        },
    )
    assert entry["gbraid"] == "gbraid-value"
    assert entry["wbraid"] == "wbraid-value"


def test_click_conversion_rejects_unspecified_consent():
    with pytest.raises(PermissionError, match="UNSPECIFIED"):
        oc._build_click_conversion(
            CID,
            {
                "conversion_action_id": "1",
                "conversion_date_time": "2026-08-16 12:00:00",
                "gclid": "x",
                "consent": {"ad_user_data": "UNSPECIFIED", "ad_personalization": "GRANTED"},
            },
        )


def test_click_conversion_requires_consent():
    with pytest.raises(PermissionError, match="consent is required"):
        oc._build_click_conversion(
            CID,
            {
                "conversion_action_id": "1",
                "conversion_date_time": "2026-08-16 12:00:00",
                "gclid": "x",
            },
        )


def test_click_conversion_order_id_and_custom_variables():
    entry = oc._build_click_conversion(
        CID,
        {
            "conversion_action_id": "1",
            "conversion_date_time": "2026-08-16 12:00:00",
            "gclid": "x",
            "order_id": "ORDER-42",
            "custom_variables": [
                {"conversion_custom_variable_id": "9", "value": "premium"}
            ],
            "consent": _consent(),
        },
    )
    assert entry["orderId"] == "ORDER-42"
    assert entry["customVariables"] == [
        {
            "conversionCustomVariable": f"customers/{CID}/conversionCustomVariables/9",
            "value": "premium",
        }
    ]


# ---------- _build_call_conversion ----------


def test_call_conversion_shape():
    entry = oc._build_call_conversion(
        CID,
        {
            "conversion_action_id": "1",
            "caller_id": "+15555550100",
            "call_start_date_time": "2026-08-16 11:00:00",
            "conversion_date_time": "2026-08-16 12:00:00",
            "conversion_value": 40.0,
            "currency_code": "USD",
            "consent": _consent(),
        },
    )
    assert entry["callerId"] == "+15555550100"
    assert entry["callStartDateTime"] == "2026-08-16 11:00:00"
    assert entry["consent"] == {"adUserData": "GRANTED", "adPersonalization": "GRANTED"}


def test_call_conversion_requires_caller_id():
    with pytest.raises(ValueError, match="caller_id is required"):
        oc._build_call_conversion(
            CID,
            {
                "conversion_action_id": "1",
                "call_start_date_time": "2026-08-16 11:00:00",
                "conversion_date_time": "2026-08-16 12:00:00",
                "consent": _consent(),
            },
        )


# ---------- _build_conversion_adjustment ----------


def test_adjustment_retraction_with_order_id():
    entry = oc._build_conversion_adjustment(
        CID,
        {
            "conversion_action_id": "1",
            "adjustment_date_time": "2026-08-16 15:00:00",
            "adjustment_type": "retraction",
            "order_id": "ORDER-42",
        },
    )
    assert entry["adjustmentType"] == "RETRACTION"
    assert entry["orderId"] == "ORDER-42"


def test_adjustment_restatement_requires_value():
    with pytest.raises(ValueError, match="restatement_value.adjusted_value"):
        oc._build_conversion_adjustment(
            CID,
            {
                "conversion_action_id": "1",
                "adjustment_date_time": "2026-08-16 15:00:00",
                "adjustment_type": "RESTATEMENT",
                "order_id": "ORDER-42",
            },
        )


def test_adjustment_restatement_shape():
    entry = oc._build_conversion_adjustment(
        CID,
        {
            "conversion_action_id": "1",
            "adjustment_date_time": "2026-08-16 15:00:00",
            "adjustment_type": "RESTATEMENT",
            "order_id": "ORDER-42",
            "restatement_value": {"adjusted_value": 150.0, "currency_code": "USD"},
        },
    )
    assert entry["restatementValue"] == {"adjustedValue": 150.0, "currencyCode": "USD"}


def test_adjustment_enhancement_requires_identifiers():
    with pytest.raises(ValueError, match="ENHANCEMENT requires user_identifiers"):
        oc._build_conversion_adjustment(
            CID,
            {
                "conversion_action_id": "1",
                "adjustment_date_time": "2026-08-16 15:00:00",
                "adjustment_type": "ENHANCEMENT",
                "order_id": "ORDER-42",
            },
        )


def test_adjustment_enhancement_hashes_identifiers():
    entry = oc._build_conversion_adjustment(
        CID,
        {
            "conversion_action_id": "1",
            "adjustment_date_time": "2026-08-16 15:00:00",
            "adjustment_type": "ENHANCEMENT",
            "order_id": "ORDER-42",
            "user_identifiers": [{"email": "user@example.com"}],
        },
    )
    assert entry["userIdentifiers"] == [
        {"hashedEmail": _sha256("user@example.com")}
    ]


def test_adjustment_requires_order_id_or_gclid_pair():
    with pytest.raises(ValueError, match="order_id OR gclid_date_time_pair"):
        oc._build_conversion_adjustment(
            CID,
            {
                "conversion_action_id": "1",
                "adjustment_date_time": "2026-08-16 15:00:00",
                "adjustment_type": "RETRACTION",
            },
        )


def test_adjustment_accepts_gclid_pair():
    entry = oc._build_conversion_adjustment(
        CID,
        {
            "conversion_action_id": "1",
            "adjustment_date_time": "2026-08-16 15:00:00",
            "adjustment_type": "RETRACTION",
            "gclid_date_time_pair": {
                "gclid": "gclid-abc",
                "conversion_date_time": "2026-08-15 12:00:00",
            },
        },
    )
    assert entry["gclidDateTimePair"] == {
        "gclid": "gclid-abc",
        "conversionDateTime": "2026-08-15 12:00:00",
    }


def test_adjustment_rejects_unknown_type():
    with pytest.raises(ValueError, match="adjustment_type must"):
        oc._build_conversion_adjustment(
            CID,
            {
                "conversion_action_id": "1",
                "adjustment_date_time": "2026-08-16 15:00:00",
                "adjustment_type": "OTHER",
                "order_id": "1",
            },
        )


# ---------- _build_operations batch limit ----------


def test_build_operations_rejects_empty():
    with pytest.raises(ValueError, match="non-empty list"):
        oc._build_operations(CID, [], oc._build_click_conversion)


def test_build_operations_enforces_limit(monkeypatch):
    monkeypatch.setattr(oc, "_MAX_CONVERSIONS_PER_CALL", 3)
    records = [
        {
            "conversion_action_id": "1",
            "conversion_date_time": "2026-08-16 12:00:00",
            "gclid": f"g{i}",
            "consent": _consent(),
        }
        for i in range(4)
    ]
    with pytest.raises(ValueError, match="limited to"):
        oc._build_operations(CID, records, oc._build_click_conversion)
