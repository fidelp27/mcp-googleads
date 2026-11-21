import json
import sys
from argparse import Namespace

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import google_ads_connect as connect


def test_connect_validates_mcc_access_before_replacing_profile(tmp_path, monkeypatch):
    client = tmp_path / "oauth.json"
    client.write_text('{"installed":{"client_id":"demo","client_secret":"demo"}}', encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text("previous", encoding="utf-8")
    calls = []

    class DemoCredentials:
        token = "demo-access"
        refresh_token = "demo-refresh"

        def to_json(self):
            return json.dumps({"refresh_token": self.refresh_token})

    monkeypatch.setattr(connect.InstalledAppFlow, "run_local_server", lambda *a, **kw: DemoCredentials())

    def fake_request(method, endpoint, headers, **kwargs):
        calls.append((method, endpoint, headers.copy()))
        if method == "GET":
            return {"resourceNames": ["customers/1234567890"]}
        raise ValueError("Google Ads returned HTTP 403 (USER_PERMISSION_DENIED).")

    monkeypatch.setattr(connect, "ads_request", fake_request)
    args = Namespace(oauth_client=str(client), customer="987-654-3210", manager="123-456-7890", no_browser=True)
    with pytest.raises(ValueError, match="USER_PERMISSION_DENIED"):
        connect.authorize(args, profile)
    assert calls[1][1] == "customers/9876543210/googleAds:search"
    assert "login-customer-id" not in calls[0][2]
    assert calls[1][2]["login-customer-id"] == "1234567890"
    assert profile.read_text(encoding="utf-8") == "previous"


@pytest.mark.asyncio
async def test_profile_launches_from_another_directory_in_read_only_mode(tmp_path):
    profile = tmp_path / "local profile.json"
    profile.write_text(json.dumps({
        "customer": "1234567890", "manager": "",
        "credentials": {"client_id": "demo", "client_secret": "demo", "refresh_token": "demo"},
    }), encoding="utf-8")
    command, *args = connect.server_command(profile)
    parameters = StdioServerParameters(
        command=command, args=args, cwd=str(tmp_path),
        env={"READ_ONLY_MODE": "false", "ALLOWED_CUSTOMER_IDS": "9876543210"},
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            result = (await session.call_tool("server_status")).structuredContent["result"]
            assert result["ready"] is True
            assert result["agency"]["read_only_mode"] is True
            assert result["developer_token_configured"] is False
            accounts = await session.call_tool("list_accounts")
            assert "1234567890" in accounts.content[0].text
            assert "9876543210" not in accounts.content[0].text
            outside = await session.call_tool("run_gaql", {
                "customer_id": "9876543210", "query": "SELECT customer.id FROM customer",
            })
            assert outside.isError and "ALLOWED_CUSTOMER_IDS" in outside.content[0].text
    assert (tmp_path / "local profile-data" / "audit.jsonl").is_file()


def test_codex_registration_preserves_existing_servers(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config = tmp_path / "config.toml"
    config.write_text('[mcp_servers.other]\ncommand = "existing"\n', encoding="utf-8")
    profile = tmp_path / "local profile.json"
    connect.register_client("codex", profile)
    contents = config.read_bytes()
    connect.register_client("codex", profile)
    assert config.read_bytes() == contents
    settings = connect.tomllib.loads(contents.decode("utf-8"))["mcp_servers"]
    assert settings["other"]["command"] == "existing"
    assert settings[connect.NAME]["command"] == sys.executable
    assert str(profile) in settings[connect.NAME]["args"]
    with pytest.raises(ValueError, match="already configured"):
        connect.register_client("codex", tmp_path / "another.json")
