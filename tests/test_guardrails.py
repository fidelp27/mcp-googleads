"""Guardrail tests: whitelist, blocked tools, read-only mode and budget caps."""

import pytest

import google_ads_server as server
from agency.config import (
    ensure_customer_allowed,
    ensure_tool_allowed,
    normalize_customer_id,
)
from agency.mutations import execute_mutation


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9873186703", "9873186703"),
        ("987-318-6703", "9873186703"),
    ],
)
def test_normalize_customer_id_accepts_valid(raw, expected):
    assert normalize_customer_id(raw) == expected


@pytest.mark.parametrize("raw", ["", "12345", "12345678901", "not-an-id"])
def test_normalize_customer_id_rejects_invalid(raw):
    with pytest.raises(ValueError):
        normalize_customer_id(raw)


def test_ensure_customer_allowed_permits_when_whitelist_empty(agency_data, monkeypatch):
    monkeypatch.delenv("ALLOWED_CUSTOMER_IDS", raising=False)
    assert ensure_customer_allowed("987-318-6703") == "9873186703"


def test_ensure_customer_allowed_permits_whitelisted(agency_data, monkeypatch):
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "9873186703, 1111111111")
    assert ensure_customer_allowed("9873186703") == "9873186703"


def test_ensure_customer_allowed_rejects_non_whitelisted(agency_data, monkeypatch):
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1111111111")
    with pytest.raises(PermissionError):
        ensure_customer_allowed("9873186703")


def test_read_only_mode_blocks_write_tools(agency_data, monkeypatch):
    monkeypatch.setenv("READ_ONLY_MODE", "true")
    with pytest.raises(PermissionError):
        ensure_tool_allowed("pause_campaign", write=True)


def test_read_only_mode_allows_read_tools(agency_data, monkeypatch):
    monkeypatch.setenv("READ_ONLY_MODE", "true")
    # Read tools must remain callable even while writes are frozen.
    ensure_tool_allowed("get_campaign_performance", write=False)


def test_blocked_tools_are_rejected(agency_data, monkeypatch):
    monkeypatch.setenv("READ_ONLY_MODE", "false")
    monkeypatch.setenv("BLOCKED_TOOLS", "remove_campaign")
    with pytest.raises(PermissionError):
        ensure_tool_allowed("remove_campaign", write=True)


def test_non_whitelisted_customer_blocks_mutation(agency_data, monkeypatch):
    monkeypatch.setenv("READ_ONLY_MODE", "false")
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1111111111")
    called = []
    monkeypatch.setattr(
        "agency.mutations.mutate",
        lambda *a, **k: called.append(True) or {},
    )
    with pytest.raises(PermissionError):
        execute_mutation(
            tool_name="pause_campaign",
            customer_id="9873186703",
            service="campaigns",
            operations=[{"update": {}, "updateMask": "status"}],
            dry_run=True,
            approval_id=None,
            confirmation_text=None,
        )
    assert called == []


def test_budget_cap_blocks_amount_over_limit(agency_data, monkeypatch):
    monkeypatch.setenv("READ_ONLY_MODE", "false")
    monkeypatch.setenv("MAX_DAILY_BUDGET_INCREASE_MICROS", "1000000")
    called = []
    monkeypatch.setattr(
        "agency.mutations.mutate",
        lambda *a, **k: called.append(True) or {},
    )
    create_budget = server.mcp._tool_manager.get_tool("create_campaign_budget").fn
    with pytest.raises(PermissionError):
        create_budget(
            customer_id="9873186703",
            name="Over cap",
            amount_micros=5_000_000,
            dry_run=True,
        )
    # The cap is enforced before any Google Ads call is attempted.
    assert called == []


def test_budget_within_cap_is_validated(agency_data, monkeypatch):
    monkeypatch.setenv("READ_ONLY_MODE", "false")
    monkeypatch.setenv("MAX_DAILY_BUDGET_INCREASE_MICROS", "5000000")
    validate_calls = []
    monkeypatch.setattr(
        "agency.mutations.mutate",
        lambda customer_id, service, operations, *, validate_only, partial_failure=False: (
            validate_calls.append(validate_only) or {"results": [{}]}
        ),
    )
    create_budget = server.mcp._tool_manager.get_tool("create_campaign_budget").fn
    create_budget(
        customer_id="9873186703",
        name="Within cap",
        amount_micros=1_000_000,
        dry_run=True,
    )
    assert validate_calls == [True]
