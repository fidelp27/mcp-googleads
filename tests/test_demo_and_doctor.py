"""End-to-end tests for --demo mode, doctor preflight, and multi-client registration."""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import google_ads_connect as connect


PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ---------- --demo mode E2E over MCP stdio ----------


@pytest.mark.asyncio
async def test_demo_mode_runs_end_to_end_without_credentials(tmp_path):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[str(PROJECT_ROOT / "google_ads_server.py"), "--transport", "stdio", "--demo"],
        cwd=str(tmp_path),
        env={
            "READ_ONLY_MODE": "false",  # must be overridden by --demo
            "GOOGLE_ADS_DATA_DIR": str(tmp_path / "demo-data"),
        },
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()

            status_res = await session.call_tool("server_status")
            assert status_res.isError is False
            status = status_res.structuredContent["result"]
            assert status["ready"] is True
            assert status["demo_mode"] is True
            assert status["agency"]["read_only_mode"] is True
            assert status["package"] == "google-ads-mcp-server"

            accounts = await session.call_tool("list_accounts")
            assert accounts.isError is False
            accounts_text = accounts.content[0].text
            assert "1111111111" in accounts_text and "EUR" in accounts_text
            assert "2222222222" in accounts_text and "USD" in accounts_text

            dashboard_res = await session.call_tool("generate_agency_dashboard", {"days": 30})
            assert dashboard_res.isError is False
            dashboard = json.loads(dashboard_res.content[0].text)
            assert dashboard["mixed_currencies"] is True
            assert dashboard["managed_totals"] is None
            assert dashboard["currencies"] == ["EUR", "USD"]
            assert set(dashboard["managed_totals_by_currency"].keys()) == {"EUR", "USD"}

            waste_res = await session.call_tool("detect_waste", {"customer_id": "1111111111", "days": 30})
            assert waste_res.isError is False
            waste = json.loads(waste_res.content[0].text)
            assert len(waste["wasted_search_terms"]) >= 2

            alerts_res = await session.call_tool("check_alerts")
            assert alerts_res.isError is False
            alerts_data = json.loads(alerts_res.content[0].text)
            assert "alerts" in alerts_data

            # Write tools must not even be exposed in tools/list in read-only --demo mode
            listed = {tool.name for tool in (await session.list_tools()).tools}
            assert len(listed) <= 25
            assert "pause_campaign" not in listed
            assert "list_analysis_profiles" in listed
            assert "get_analysis_profile" in listed

            profiles_res = await session.call_tool("list_analysis_profiles")
            assert profiles_res.isError is False
            profiles_data = json.loads(profiles_res.content[0].text)
            assert profiles_data["count"] >= 2

            blocked = await session.call_tool(
                "pause_campaign",
                {"customer_id": "1111111111", "campaign_id": "101", "dry_run": True},
            )
            assert blocked.isError is True


# ---------- doctor preflight ----------


def test_diagnose_google_ads_error_classifies_common_failures():
    code, hint = connect.diagnose_google_ads_error(403, ["CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"])
    assert code == "ACCESS_LEVEL_INSUFFICIENT"
    assert "access-levels" in hint

    code, hint = connect.diagnose_google_ads_error(403, ["DEVELOPER_TOKEN_PROHIBITED"])
    assert code == "DEVELOPER_TOKEN_PROHIBITED"
    assert "dedicated Cloud project" in hint

    code, hint = connect.diagnose_google_ads_error(403, ["USER_PERMISSION_DENIED"])
    assert code == "USER_PERMISSION_DENIED"
    assert "--manager" in hint

    code, _ = connect.diagnose_google_ads_error(401, [])
    assert code == "INVALID_CREDENTIALS"

    code, _ = connect.diagnose_google_ads_error(404, ["CUSTOMER_NOT_FOUND"])
    assert code == "ACCOUNT_NOT_FOUND"


def test_doctor_reports_missing_credentials_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("GOOGLE_ADS_CREDENTIALS_PATH", raising=False)
    monkeypatch.delenv("GOOGLE_ADS_TOKEN_PATH", raising=False)
    monkeypatch.delenv("ALLOWED_CUSTOMER_IDS", raising=False)
    report = connect.run_doctor(tmp_path / "missing-profile.json", offline=True)
    assert report["ready"] is False
    cred_checks = [c for c in report["checks"] if c["check"] == "credentials"]
    assert cred_checks[0]["code"] == "MISSING_CREDENTIALS"
    assert "--demo" in cred_checks[0]["next_step"]


def test_doctor_detects_token_and_credentials_path_collision(tmp_path, monkeypatch):
    same_file = tmp_path / "client_secret.json"
    same_file.write_text('{"installed":{"client_id":"a","client_secret":"b"}}', encoding="utf-8")
    monkeypatch.setenv("GOOGLE_ADS_AUTH_TYPE", "oauth")
    monkeypatch.setenv("GOOGLE_ADS_CREDENTIALS_PATH", str(same_file))
    monkeypatch.setenv("GOOGLE_ADS_TOKEN_PATH", str(same_file))
    report = connect.run_doctor(tmp_path / "no-profile.json", offline=True)
    assert report["ready"] is False
    cred_checks = [c for c in report["checks"] if c["check"] == "credentials"]
    assert cred_checks[0]["code"] == "INVALID_CREDENTIALS"
    assert "separate files" in cred_checks[0]["next_step"].lower()


def test_doctor_distinguishes_accessible_test_account_with_no_data(tmp_path, monkeypatch):
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "customer": "1234567890",
                "customers": ["1234567890"],
                "manager": "",
                "credentials": {
                    "token": "live-token",
                    "refresh_token": "live-refresh",
                    "client_id": "demo",
                    "client_secret": "demo",
                },
            }
        ),
        encoding="utf-8",
    )

    class _FakeCreds:
        valid = True
        token = "live-token"
        refresh_token = "live-refresh"

        @classmethod
        def from_authorized_user_info(cls, info, scopes):
            return cls()

    monkeypatch.setattr(connect, "Credentials", _FakeCreds)

    def fake_ads_request(method, endpoint, headers, **kwargs):
        if method == "GET":
            return {"resourceNames": ["customers/1234567890"]}
        query = (kwargs.get("json") or {}).get("query", "")
        if "customer.test_account" in query:
            return {
                "results": [
                    {
                        "customer": {
                            "id": "1234567890",
                            "descriptiveName": "Test Account",
                            "currencyCode": "USD",
                            "timeZone": "America/New_York",
                            "manager": False,
                            "testAccount": True,
                        }
                    }
                ]
            }
        return {"results": []}

    monkeypatch.setattr(connect, "ads_request", fake_ads_request)
    report = connect.run_doctor(profile, offline=False)
    assert report["ready"] is True
    data_check = next(c for c in report["checks"] if c["check"] == "account_1234567890_data")
    assert data_check["status"] == "warn"
    assert data_check["code"] == "NO_DATA"
    assert "test accounts" in data_check["detail"]


# ---------- connect: new clients & multi-account MCC ----------


def test_register_antigravity_and_gemini_cli_and_generic(tmp_path, monkeypatch, capsys):
    antigravity_dir = tmp_path / "antigravity"
    gemini_dir = tmp_path / "gemini"
    monkeypatch.setenv("ANTIGRAVITY_CONFIG_DIR", str(antigravity_dir))
    monkeypatch.setenv("GEMINI_HOME", str(gemini_dir))

    profile = tmp_path / "profile.json"
    connect.register_client("antigravity", None, demo=True)
    connect.register_client("antigravity", None, demo=True)  # idempotent
    ag_cfg = json.loads((antigravity_dir / "mcp_config.json").read_text(encoding="utf-8"))
    assert "--demo" in ag_cfg["mcpServers"][connect.NAME]["args"]

    connect.register_client("gemini-cli", profile)
    gm_cfg = json.loads((gemini_dir / "settings.json").read_text(encoding="utf-8"))
    assert str(profile) in gm_cfg["mcpServers"][connect.NAME]["args"]

    connect.register_client("generic", None, demo=True)
    out = json.loads(capsys.readouterr().out)
    assert connect.NAME in out["mcpServers"]
    assert "--demo" in out["mcpServers"][connect.NAME]["args"]


def test_connect_all_mcc_clients_discovers_and_saves_multiple_accounts(tmp_path, monkeypatch):
    client_json = tmp_path / "oauth.json"
    client_json.write_text('{"installed":{"client_id":"demo","client_secret":"demo"}}', encoding="utf-8")
    profile = tmp_path / "profile.json"

    class _DemoCreds:
        token = "access"
        refresh_token = "refresh"

        def to_json(self):
            return json.dumps({"refresh_token": self.refresh_token})

    monkeypatch.setattr(connect.InstalledAppFlow, "run_local_server", lambda *a, **kw: _DemoCreds())

    def fake_ads_request(method, endpoint, headers, **kwargs):
        if method == "GET":
            return {"resourceNames": ["customers/9999999999"]}
        query = (kwargs.get("json") or {}).get("query", "")
        if "FROM customer_client" in query:
            return {
                "results": [
                    {"customerClient": {"id": "1111111111", "descriptiveName": "Client A", "currencyCode": "EUR"}},
                    {"customerClient": {"id": "2222222222", "descriptiveName": "Client B", "currencyCode": "USD"}},
                ]
            }
        return {"results": [{"customer": {"id": endpoint.split("/")[1], "manager": False}}]}

    monkeypatch.setattr(connect, "ads_request", fake_ads_request)
    args = Namespace(
        oauth_client=str(client_json),
        customer=None,
        manager="999-999-9999",
        all_mcc_clients=True,
        no_browser=True,
    )
    connect.authorize(args, profile)
    saved = json.loads(profile.read_text(encoding="utf-8"))
    assert saved["manager"] == "9999999999"
    assert saved["customer"] == "1111111111"
    assert saved["customers"] == ["1111111111", "2222222222"]
