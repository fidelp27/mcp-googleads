"""Regression tests for post-v2 audit fixes (packaging, CLI presets, v25 GAQL, HTTP security, truncation, optimistic locking)."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import google_ads_connect as connect
from agency import (
    channels_tools as ct,
    diagnostics_tools as dt,
    measurement_tools as mt,
    reports,
    write_tools as wt,
)
from agency.errors import GoogleAdsError
from agency.modules import OPERATIONS_TOOLS


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _FakeMCP:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, **kwargs):
        def dec(fn):
            self.tools[fn.__name__] = fn
            return fn

        return dec


# ---------- 1. Wheel packaging outside repo checkout ----------


def test_wheel_packages_builtin_profiles_outside_repo_checkout(tmp_path):
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-build-isolation",
            "--no-deps",
            "-w",
            str(wheel_dir),
            str(PROJECT_ROOT),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Wheel build failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    wheels = list(wheel_dir.glob("google_ads_mcp_server-*.whl"))
    assert len(wheels) == 1, f"Expected 1 wheel, found {wheels}"

    wheel_path = wheels[0]
    with zipfile.ZipFile(wheel_path) as zf:
        names = set(zf.namelist())
        assert "google_ads_cli.py" in names
        for slug in ("leads-audit", "ecommerce-audit", "agency-weekly"):
            assert f"agency/builtin_profiles/{slug}.toml" in names
            assert f"agency/builtin_profiles/{slug}.md" in names
        site_dir = tmp_path / "isolated_site"
        zf.extractall(site_dir)

    # Run Python completely outside PROJECT_ROOT, stripping repo root & editable finders
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "GOOGLE_ADS_CONFIG_DIR": str(tmp_path / "empty_config"),
    }
    isolate_prefix = (
        f"import json, sys; from pathlib import Path; "
        f"repo = Path({str(PROJECT_ROOT)!r}).resolve(); "
        f"sys.meta_path[:] = [f for f in sys.meta_path if 'editable' not in type(f).__name__.lower() and 'editable' not in getattr(f, '__module__', '').lower()]; "
        f"sys.path[:] = [{str(site_dir)!r}] + [p for p in sys.path if p and Path(p).resolve() != repo]; "
    )
    probe = (
        isolate_prefix
        + "import agency.analysis_profiles as ap; "
        + "items = ap.list_profiles(); "
        + "leads = ap.load_profile('leads-audit'); "
        + "print(json.dumps({'mod_file': ap.__file__, 'ids': [p['id'] for p in items], 'sources': {p['id']: p['source'] for p in items}, 'has_guide': bool(leads['instructions_markdown'])}))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"Isolated probe failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    data = json.loads(proc.stdout)
    assert str(site_dir).lower() in data["mod_file"].lower()
    assert {"leads-audit", "ecommerce-audit", "agency-weekly"} <= set(data["ids"])
    assert data["sources"]["leads-audit"] == "builtin"
    assert data["has_guide"] is True

    # Verify user override in GOOGLE_ADS_CONFIG_DIR/profiles overrides built-in
    user_profiles = tmp_path / "empty_config" / "profiles"
    user_profiles.mkdir(parents=True)
    (user_profiles / "leads-audit.toml").write_text(
        'name = "Custom Leads Override"\ndescription = "User override"\n',
        encoding="utf-8",
    )
    probe_override = (
        isolate_prefix
        + "from agency.analysis_profiles import load_profile; "
        + "print(json.dumps(load_profile('leads-audit')))"
    )
    proc_override = subprocess.run(
        [sys.executable, "-c", probe_override],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc_override.returncode == 0, f"Override probe failed:\nSTDERR: {proc_override.stderr}"
    override_data = json.loads(proc_override.stdout)
    assert override_data["name"] == "Custom Leads Override"
    assert override_data["source"] == "user"


# ---------- 2. connect / register / serve presets & E2E stdio ----------


def test_server_and_demo_commands_preserve_preset_and_modules(tmp_path):
    profile = tmp_path / "profile.json"
    default_cmd = connect.server_command(profile)
    assert default_cmd[-1] == "serve"
    assert "--preset" not in default_cmd

    preset_cmd = connect.server_command(profile, preset="ecommerce", modules="display")
    assert "--preset" in preset_cmd
    assert preset_cmd[preset_cmd.index("--preset") + 1] == "ecommerce"
    assert "--modules" in preset_cmd
    assert preset_cmd[preset_cmd.index("--modules") + 1] == "display"

    demo_cmd = connect.demo_server_command(preset="leads")
    assert "--demo" in demo_cmd
    assert "--preset" in demo_cmd
    assert demo_cmd[demo_cmd.index("--preset") + 1] == "leads"


def test_register_generic_demo_with_preset_emits_preset_args(capsys):
    connect.main(["register", "--client", "generic", "--demo", "--preset", "agency"])
    payload = json.loads(capsys.readouterr().out)
    args = payload["mcpServers"][connect.NAME]["args"]
    assert "--demo" in args
    assert "--preset" in args
    assert args[args.index("--preset") + 1] == "agency"


def test_managed_serve_with_preset_exposes_domain_tools_and_blocks_writes(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "customer": "1111111111",
                "customers": ["1111111111"],
                "manager": "",
                "preset": "ecommerce",
                "credentials": {
                    "token": "fake",
                    "refresh_token": "fake-refresh",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_id": "fake-client",
                    "client_secret": "fake-secret",
                    "scopes": [connect.SCOPE],
                },
            }
        ),
        encoding="utf-8",
    )

    # Launch the managed `serve` command with `--preset full` to confirm:
    # 1. Domain modules load through `connect.py ... serve --preset ...`
    # 2. Managed read-only mode STILL strips 100% of `operations` write tools
    cmd = connect.server_command(profile, preset="full")

    async def list_tool_names() -> set[str]:
        params = StdioServerParameters(command=cmd[0], args=cmd[1:])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                return {t.name for t in tools.tools}

    names = asyncio.run(list_tool_names())
    assert "get_pmax_diagnostics" in names
    assert "get_shopping_performance" in names
    assert "audit_conversion_tracking" in names
    assert "diagnose_delivery" in names
    assert not (names & OPERATIONS_TOOLS), f"Write tools leaked in managed serve: {names & OPERATIONS_TOOLS}"


# ---------- 3. HTTP non-loopback security guard ----------


def test_streamable_http_refuses_non_loopback_without_opt_in():
    proc = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "google_ads_server.py"),
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--demo",
        ],
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "MCP_HTTP_ALLOW_REMOTE"},
    )
    assert proc.returncode != 0
    assert "MCP_HTTP_ALLOW_REMOTE=true" in proc.stderr


def test_dockerfile_defaults_to_loopback_and_dockerignore_keeps_required_files():
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "MCP_HOST=127.0.0.1" in dockerfile

    raw_patterns = [
        line.strip()
        for line in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    def is_ignored(rel_path: str) -> bool:
        parts = rel_path.split("/")
        for pat in raw_patterns:
            clean_pat = pat.rstrip("/")
            if fnmatch.fnmatch(rel_path, clean_pat) or fnmatch.fnmatch(parts[-1], clean_pat):
                return True
            if any(fnmatch.fnmatch(part, clean_pat) for part in parts):
                return True
        return False

    # Required files copied by Dockerfile MUST NOT be ignored
    for required in (
        "pyproject.toml",
        "google_ads_cli.py",
        "google_ads_server.py",
        "google_ads_connect.py",
        "agency/builtin_profiles/leads-audit.toml",
        "config/clients.example.json",
        "config/branding.example.json",
        "config/profiles/leads-audit.toml",
        "docs/connect.md",
    ):
        assert not is_ignored(required), f".dockerignore wrongly excludes {required}"

    # Secrets & local-only dirs MUST be ignored
    for excluded in (
        ".env",
        "client_secret.json",
        "google_ads_token.json",
        "tmp/roadmap-v2-auditoria/doc.docx",
        "tests/test_audit_fixes.py",
        "analisis-cliente-ejemplo.md",
    ):
        assert is_ignored(excluded), f".dockerignore must exclude {excluded}"


def test_powershell_launchers_fallback_to_path_when_no_venv():
    for script_name in ("run_stdio.ps1", "run_http.ps1"):
        content = (PROJECT_ROOT / "scripts" / script_name).read_text(encoding="utf-8")
        assert "Get-Command google-ads-mcp-server" in content


# ---------- 4. Real Google Ads API v25 GAQL contracts ----------


def test_audit_conversion_tracking_uses_v25_conversion_goal_campaign_config(monkeypatch):
    queries: list[str] = []

    def fake_search(cid: str, query: str):
        queries.append(query)
        q = query.upper()
        if "FROM CUSTOMER\n" in q or "FROM CUSTOMER " in q:
            return [
                {
                    "customer": {
                        "id": cid,
                        "descriptiveName": "V25 Test",
                        "currencyCode": "USD",
                        "timeZone": "UTC",
                        "conversionTrackingSetting": {
                            "conversionTrackingId": "777",
                            "conversionTrackingStatus": "CONVERSION_TRACKING_ENABLED",
                            "googleAdsConversionCustomer": f"customers/{cid}",
                            "acceptedCustomerDataTerms": True,
                            "enhancedConversionsForLeadsEnabled": True,
                        },
                    }
                }
            ]
        if "FROM CONVERSION_ACTION" in q:
            return [
                {
                    "conversionAction": {
                        "resourceName": f"customers/{cid}/conversionActions/1",
                        "id": "1",
                        "name": "Lead Primary",
                        "status": "ENABLED",
                        "category": "LEAD",
                        "origin": "WEBSITE",
                        "type": "WEBPAGE",
                        "primaryForGoal": True,
                    }
                },
                {
                    "conversionAction": {
                        "resourceName": f"customers/{cid}/conversionActions/2",
                        "id": "2",
                        "name": "SQL Custom",
                        "status": "ENABLED",
                        "category": "QUALIFIED_LEAD",
                        "origin": "IMPORT",
                        "type": "UPLOAD_CLICKS",
                        "primaryForGoal": False,
                    }
                },
            ]
        if "FROM CUSTOMER_CONVERSION_GOAL" in q:
            return [{"customerConversionGoal": {"category": "LEAD", "origin": "WEBSITE", "biddable": True}}]
        if "FROM CUSTOM_CONVERSION_GOAL" in q:
            return [
                {
                    "customConversionGoal": {
                        "resourceName": f"customers/{cid}/customConversionGoals/90",
                        "id": "90",
                        "name": "SQL Pipeline",
                        "status": "ENABLED",
                        "conversionActions": [f"customers/{cid}/conversionActions/2"],
                    }
                }
            ]
        if "FROM CONVERSION_GOAL_CAMPAIGN_CONFIG" in q:
            return [
                {
                    "campaign": {"id": "10"},
                    "conversionGoalCampaignConfig": {
                        "goalConfigLevel": "CAMPAIGN",
                        "customConversionGoal": f"customers/{cid}/customConversionGoals/90",
                    },
                },
                {
                    "campaign": {"id": "11"},
                    "conversionGoalCampaignConfig": {
                        "goalConfigLevel": "CUSTOMER",
                        "customConversionGoal": f"customers/{cid}/customConversionGoals/90",
                    },
                },
            ]
        if "FROM CAMPAIGN" in q and "SEGMENTS.CONVERSION_ACTION" not in q:
            return [
                {
                    "campaign": {
                        "id": "10",
                        "name": "Custom Goal Camp",
                        "status": "ENABLED",
                        "advertisingChannelType": "SEARCH",
                        "biddingStrategyType": "TARGET_CPA",
                    },
                    "metrics": {"costMicros": "10000000", "conversions": 5.0, "allConversions": 5.0},
                },
                {
                    "campaign": {
                        "id": "11",
                        "name": "Account Default Camp",
                        "status": "ENABLED",
                        "advertisingChannelType": "SEARCH",
                        "biddingStrategyType": "TARGET_CPA",
                    },
                    "metrics": {"costMicros": "10000000", "conversions": 3.0, "allConversions": 3.0},
                },
            ]
        return []

    monkeypatch.setattr(mt, "search", fake_search)
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1111111111")
    mcp = _FakeMCP()
    mt.register_measurement_tools(mcp)

    out = json.loads(mcp.tools["audit_conversion_tracking"](customer_id="1111111111", days=30))
    assert any("FROM conversion_goal_campaign_config" in q for q in queries)
    assert not any("campaign_custom_conversion_goal" in q for q in queries)
    assert out["coverage_complete"] is True
    by_id = {c["campaign_id"]: c for c in out["campaigns"]}
    assert by_id["10"]["goal_mode"] == "custom_goal"
    assert by_id["10"]["custom_goals"] == ["SQL Pipeline"]
    # goalConfigLevel == CUSTOMER must force account_default even if customConversionGoal was populated
    assert by_id["11"]["goal_mode"] == "account_default"

    # Also verify coverage_complete flips to False if an optional query fails
    def failing_optional(cid: str, query: str):
        if "conversion_goal_campaign_config" in query:
            raise GoogleAdsError(
                message="simulated error",
                http_status=400,
                status="INVALID_ARGUMENT",
                api_version="v25",
            )
        return fake_search(cid, query)

    monkeypatch.setattr(mt, "search", failing_optional)
    out_partial = json.loads(mcp.tools["audit_conversion_tracking"](customer_id="1111111111", days=30))
    assert out_partial["coverage_complete"] is False
    assert out_partial["diagnostic_warnings"]


def test_pmax_diagnostics_v25_gaql_removes_performance_label_and_filters_insights_per_campaign(monkeypatch):
    queries: list[str] = []

    def fake_search(cid: str, query: str):
        queries.append(query)
        q = query.upper()
        if "FROM ASSET_GROUP\n" in q or "FROM ASSET_GROUP " in q:
            return [
                {
                    "campaign": {"id": "102", "name": "PMax A"},
                    "assetGroup": {"id": "4001", "name": "AG A", "status": "ENABLED", "adStrength": "GOOD"},
                    "metrics": {"impressions": "1000", "clicks": "50", "costMicros": "10000000", "conversions": 2.0, "conversionsValue": 200.0},
                },
                {
                    "campaign": {"id": "105", "name": "PMax B"},
                    "assetGroup": {"id": "4002", "name": "AG B", "status": "ENABLED", "adStrength": "GOOD"},
                    "metrics": {"impressions": "800", "clicks": "30", "costMicros": "5000000", "conversions": 1.0, "conversionsValue": 100.0},
                },
            ]
        if "FROM ASSET_GROUP_ASSET" in q:
            return [
                {
                    "assetGroup": {"id": "4001"},
                    "assetGroupAsset": {
                        "fieldType": "HEADLINE",
                        "status": "ENABLED",
                        "primaryStatus": "LIMITED",
                        "primaryStatusReasons": ["ASSET_LIMITED"],
                        "policySummary": {"approvalStatus": "APPROVED_LIMITED"},
                    },
                    "asset": {"id": "91", "name": "Headline 1", "type": "TEXT"},
                }
            ]
        if "FROM CAMPAIGN_SEARCH_TERM_INSIGHT" in q:
            match = re.search(r"campaign_search_term_insight\.campaign_id\s*=\s*(\d+)", query)
            assert match, f"campaign_search_term_insight MUST filter by single campaign_id: {query}"
            return [
                {
                    "campaignSearchTermInsight": {
                        "campaignId": match.group(1),
                        "categoryLabel": f"theme-{match.group(1)}",
                    },
                    "metrics": {"impressions": "500", "clicks": "25", "conversions": 1.0, "conversionsValue": 90.0},
                }
            ]
        return []

    monkeypatch.setattr(ct, "search", fake_search)
    monkeypatch.setattr(ct, "_account_meta", lambda cid: ("EUR", "Europe/Madrid", "Demo"))
    monkeypatch.setattr(
        ct,
        "_safe_search",
        lambda cid, q: (fake_search(cid, q), None),
    )
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1111111111")
    mcp = _FakeMCP()
    ct.register_channel_tools(mcp)

    out = json.loads(mcp.tools["get_pmax_diagnostics"](customer_id="1111111111", days=30))
    aga_queries = [q for q in queries if "FROM asset_group_asset" in q]
    assert len(aga_queries) == 1
    assert "performance_label" not in aga_queries[0]
    assert "asset_group_asset.primary_status" in aga_queries[0]

    insight_queries = [q for q in queries if "FROM campaign_search_term_insight" in q]
    assert len(insight_queries) == 2
    assert {t["campaign_id"] for t in out["search_term_insights"]} == {"102", "105"}
    assert any(a["code"] == "PMAX_LIMITED_OR_INELIGIBLE_ASSETS" for a in out["alerts"])
    assert out["coverage_complete"] is True


# ---------- 5. Timezone metadata & truncation propagation ----------


def test_detect_waste_and_optimization_plan_include_timezone_and_truncation_flags(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_DEMO_MODE", "true")
    monkeypatch.setenv("GOOGLE_ADS_MAX_SEARCH_ROWS", "1")
    mcp = _FakeMCP()
    reports.register_report_tools(mcp)
    ct.register_channel_tools(mcp)

    waste = json.loads(mcp.tools["detect_waste"](customer_id="1111111111", days=30))
    assert waste["currency_code"] == "EUR"
    assert waste["time_zone"] == "Europe/Madrid"
    assert waste["complete_days_only"] is True
    assert "queried_at_utc" in waste
    assert waste["truncated"] is True
    assert waste["totals_are_partial"] is True
    assert "truncation_warning" in waste

    plan = json.loads(mcp.tools["generate_optimization_plan"](customer_id="1111111111", days=30))
    assert plan["currency_code"] == "EUR"
    assert plan["time_zone"] == "Europe/Madrid"
    assert plan["complete_days_only"] is True
    assert "period" in plan

    rep = json.loads(mcp.tools["generate_client_report"](customer_id="1111111111", period="month", format="json"))
    assert rep["truncated"] is True
    assert rep["totals_are_partial"] is True
    assert "truncation_warning" in rep

    trends = json.loads(mcp.tools["get_performance_trends"](customer_id="1111111111", days=14, granularity="daily"))
    assert trends["truncated"] is True
    assert trends["totals_are_partial"] is True

    shop = json.loads(mcp.tools["get_shopping_performance"](customer_id="1111111111", group_by="product"))
    assert shop["truncated"] is True
    assert shop["totals_are_partial"] is True
    assert "truncation_warning" in shop


# ---------- 6. Optimistic locking in core write_tools.py ----------


def test_write_tools_budget_and_campaign_status_reject_stale_approvals(monkeypatch, tmp_path):
    monkeypatch.setenv("READ_ONLY_MODE", "false")
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1111111111")
    monkeypatch.setenv("GOOGLE_ADS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GOOGLE_ADS_CONFIG_DIR", str(tmp_path / "config"))

    live_budget = {"id": "501", "name": "Core Daily", "amountMicros": "50000000"}
    live_campaign = {"id": "101", "name": "Brand Search", "status": "ENABLED"}

    def fake_search(cid: str, query: str):
        if "FROM campaign_budget" in query:
            return [{"campaignBudget": dict(live_budget)}]
        if "FROM campaign" in query:
            return [{"campaign": dict(live_campaign), "campaignBudget": dict(live_budget)}]
        return []

    monkeypatch.setattr(wt, "search", fake_search)
    monkeypatch.setattr("agency.mutations.mutate", lambda *a, **kw: {"results": [{"resourceName": "ok"}]})

    mcp = _FakeMCP()
    wt.register_write_tools(mcp)

    # 1. update_campaign_budget: dry-run at 50 EUR, then live budget drifts to 55 EUR before execute
    preview = json.loads(
        mcp.tools["update_campaign_budget"](
            customer_id="1111111111",
            budget_id="501",
            new_amount_micros=60_000_000,
            dry_run=True,
        )
    )
    approval_id = preview["approval_id"]
    live_budget["amountMicros"] = "55000000"
    with pytest.raises(PermissionError, match="Stale approval"):
        mcp.tools["update_campaign_budget"](
            customer_id="1111111111",
            budget_id="501",
            new_amount_micros=60_000_000,
            dry_run=False,
            approval_id=approval_id,
        )

    # 2. pause_campaign: dry-run while ENABLED, then campaign status drifts to PAUSED before execute
    preview_camp = json.loads(
        mcp.tools["pause_campaign"](
            customer_id="1111111111",
            campaign_id="101",
            dry_run=True,
        )
    )
    live_campaign["status"] = "PAUSED"
    with pytest.raises(PermissionError, match="Stale approval"):
        mcp.tools["pause_campaign"](
            customer_id="1111111111",
            campaign_id="101",
            dry_run=False,
            approval_id=preview_camp["approval_id"],
        )


# ---------- 7. Roadmap v3 P0: direct `google-ads-mcp-server serve` & PMax sampling coverage ----------


@pytest.mark.parametrize(
    "entrypoint_script",
    [
        str(PROJECT_ROOT / "google_ads_cli.py"),
        str(PROJECT_ROOT / "google_ads_server.py"),
    ],
)
def test_direct_cli_and_server_script_serve_managed_profile_identically(tmp_path, entrypoint_script):
    profile = tmp_path / "managed_profile.json"
    profile.write_text(
        json.dumps(
            {
                "customer": "1111111111",
                "customers": ["1111111111", "2222222222"],
                "manager": "9999999999",
                "preset": "ecommerce",
                "credentials": {
                    "token": "fake",
                    "refresh_token": "fake-refresh-token",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_id": "fake-client",
                    "client_secret": "fake-secret",
                    "scopes": [connect.SCOPE],
                },
            }
        ),
        encoding="utf-8",
    )

    # Inject decoy .env-style variables in parent env to prove managed profile overrides them cleanly
    decoy_env = {
        **os.environ,
        "READ_ONLY_MODE": "false",
        "GOOGLE_ADS_CREDENTIALS_PATH": str(tmp_path / "does-not-exist.json"),
        "GOOGLE_ADS_DEVELOPER_TOKEN": "decoy-dev-token",
        "ALLOWED_CUSTOMER_IDS": "9990001112",
    }

    async def probe_session() -> tuple[set[str], dict]:
        params = StdioServerParameters(
            command=sys.executable,
            args=[entrypoint_script, "--profile", str(profile), "serve"],
            env=decoy_env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_resp = await session.list_tools()
                status_resp = await session.call_tool("server_status", {})
                status_text = status_resp.content[0].text
                return {t.name for t in tools_resp.tools}, json.loads(status_text)

    tool_names, status = asyncio.run(probe_session())
    assert "get_pmax_diagnostics" in tool_names
    assert "get_shopping_performance" in tool_names
    assert "audit_conversion_tracking" in tool_names
    assert not (tool_names & OPERATIONS_TOOLS), "Write tools must never be exposed in managed serve"

    # Verify MANAGED_PROFILE took effect (credentials_configured=True, read_only=True, scoped customers, preset active)
    assert status["ready"] is True
    assert status["credentials_configured"] is True
    assert status["developer_token_configured"] is False
    assert status["login_customer_configured"] is True
    assert status["agency"]["read_only_mode"] is True
    assert status["agency"]["allowed_customer_count"] == 3
    assert "pmax" in status["catalog"]["modules"]
    assert "shopping" in status["catalog"]["modules"]
    assert status["catalog"]["read_only_mode"] is True


def test_pmax_diagnostics_distinguishes_empty_sampled_and_error_coverage(monkeypatch):
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1111111111")
    monkeypatch.setattr(ct, "_account_meta", lambda cid: ("EUR", "Europe/Madrid", "Demo"))
    mcp = _FakeMCP()
    ct.register_channel_tools(mcp)

    # Case A: Empty PMax account (zero errors, zero data)
    monkeypatch.setattr(ct, "search", lambda cid, q: [])
    monkeypatch.setattr(ct, "_safe_search", lambda cid, q: ([], None))
    empty_out = json.loads(mcp.tools["get_pmax_diagnostics"](customer_id="1111111111", days=30))
    assert empty_out["coverage_status"] == "empty"
    assert empty_out["coverage_complete"] is True
    assert empty_out["search_term_insights_coverage"]["status"] == "empty"
    assert empty_out["search_term_insights_coverage"]["eligible_campaigns_count"] == 0
    assert empty_out["sampling_warnings"] == []

    # Case B: Sampled by campaign cap (12 eligible campaigns > limit 10)
    twelve_ag_rows = [
        {
            "campaign": {"id": str(100 + idx), "name": f"PMax {idx}"},
            "assetGroup": {"id": str(4000 + idx), "name": f"AG {idx}", "status": "ENABLED", "adStrength": "GOOD"},
            "metrics": {"impressions": "100", "clicks": "10", "costMicros": str((20 - idx) * 1_000_000), "conversions": 1.0, "conversionsValue": 50.0},
        }
        for idx in range(12)
    ]

    def fake_safe_sampled_campaigns(cid: str, query: str):
        if "FROM campaign_search_term_insight" in query:
            m = re.search(r"campaign_search_term_insight\.campaign_id\s*=\s*(\d+)", query)
            return (
                [
                    {
                        "campaignSearchTermInsight": {"campaignId": m.group(1), "categoryLabel": f"cat-{m.group(1)}"},
                        "metrics": {"impressions": "200", "clicks": "10", "conversions": 1.0, "conversionsValue": 40.0},
                    }
                ],
                None,
            )
        return [], None

    monkeypatch.setattr(ct, "search", lambda cid, q: twelve_ag_rows)
    monkeypatch.setattr(ct, "_safe_search", fake_safe_sampled_campaigns)
    sampled_camp_out = json.loads(mcp.tools["get_pmax_diagnostics"](customer_id="1111111111", days=30))
    assert sampled_camp_out["coverage_status"] == "sampled"
    assert sampled_camp_out["coverage_complete"] is False
    cov = sampled_camp_out["search_term_insights_coverage"]
    assert cov["eligible_campaigns_count"] == 12
    assert cov["queried_campaigns_count"] == 10
    assert cov["omitted_campaigns_count"] == 2
    assert cov["capped_by_campaign_limit"] is True
    assert cov["status"] == "sampled"
    assert sampled_camp_out["sampling_warnings"]

    # Case C: Sampled by per-campaign (25) and global (50) limits (3 campaigns x 25 = 75 -> trimmed to 50)
    three_ag_rows = twelve_ag_rows[:3]

    def fake_safe_heavy_insights(cid: str, query: str):
        if "FROM campaign_search_term_insight" in query:
            m = re.search(r"campaign_search_term_insight\.campaign_id\s*=\s*(\d+)", query)
            return (
                [
                    {
                        "campaignSearchTermInsight": {"campaignId": m.group(1), "categoryLabel": f"cat-{m.group(1)}-{i}"},
                        "metrics": {"impressions": str(1000 - i), "clicks": "10", "conversions": 1.0, "conversionsValue": 40.0},
                    }
                    for i in range(25)
                ],
                None,
            )
        return [], None

    monkeypatch.setattr(ct, "search", lambda cid, q: three_ag_rows)
    monkeypatch.setattr(ct, "_safe_search", fake_safe_heavy_insights)
    heavy_out = json.loads(mcp.tools["get_pmax_diagnostics"](customer_id="1111111111", days=30))
    assert heavy_out["coverage_status"] == "sampled"
    assert heavy_out["coverage_complete"] is False
    heavy_cov = heavy_out["search_term_insights_coverage"]
    assert heavy_cov["capped_by_per_campaign_limit"] is True
    assert len(heavy_cov["per_campaign_capped_ids"]) == 3
    assert heavy_cov["capped_by_global_limit"] is True
    assert heavy_cov["combined_rows_before_global_cap"] == 75
    assert heavy_cov["returned_rows"] == 50

    # Case D: Partial error on insight query
    def fake_safe_error(cid: str, query: str):
        if "FROM campaign_search_term_insight" in query:
            return [], "Optional diagnostic query unavailable (INVALID_ARGUMENT): simulated failure"
        return [], None

    monkeypatch.setattr(ct, "_safe_search", fake_safe_error)
    err_out = json.loads(mcp.tools["get_pmax_diagnostics"](customer_id="1111111111", days=30))
    assert err_out["coverage_status"] == "partial_error"
    assert err_out["coverage_complete"] is False
    assert err_out["search_term_insights_coverage"]["status"] == "partial_error"
    assert err_out["search_term_insights_coverage"]["failed_campaigns_count"] == 3


def test_remaining_aggregators_propagate_truncation_and_partial_totals(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_DEMO_MODE", "true")
    monkeypatch.setenv("GOOGLE_ADS_MAX_SEARCH_ROWS", "1")
    mcp = _FakeMCP()
    dt.register_diagnostics_tools(mcp)
    reports.register_report_tools(mcp)
    mt.register_measurement_tools(mcp)

    pacing = json.loads(mcp.tools["get_budget_pacing"](customer_id="1111111111", monthly_target_budget=5000.0))
    assert pacing["truncated"] is True
    assert pacing["totals_are_partial"] is True
    assert "truncation_warning" in pacing

    delivery = json.loads(mcp.tools["diagnose_delivery"](customer_id="1111111111", days=14))
    assert delivery["truncated"] is True
    assert delivery["coverage_complete"] is False
    assert "truncation_warning" in delivery

    dashboard = json.loads(mcp.tools["generate_agency_dashboard"](days=30))
    assert dashboard["truncated"] is True
    assert dashboard["totals_are_partial"] is True
    assert "1111111111" in dashboard["truncated_customer_ids"]
    assert "truncation_warning" in dashboard

    conv_audit = json.loads(mcp.tools["audit_conversion_tracking"](customer_id="1111111111", days=30))
    assert conv_audit["truncated"] is True
    assert conv_audit["totals_are_partial"] is True
    assert conv_audit["coverage_complete"] is False
    assert "truncation_warning" in conv_audit


def test_pmax_paginated_asset_group_truncation_marks_coverage_incomplete(monkeypatch):
    """Reproduces P0: paginated API row-cap truncation on asset_group / asset_group_asset MUST flip coverage_complete=False."""
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1111111111")
    monkeypatch.setenv("GOOGLE_ADS_MAX_SEARCH_ROWS", "2")
    monkeypatch.setenv("GOOGLE_ADS_DEMO_MODE", "false")
    monkeypatch.setattr(ct, "_account_meta", lambda cid: ("EUR", "Europe/Madrid", "Paginated Test"))

    def fake_api_request(method: str, endpoint: str, *, customer_id: str | None = None, body: dict | None = None, **kwargs):
        q = (body or {}).get("query", "")
        if "FROM asset_group\n" in q or "FROM asset_group " in q:
            return {
                "results": [
                    {
                        "campaign": {"id": "102", "name": "PMax 1"},
                        "assetGroup": {"id": "4001", "name": "AG 1", "status": "ENABLED", "adStrength": "GOOD"},
                        "metrics": {"impressions": "100", "clicks": "10", "costMicros": "1000000", "conversions": 1.0, "conversionsValue": 50.0},
                    },
                    {
                        "campaign": {"id": "102", "name": "PMax 1"},
                        "assetGroup": {"id": "4002", "name": "AG 2", "status": "ENABLED", "adStrength": "GOOD"},
                        "metrics": {"impressions": "80", "clicks": "8", "costMicros": "800000", "conversions": 1.0, "conversionsValue": 40.0},
                    },
                ],
                "nextPageToken": "page-2-truncated",
            }
        if "FROM asset_group_asset" in q:
            return {
                "results": [
                    {
                        "assetGroup": {"id": "4001"},
                        "assetGroupAsset": {
                            "fieldType": "HEADLINE",
                            "status": "ENABLED",
                            "primaryStatus": "ELIGIBLE",
                            "policySummary": {"approvalStatus": "APPROVED"},
                        },
                        "asset": {"id": "1", "name": "H1", "type": "TEXT"},
                    },
                    {
                        "assetGroup": {"id": "4001"},
                        "assetGroupAsset": {
                            "fieldType": "HEADLINE",
                            "status": "ENABLED",
                            "primaryStatus": "ELIGIBLE",
                            "policySummary": {"approvalStatus": "APPROVED"},
                        },
                        "asset": {"id": "2", "name": "H2", "type": "TEXT"},
                    },
                ],
                "nextPageToken": "page-2-assets",
            }
        if "FROM campaign_search_term_insight" in q:
            return {
                "results": [
                    {
                        "campaignSearchTermInsight": {"campaignId": "102", "categoryLabel": "single theme"},
                        "metrics": {"impressions": "50", "clicks": "5", "conversions": 1.0, "conversionsValue": 30.0},
                    }
                ]
            }
        return {"results": []}

    monkeypatch.setattr("agency.api.request", fake_api_request)
    mcp = _FakeMCP()
    ct.register_channel_tools(mcp)

    out = json.loads(mcp.tools["get_pmax_diagnostics"](customer_id="1111111111", days=30))
    assert out["truncated"] is True
    assert out["totals_are_partial"] is True
    assert "asset_group" in out["truncated_queries"]
    assert "asset_group_asset" in out["truncated_queries"]
    assert out["coverage_complete"] is False
    assert out["coverage_status"] == "sampled"
    assert out["search_term_insights_coverage"]["capped_by_api_row_limit"] is True
    assert out["sampling_warnings"]
    assert "truncation_warning" in out


def test_conversion_audit_config_query_truncation_marks_coverage_incomplete(monkeypatch):
    """Reproduces P1: paginated truncation on conversion_action config query MUST flip truncated=True and coverage_complete=False even when metric queries are NOT truncated."""
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1111111111")
    monkeypatch.setenv("GOOGLE_ADS_MAX_SEARCH_ROWS", "2")
    monkeypatch.setenv("GOOGLE_ADS_DEMO_MODE", "false")

    def fake_api_request(method: str, endpoint: str, *, customer_id: str | None = None, body: dict | None = None, **kwargs):
        q = (body or {}).get("query", "")
        if "FROM customer" in q:
            return {
                "results": [
                    {
                        "customer": {
                            "id": "1111111111",
                            "descriptiveName": "Config Truncation Test",
                            "currencyCode": "USD",
                            "timeZone": "UTC",
                            "conversionTrackingSetting": {
                                "conversionTrackingId": "777",
                                "conversionTrackingStatus": "CONVERSION_TRACKING_ENABLED",
                                "googleAdsConversionCustomer": "customers/1111111111",
                                "acceptedCustomerDataTerms": True,
                                "enhancedConversionsForLeadsEnabled": True,
                            },
                        }
                    }
                ]
            }
        if "FROM conversion_action" in q:
            return {
                "results": [
                    {
                        "conversionAction": {
                            "resourceName": "customers/1111111111/conversionActions/1",
                            "id": "1",
                            "name": "Lead 1",
                            "status": "ENABLED",
                            "category": "LEAD",
                            "origin": "WEBSITE",
                            "type": "WEBPAGE",
                            "primaryForGoal": True,
                        }
                    },
                    {
                        "conversionAction": {
                            "resourceName": "customers/1111111111/conversionActions/2",
                            "id": "2",
                            "name": "Lead 2",
                            "status": "ENABLED",
                            "category": "LEAD",
                            "origin": "WEBSITE",
                            "type": "UPLOAD_CLICKS",
                            "primaryForGoal": False,
                        }
                    },
                ],
                "nextPageToken": "page-2-conversion-actions",
            }
        if "FROM campaign" in q and "segments.conversion_action" not in q:
            # Metric query returns only 1 row (NOT truncated!)
            return {
                "results": [
                    {
                        "campaign": {
                            "id": "10",
                            "name": "Search Core",
                            "status": "ENABLED",
                            "advertisingChannelType": "SEARCH",
                            "biddingStrategyType": "TARGET_CPA",
                        },
                        "metrics": {"costMicros": "10000000", "conversions": 4.0, "allConversions": 4.0},
                    }
                ]
            }
        return {"results": []}

    monkeypatch.setattr("agency.api.request", fake_api_request)
    mcp = _FakeMCP()
    mt.register_measurement_tools(mcp)

    audit = json.loads(mcp.tools["audit_conversion_tracking"](customer_id="1111111111", days=30))
    assert audit["truncated"] is True
    assert audit["totals_are_partial"] is True
    assert audit["truncated_queries"] == ["conversion_action"]
    assert audit["coverage_complete"] is False
    assert "truncation_warning" in audit

    offline = json.loads(mcp.tools["get_offline_conversion_diagnostics"](customer_id="1111111111"))
    assert offline["truncated"] is True
    assert offline["truncated_queries"] == ["conversion_action"]
    assert offline["coverage_complete"] is False
    assert "truncation_warning" in offline


def test_truncated_conversion_actions_suppresses_smart_bidding_without_effective_actions(monkeypatch):
    """Absence-based SMART_BIDDING_WITHOUT_EFFECTIVE_ACTIONS MUST downgrade to insufficient_data when conversion_action is truncated."""
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1111111111")
    monkeypatch.setenv("GOOGLE_ADS_MAX_SEARCH_ROWS", "1")
    monkeypatch.setenv("GOOGLE_ADS_DEMO_MODE", "false")

    paginated = {"actions_has_next_page": True}

    def fake_api_request(method: str, endpoint: str, *, customer_id: str | None = None, body: dict | None = None, **kwargs):
        q = (body or {}).get("query", "")
        if "FROM customer" in q:
            return {
                "results": [
                    {
                        "customer": {
                            "id": "1111111111",
                            "descriptiveName": "Absence Guard Test",
                            "currencyCode": "USD",
                            "timeZone": "UTC",
                            "conversionTrackingSetting": {
                                "conversionTrackingId": "777",
                                "conversionTrackingStatus": "CONVERSION_TRACKING_ENABLED",
                                "googleAdsConversionCustomer": "customers/1111111111",
                                "acceptedCustomerDataTerms": True,
                                "enhancedConversionsForLeadsEnabled": True,
                            },
                        }
                    }
                ]
            }
        if "FROM conversion_action" in q:
            # Page 1 contains ONLY a secondary action; a valid primary action may be on page 2
            resp = {
                "results": [
                    {
                        "conversionAction": {
                            "resourceName": "customers/1111111111/conversionActions/2",
                            "id": "2",
                            "name": "Page View Secondary",
                            "status": "ENABLED",
                            "category": "PAGE_VIEW",
                            "origin": "WEBSITE",
                            "type": "WEBPAGE",
                            "primaryForGoal": False,
                        }
                    }
                ]
            }
            if paginated["actions_has_next_page"]:
                resp["nextPageToken"] = "page-2-has-primary"
            return resp
        if "FROM campaign" in q and "segments.conversion_action" not in q:
            return {
                "results": [
                    {
                        "campaign": {
                            "id": "10",
                            "name": "Smart Bidding Search",
                            "status": "ENABLED",
                            "advertisingChannelType": "SEARCH",
                            "biddingStrategyType": "TARGET_CPA",
                        },
                        "metrics": {"costMicros": "10000000", "conversions": 5.0, "allConversions": 5.0},
                    }
                ]
            }
        return {"results": []}

    monkeypatch.setattr("agency.api.request", fake_api_request)
    mcp = _FakeMCP()
    mt.register_measurement_tools(mcp)

    # 1. Truncated: MUST NOT emit critical SMART_BIDDING_WITHOUT_EFFECTIVE_ACTIONS; MUST record insufficient_data
    truncated_audit = json.loads(mcp.tools["audit_conversion_tracking"](customer_id="1111111111", days=30))
    assert truncated_audit["coverage_complete"] is False
    alert_codes = {a["code"] for a in truncated_audit["alerts"]}
    assert "SMART_BIDDING_WITHOUT_EFFECTIVE_ACTIONS" not in alert_codes
    insuff_codes = {i["code"] for i in truncated_audit["insufficient_data"]}
    assert "INSUFFICIENT_DATA_EFFECTIVE_ACTIONS" in insuff_codes

    # 2. Control (complete, non-truncated): MUST emit critical SMART_BIDDING_WITHOUT_EFFECTIVE_ACTIONS
    paginated["actions_has_next_page"] = False
    complete_audit = json.loads(mcp.tools["audit_conversion_tracking"](customer_id="1111111111", days=30))
    assert complete_audit["coverage_complete"] is True
    assert "SMART_BIDDING_WITHOUT_EFFECTIVE_ACTIONS" in {a["code"] for a in complete_audit["alerts"]}
    assert complete_audit["insufficient_data"] == []


def test_truncated_billing_setup_suppresses_no_approved_billing_confirmed_cause(monkeypatch):
    """Absence-based NO_APPROVED_BILLING_SETUP MUST downgrade to insufficient_data when billing_setup is truncated."""
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1111111111")
    monkeypatch.setenv("GOOGLE_ADS_MAX_SEARCH_ROWS", "1")
    monkeypatch.setenv("GOOGLE_ADS_DEMO_MODE", "false")

    paginated = {"billing_has_next_page": True}

    def fake_api_request(method: str, endpoint: str, *, customer_id: str | None = None, body: dict | None = None, **kwargs):
        q = (body or {}).get("query", "")
        if "FROM customer" in q:
            return {
                "results": [
                    {
                        "customer": {
                            "id": "1111111111",
                            "descriptiveName": "Billing Truncation Test",
                            "status": "ENABLED",
                            "currencyCode": "USD",
                            "timeZone": "UTC",
                        }
                    }
                ]
            }
        if "FROM billing_setup" in q:
            resp = {
                "results": [
                    {
                        "billingSetup": {
                            "id": "9001",
                            "status": "PENDING",
                            "paymentsAccount": "customers/1111111111/paymentsAccounts/1111-2222",
                        }
                    }
                ]
            }
            if paginated["billing_has_next_page"]:
                resp["nextPageToken"] = "page-2-has-approved-billing"
            return resp
        if "FROM campaign" in q:
            return {
                "results": [
                    {
                        "campaign": {
                            "id": "10",
                            "name": "Search Core",
                            "status": "ENABLED",
                            "primaryStatus": "ELIGIBLE",
                            "primaryStatusReasons": [],
                        },
                        "campaignBudget": {"amountMicros": "50000000"},
                        "metrics": {"impressions": "100", "clicks": "10", "costMicros": "5000000"},
                    }
                ]
            }
        return {"results": []}

    monkeypatch.setattr("agency.api.request", fake_api_request)
    mcp = _FakeMCP()
    dt.register_diagnostics_tools(mcp)

    # 1. Truncated: MUST NOT assert NO_APPROVED_BILLING_SETUP in confirmed_causes; MUST record BILLING_SETUP_TRUNCATED in insufficient_data
    truncated_diag = json.loads(mcp.tools["diagnose_delivery"](customer_id="1111111111", days=14))
    assert truncated_diag["coverage_complete"] is False
    assert "NO_APPROVED_BILLING_SETUP" not in {c["code"] for c in truncated_diag["confirmed_causes"]}
    assert "BILLING_SETUP_TRUNCATED" in {i["code"] for i in truncated_diag["insufficient_data"]}

    # 2. Control (complete, non-truncated): MUST confirm NO_APPROVED_BILLING_SETUP
    paginated["billing_has_next_page"] = False
    complete_diag = json.loads(mcp.tools["diagnose_delivery"](customer_id="1111111111", days=14))
    assert complete_diag["coverage_complete"] is True
    assert "NO_APPROVED_BILLING_SETUP" in {c["code"] for c in complete_diag["confirmed_causes"]}
