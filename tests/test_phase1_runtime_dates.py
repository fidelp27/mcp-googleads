"""Phase 1 tests: account timezone date windows, conversion lag, time-series trends, multi-MCC routing, and RuntimeContext."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from agency import api, reports
from agency.dates import coverage_metadata, resolve_date_range
from agency.runtime import (
    LocalFileStorageBackend,
    RuntimeContext,
    clear_session_login_map,
    register_login_customer,
    resolve_login_customer_id,
    use_runtime_context,
)


# ---------- Timezone, complete days, and conversion lag ----------


def test_resolve_date_range_respects_account_time_zone_and_complete_days():
    # 2026-07-15 01:30 UTC:
    # - Europe/Madrid (UTC+2) is already 2026-07-15 -> yesterday is 2026-07-14
    # - America/New_York (UTC-4) is still 2026-07-14 -> yesterday is 2026-07-13
    fixed_now = datetime(2026, 7, 15, 1, 30, tzinfo=timezone.utc)

    madrid = resolve_date_range(
        days=7, time_zone="Europe/Madrid", complete_days_only=True, now_utc=fixed_now
    )
    ny = resolve_date_range(
        days=7, time_zone="America/New_York", complete_days_only=True, now_utc=fixed_now
    )

    assert madrid.time_zone == "Europe/Madrid"
    assert madrid.time_zone_fallback is False
    assert madrid.end.isoformat() == "2026-07-14"
    assert madrid.start.isoformat() == "2026-07-08"
    assert madrid.includes_incomplete_today is False
    assert madrid.conversion_lag_warning is not None

    assert ny.time_zone == "America/New_York"
    assert ny.end.isoformat() == "2026-07-13"
    assert ny.start.isoformat() == "2026-07-07"


def test_resolve_date_range_historical_window_has_no_lag_warning():
    fixed_now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    win = resolve_date_range(
        time_zone="Europe/Madrid",
        start_date="2026-05-01",
        end_date="2026-05-31",
        now_utc=fixed_now,
    )
    assert win.complete_days_only is True
    assert win.includes_incomplete_today is False
    assert win.conversion_lag_warning is None


def test_resolve_date_range_unknown_timezone_falls_back_gracefully():
    fixed_now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    win = resolve_date_range(days=7, time_zone="Mars/Olympus_Mons", now_utc=fixed_now)
    assert win.time_zone == "UTC"
    assert win.time_zone_fallback is True
    meta = coverage_metadata(currency_code="EUR", window=win, now_utc=fixed_now)
    assert meta["currency_code"] == "EUR"
    assert any("UTC" in w for w in meta["coverage_warnings"])


# ---------- Dynamic multi-MCC routing & RuntimeContext ----------


def test_resolve_login_customer_id_supports_multi_mcc_and_direct_accounts(monkeypatch):
    clear_session_login_map()
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "7777777777")
    monkeypatch.setenv(
        "GOOGLE_ADS_LOGIN_CUSTOMER_MAP",
        "1111111111:9999999999,2222222222:8888888888,3333333333:",
    )

    assert resolve_login_customer_id("1111111111") == "9999999999"
    assert resolve_login_customer_id("2222222222") == "8888888888"
    assert resolve_login_customer_id("3333333333") is None  # explicit direct account
    assert resolve_login_customer_id("4444444444") == "7777777777"  # global fallback


def test_api_headers_strip_global_mcc_header_for_direct_account(monkeypatch):
    clear_session_login_map()
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "7777777777")
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_MAP", "3333333333:,1111111111:9999999999")

    class _FakeServer:
        def get_credentials(self):
            return None

        def get_headers(self, creds):
            return {
                "Authorization": "Bearer test",
                "login-customer-id": "7777777777",
            }

    monkeypatch.setattr(api, "_server_module", lambda: _FakeServer())

    direct_headers = api._headers(customer_id="3333333333")
    assert "login-customer-id" not in direct_headers

    mcc_headers = api._headers(customer_id="1111111111")
    assert mcc_headers["login-customer-id"] == "9999999999"


def test_runtime_context_isolation_and_storage_backend(tmp_path):
    clear_session_login_map()
    register_login_customer("1111111111", "9999999999")
    assert resolve_login_customer_id("1111111111") == "9999999999"

    storage = LocalFileStorageBackend(tmp_path)
    ctx = RuntimeContext(
        tenant_id="tenant-acme",
        operator_name="alice",
        login_customer_map={"1111111111": "8888888888"},
        storage=storage,
    )
    with use_runtime_context(ctx):
        assert resolve_login_customer_id("1111111111") == "8888888888"
        ctx.storage.append_jsonl("events", {"ok": True})
        assert ctx.storage.read_jsonl("events") == [{"ok": True}]

    # Outside context, session map is preserved cleanly
    assert resolve_login_customer_id("1111111111") == "9999999999"
    clear_session_login_map()


# ---------- get_performance_trends & compare_periods ----------


class _FakeMCP:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, **kwargs):
        def dec(fn):
            self.tools[fn.__name__] = fn
            return fn

        return dec


def test_get_performance_trends_daily_and_weekly_in_demo_mode(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_DEMO_MODE", "true")
    mcp = _FakeMCP()
    reports.register_report_tools(mcp)

    daily = json.loads(
        mcp.tools["get_performance_trends"](
            customer_id="1111111111",
            days=7,
            granularity="daily",
            complete_days_only=True,
            include_conversions_by_conversion_date=True,
        )
    )
    assert daily["currency_code"] == "EUR"
    assert daily["time_zone"] == "Europe/Madrid"
    assert daily["complete_days_only"] is True
    assert daily["conversion_lag_warning"] is not None
    assert len(daily["series"]) == 7
    assert "date" in daily["series"][0]
    assert daily["totals"]["conversions_by_conversion_date"] > 0

    weekly = json.loads(
        mcp.tools["get_performance_trends"](
            customer_id="2222222222",
            days=14,
            granularity="weekly",
        )
    )
    assert weekly["currency_code"] == "USD"
    assert weekly["time_zone"] == "America/New_York"
    assert len(weekly["series"]) == 2
    assert "week" in weekly["series"][0]


def test_compare_periods_returns_currency_timezone_and_metadata(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_DEMO_MODE", "true")
    mcp = _FakeMCP()
    reports.register_report_tools(mcp)

    res = json.loads(
        mcp.tools["compare_periods"](
            customer_id="1111111111",
            period_a="2026-05-01:2026-05-15",
            period_b="2026-05-16:2026-05-30",
        )
    )
    assert res["currency_code"] == "EUR"
    assert res["time_zone"] == "Europe/Madrid"
    assert res["queried_at_utc"]
    assert res["period_a"]["start"] == "2026-05-01"
    assert "cost_micros" in res["metrics"]
