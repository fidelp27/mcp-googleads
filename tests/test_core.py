import json
from datetime import datetime, timedelta

import pytest

import google_ads_server as server


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234567890", "1234567890"),
        ("123-456-7890", "1234567890"),
        ("12345", "0000012345"),
        (12345, "0000012345"),
    ],
)
def test_format_customer_id(raw, expected):
    assert server.format_customer_id(raw) == expected


@pytest.mark.parametrize("raw", ["", "not-an-id", "12345678901"])
def test_format_customer_id_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        server.format_customer_id(raw)


def test_date_range_clause_accepts_arbitrary_days():
    clause = server.date_range_clause(31)
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=30)
    assert clause == (
        f"segments.date BETWEEN '{start_date.isoformat()}' "
        f"AND '{end_date.isoformat()}'"
    )


@pytest.mark.parametrize("days", [0, 366])
def test_date_range_clause_rejects_out_of_range(days):
    with pytest.raises(ValueError):
        server.date_range_clause(days)


def test_server_status_never_exposes_secrets(tmp_path, monkeypatch):
    token_path = tmp_path / "private-token.json"
    token_path.write_text('{"token": "private-value"}', encoding="utf-8")
    monkeypatch.setattr(server, "GOOGLE_ADS_CREDENTIALS_PATH", str(token_path))
    monkeypatch.setattr(server, "GOOGLE_ADS_DEVELOPER_TOKEN", "secret")
    monkeypatch.setattr(server, "GOOGLE_ADS_AUTH_TYPE", "oauth")
    monkeypatch.setattr(server, "GOOGLE_ADS_TOKEN_PATH", None)
    result = server.server_status()
    assert result["ready"] is True
    assert str(token_path) not in str(result)
    assert "private-value" not in str(result)
    assert "secret" not in str(result)


def test_server_status_reports_missing_configuration(monkeypatch):
    monkeypatch.setattr(server, "GOOGLE_ADS_CREDENTIALS_PATH", "")
    monkeypatch.setattr(server, "GOOGLE_ADS_DEVELOPER_TOKEN", "")
    result = server.server_status()
    assert result["ready"] is False
    assert any("GOOGLE_ADS_CREDENTIALS_PATH" in issue for issue in result["issues"])
    assert all("GOOGLE_ADS_DEVELOPER_TOKEN" not in issue for issue in result["issues"])


@pytest.mark.asyncio
async def test_list_resources_uses_google_ads_field_service(monkeypatch):
    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "results": [
                    {"name": "campaign", "category": "RESOURCE", "dataType": "MESSAGE"}
                ]
            }

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["query"] = kwargs["json"]["query"]
        return FakeResponse()

    monkeypatch.setattr(server, "get_credentials", lambda: object())
    monkeypatch.setattr(server, "get_headers", lambda credentials: {})
    monkeypatch.setattr(server, "http_post", fake_post)

    result = await server.list_resources()

    assert captured["url"].endswith("/v25/googleAdsFields:search")
    assert "FROM google_ads_field" not in captured["query"]
    assert "- campaign (MESSAGE)" in result


def test_oauth_client_config_is_not_overwritten(tmp_path, monkeypatch):
    client_path = tmp_path / "client_secret.json"
    token_path = tmp_path / "google_ads_token.json"
    original_config = {
        "installed": {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    client_path.write_text(json.dumps(original_config), encoding="utf-8")

    class FakeCredentials:
        valid = True

        @staticmethod
        def to_json():
            return '{"token": "saved-token"}'

    class FakeFlow:
        @staticmethod
        def run_local_server(port):
            assert port == 0
            return FakeCredentials()

    monkeypatch.setattr(server, "GOOGLE_ADS_CREDENTIALS_PATH", str(client_path))
    monkeypatch.setattr(server, "GOOGLE_ADS_TOKEN_PATH", str(token_path))
    monkeypatch.setattr(
        server.InstalledAppFlow,
        "from_client_config",
        lambda config, scopes: FakeFlow(),
    )

    result = server.get_oauth_credentials()

    assert isinstance(result, FakeCredentials)
    assert json.loads(client_path.read_text(encoding="utf-8")) == original_config
    assert json.loads(token_path.read_text(encoding="utf-8"))["token"] == "saved-token"
