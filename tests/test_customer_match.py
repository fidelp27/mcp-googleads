"""Tests for Customer Match helpers.

We cover the pure hashing / normalization / consent / operation-building
paths; the OfflineUserDataJobService pipeline is smoke-tested against
the real API separately.
"""
from __future__ import annotations

import hashlib

import pytest

from agency import customer_match_tools as cm


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------- _normalize_email ----------


def test_normalize_email_lowercases_and_trims():
    assert cm._normalize_email("  John.Doe@Example.COM  ") == "john.doe@example.com"


def test_normalize_email_strips_gmail_dots_and_plus_tags():
    assert cm._normalize_email("john.doe+promo@gmail.com") == "johndoe@gmail.com"
    assert cm._normalize_email("j.o.h.n@googlemail.com") == "john@googlemail.com"


def test_normalize_email_rejects_missing_at():
    with pytest.raises(ValueError, match="not a valid email"):
        cm._normalize_email("not-an-email")


# ---------- _normalize_phone ----------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+1 (234) 567-8901", "+12345678901"),
        ("+34 600 123 456", "+34600123456"),
        ("54 11 5555 5555", "+541155555555"),
    ],
)
def test_normalize_phone_strips_formatting_and_adds_plus(raw, expected):
    assert cm._normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "bad",
    ["", " ", "12345", "abc-def", "+123456789012345678"],  # too short/long
)
def test_normalize_phone_rejects_invalid(bad):
    with pytest.raises(ValueError):
        cm._normalize_phone(bad)


# ---------- _looks_hashed ----------


def test_looks_hashed_detects_sha256_hex():
    assert cm._looks_hashed(_sha256("anything")) is True
    assert cm._looks_hashed("abc") is False
    assert cm._looks_hashed("ABC" * 22) is False  # uppercase — accepted via lower()


def test_looks_hashed_case_insensitive():
    hashed = _sha256("x").upper()
    assert cm._looks_hashed(hashed) is True


# ---------- _hash_email / _hash_phone ----------


def test_hash_email_passes_through_prehashed_values():
    prehashed = _sha256("someone@example.com")
    assert cm._hash_email(prehashed) == prehashed


def test_hash_email_hashes_normalized_form():
    plain = "John.Doe+x@Gmail.com"
    assert cm._hash_email(plain) == _sha256("johndoe@gmail.com")


def test_hash_phone_hashes_normalized_form():
    plain = "+1 (234) 567-8901"
    assert cm._hash_phone(plain) == _sha256("+12345678901")


# ---------- _build_user_identifiers ----------


def test_build_identifier_from_plain_email():
    ids = cm._build_user_identifiers({"email": "user@example.com"})
    assert ids == [{"hashedEmail": _sha256("user@example.com")}]


def test_build_identifier_from_prehashed_email():
    h = _sha256("user@example.com")
    ids = cm._build_user_identifiers({"hashed_email": h})
    assert ids == [{"hashedEmail": h}]


def test_build_identifier_from_phone():
    ids = cm._build_user_identifiers({"phone": "+1 234 567 8901"})
    assert ids == [{"hashedPhoneNumber": _sha256("+12345678901")}]


def test_build_identifier_mobile_id_not_hashed():
    ids = cm._build_user_identifiers({"mobile_id": "GAID-xyz "})
    assert ids == [{"mobileId": "GAID-xyz"}]


def test_build_identifier_third_party_user_id_not_hashed():
    ids = cm._build_user_identifiers({"third_party_user_id": "crm-42"})
    assert ids == [{"thirdPartyUserId": "crm-42"}]


def test_build_identifier_address_info():
    ids = cm._build_user_identifiers(
        {
            "address_info": {
                "first_name": "John",
                "last_name": "Doe",
                "country_code": "us",
                "postal_code": "94103",
            }
        }
    )
    assert ids == [
        {
            "addressInfo": {
                "hashedFirstName": _sha256("john"),
                "hashedLastName": _sha256("doe"),
                "countryCode": "US",
                "postalCode": "94103",
            }
        }
    ]


def test_build_identifier_multi_field_creates_multiple_entries():
    ids = cm._build_user_identifiers(
        {"email": "a@b.com", "phone": "+1234567890"}
    )
    assert ids == [
        {"hashedEmail": _sha256("a@b.com")},
        {"hashedPhoneNumber": _sha256("+1234567890")},
    ]


def test_build_identifier_empty_member_rejected():
    with pytest.raises(ValueError, match="requires at least one of"):
        cm._build_user_identifiers({})


def test_build_identifier_address_missing_field_rejected():
    with pytest.raises(ValueError, match="address_info requires"):
        cm._build_user_identifiers(
            {"address_info": {"first_name": "x", "last_name": "y"}}
        )


# ---------- _normalize_consent ----------


def test_consent_none_rejected():
    with pytest.raises(PermissionError, match="consent is required"):
        cm._normalize_consent(None)
    with pytest.raises(PermissionError, match="consent is required"):
        cm._normalize_consent({})


def test_consent_unspecified_rejected():
    with pytest.raises(PermissionError, match="UNSPECIFIED"):
        cm._normalize_consent(
            {"ad_user_data": "UNSPECIFIED", "ad_personalization": "GRANTED"}
        )


def test_consent_invalid_value_rejected():
    with pytest.raises(ValueError, match="must be in"):
        cm._normalize_consent(
            {"ad_user_data": "MAYBE", "ad_personalization": "GRANTED"}
        )


def test_consent_valid_returns_camel_case_keys():
    result = cm._normalize_consent(
        {"ad_user_data": "GRANTED", "ad_personalization": "DENIED"}
    )
    assert result == {"adUserData": "GRANTED", "adPersonalization": "DENIED"}


# ---------- _build_operations ----------


def test_build_operations_wraps_each_member():
    members = [
        {"email": "a@b.com"},
        {"phone": "+15555555555"},
    ]
    consent = {"adUserData": "GRANTED", "adPersonalization": "GRANTED"}
    ops = cm._build_operations(members, "create", consent)
    assert len(ops) == 2
    assert ops[0]["create"]["consent"] == consent
    assert ops[0]["create"]["userIdentifiers"] == [
        {"hashedEmail": _sha256("a@b.com")}
    ]
    assert ops[1]["create"]["userIdentifiers"] == [
        {"hashedPhoneNumber": _sha256("+15555555555")}
    ]


def test_build_operations_supports_remove_verb():
    consent = {"adUserData": "GRANTED", "adPersonalization": "GRANTED"}
    ops = cm._build_operations([{"email": "a@b.com"}], "remove", consent)
    assert list(ops[0].keys()) == ["remove"]


def test_build_operations_rejects_non_list():
    consent = {"adUserData": "GRANTED", "adPersonalization": "GRANTED"}
    with pytest.raises(ValueError, match="non-empty list"):
        cm._build_operations([], "create", consent)
    with pytest.raises(ValueError, match="non-empty list"):
        cm._build_operations(None, "create", consent)


def test_build_operations_enforces_max_members(monkeypatch):
    monkeypatch.setattr(cm, "_MAX_MEMBERS_PER_CALL", 3)
    consent = {"adUserData": "GRANTED", "adPersonalization": "GRANTED"}
    members = [{"email": f"u{i}@b.com"} for i in range(4)]
    with pytest.raises(ValueError, match="limited to"):
        cm._build_operations(members, "create", consent)


def test_build_operations_rejects_non_dict_member():
    consent = {"adUserData": "GRANTED", "adPersonalization": "GRANTED"}
    with pytest.raises(ValueError, match="members\\[0\\] must be a dict"):
        cm._build_operations(["not a dict"], "create", consent)


# ---------- _chunks + _user_list_resource ----------


def test_chunks_splits_into_size_batches():
    ops = list(range(25))
    batches = list(cm._chunks(ops, 10))
    assert [len(b) for b in batches] == [10, 10, 5]


def test_user_list_resource_composed_correctly():
    assert (
        cm._user_list_resource("5461642152", "999")
        == "customers/5461642152/userLists/999"
    )


@pytest.mark.parametrize("bad", ["", "abc", "0", "-3"])
def test_user_list_resource_rejects_bad_ids(bad):
    with pytest.raises(ValueError):
        cm._user_list_resource("5461642152", bad)
