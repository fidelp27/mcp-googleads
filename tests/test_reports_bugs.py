"""Regression tests for P0 fixes in reports, alerts, multi-currency and Docker packaging."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from agency import reports
from agency.reports import (
    _consolidate_by_currency,
    _resolve_alert_customer_ids,
    register_report_tools,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, **kwargs):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorator


# ---------- check_alerts ----------


def test_resolve_alert_ids_ignores_defaults_and_falls_back_to_clients(monkeypatch):
    monkeypatch.setattr(
        reports,
        "load_json_config",
        lambda filename, default: {"1111111111": {"name": "Client A"}}
        if filename == "clients.json"
        else default,
    )
    ids = _resolve_alert_customer_ids(
        None,
        {"defaults": {"max_daily_spend_micros": 100_000_000}},
    )
    assert ids == ["1111111111"]


def test_resolve_alert_ids_uses_customer_entries_excluding_defaults(monkeypatch):
    monkeypatch.setattr(reports, "load_json_config", lambda filename, default: default)
    ids = _resolve_alert_customer_ids(
        None,
        {
            "defaults": {"min_ctr": 0.01},
            "2222222222": {"min_ctr": 0.02},
        },
    )
    assert ids == ["2222222222"]


def test_resolve_alert_ids_falls_back_to_allowed_customer_ids(monkeypatch):
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "3333333333,1111111111")
    monkeypatch.setattr(reports, "load_json_config", lambda filename, default: {})
    ids = _resolve_alert_customer_ids(None, {"defaults": {"min_ctr": 0.01}})
    assert ids == ["1111111111", "3333333333"]


def test_resolve_alert_ids_raises_when_none_configured(monkeypatch):
    monkeypatch.delenv("ALLOWED_CUSTOMER_IDS", raising=False)
    monkeypatch.setattr(reports, "load_json_config", lambda filename, default: {})
    with pytest.raises(ValueError, match="Provide customer_ids"):
        _resolve_alert_customer_ids(None, {"defaults": {"min_ctr": 0.01}})


def test_check_alerts_tool_works_with_defaults_only_config(monkeypatch):
    mcp = _FakeMCP()
    register_report_tools(mcp)
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1111111111")
    monkeypatch.setattr(
        reports,
        "load_json_config",
        lambda filename, default: {"defaults": {"max_daily_spend_micros": 50_000_000}}
        if filename == "alerts.json"
        else {},
    )

    def fake_search(customer_id: str, query: str):
        if "DURING YESTERDAY" in query and "FROM campaign" in query:
            return [
                {
                    "campaign": {"id": "10", "name": "Brand"},
                    "metrics": {
                        "impressions": "1000",
                        "clicks": "50",
                        "ctr": 0.05,
                        "costMicros": "80000000",
                        "conversions": 2,
                        "costPerConversion": 40_000_000,
                    },
                }
            ]
        return []

    monkeypatch.setattr(reports, "search", fake_search)
    raw = mcp.tools["check_alerts"]()
    payload = json.loads(raw)
    assert payload["count"] == 1
    assert payload["alerts"][0]["customer_id"] == "1111111111"
    assert payload["alerts"][0]["type"] == "daily_spend"


# ---------- multi-currency dashboard ----------


def test_consolidate_by_currency_keeps_mixed_currencies_separate():
    results = {
        "1111111111": {
            "currency_code": "EUR",
            "metrics": {
                "impressions": 1000.0,
                "clicks": 100.0,
                "cost_micros": 50_000_000.0,
                "conversions": 5.0,
                "conversions_value": 250.0,
            },
        },
        "2222222222": {
            "currency_code": "ARS",
            "metrics": {
                "impressions": 2000.0,
                "clicks": 200.0,
                "cost_micros": 100_000_000_000.0,
                "conversions": 10.0,
                "conversions_value": 500_000.0,
            },
        },
    }
    totals, by_currency, currencies, mixed, warning = _consolidate_by_currency(results)
    assert totals is None
    assert mixed is True
    assert currencies == ["ARS", "EUR"]
    assert set(by_currency.keys()) == {"ARS", "EUR"}
    assert by_currency["EUR"]["cost_micros"] == 50_000_000.0
    assert by_currency["ARS"]["cost_micros"] == 100_000_000_000.0
    assert warning is not None and "ARS, EUR" in warning


def test_consolidate_by_currency_combines_single_currency():
    results = {
        "1111111111": {
            "currency_code": "USD",
            "metrics": {
                "impressions": 1000.0,
                "clicks": 50.0,
                "cost_micros": 10_000_000.0,
                "conversions": 2.0,
                "conversions_value": 100.0,
            },
        },
        "2222222222": {
            "currency_code": "USD",
            "metrics": {
                "impressions": 500.0,
                "clicks": 25.0,
                "cost_micros": 20_000_000.0,
                "conversions": 3.0,
                "conversions_value": 150.0,
            },
        },
    }
    totals, by_currency, currencies, mixed, warning = _consolidate_by_currency(results)
    assert mixed is False
    assert warning is None
    assert currencies == ["USD"]
    assert totals is not None
    assert totals["currency_code"] == "USD"
    assert totals["cost_micros"] == 30_000_000.0
    assert by_currency["USD"]["conversions"] == 5.0


def test_generate_agency_dashboard_separates_currencies(monkeypatch):
    mcp = _FakeMCP()
    register_report_tools(mcp)
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1111111111,2222222222")

    def fake_load_config(filename: str, default):
        if filename == "clients.json":
            return {"1111111111": {"name": "Spain"}, "2222222222": {"name": "Argentina"}}
        if filename == "alerts.json":
            return {"defaults": {"min_ctr": 0.05}}
        return default

    def fake_search(customer_id: str, query: str):
        if "customer.currency_code" in query:
            return [
                {
                    "customer": {
                        "currencyCode": "EUR" if customer_id == "1111111111" else "ARS",
                        "timeZone": "Europe/Madrid"
                        if customer_id == "1111111111"
                        else "America/Argentina/Buenos_Aires",
                    }
                }
            ]
        if "FROM campaign" in query:
            return [
                {
                    "campaign": {"id": "1", "name": "Search", "status": "ENABLED"},
                    "metrics": {
                        "impressions": "1000",
                        "clicks": "20",
                        "costMicros": "10000000"
                        if customer_id == "1111111111"
                        else "50000000000",
                        "conversions": 2,
                        "conversionsValue": 100
                        if customer_id == "1111111111"
                        else 200000,
                    },
                }
            ]
        return []

    monkeypatch.setattr(reports, "load_json_config", fake_load_config)
    monkeypatch.setattr(reports, "search", fake_search)
    monkeypatch.setattr(reports, "approvals", lambda: {})

    raw = mcp.tools["generate_agency_dashboard"](days=30)
    payload = json.loads(raw)
    assert payload["mixed_currencies"] is True
    assert payload["managed_totals"] is None
    assert payload["currencies"] == ["ARS", "EUR"]
    assert set(payload["managed_totals_by_currency"].keys()) == {"ARS", "EUR"}
    assert "currency_warning" in payload
    assert all(alert["currency_code"] in {"ARS", "EUR"} for alert in payload["alerts"])


# ---------- Dockerfile packaging ----------


def test_dockerfile_copies_all_declared_py_modules():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    py_modules = pyproject["tool"]["setuptools"]["py-modules"]
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    for module in py_modules:
        assert f"{module}.py" in dockerfile, (
            f"Dockerfile must COPY {module}.py declared in pyproject.toml"
        )
