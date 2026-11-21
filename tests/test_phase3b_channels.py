"""Phase 3B tests: PMax diagnostics, Shopping performance, Display/Demand Gen placements & video, and Auction Insights CSV."""

from __future__ import annotations

import json

import google_ads_server as server
from agency import channels_tools as ct
from agency.modules import configure_tool_catalog
from tests.test_phase1_catalog_profiles import _collect_untyped_objects


class _FakeMCP:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, **kwargs):
        def dec(fn):
            self.tools[fn.__name__] = fn
            return fn

        return dec


def test_pmax_shopping_and_placements_in_demo_mode(monkeypatch):
    monkeypatch.setenv("GOOGLE_ADS_DEMO_MODE", "true")
    mcp = _FakeMCP()
    ct.register_channel_tools(mcp)

    pmax = json.loads(mcp.tools["get_pmax_diagnostics"](customer_id="1111111111", days=30))
    assert pmax["currency_code"] == "EUR"
    assert pmax["summary"]["asset_groups_count"] == 2
    alert_codes = {a["code"] for a in pmax["alerts"]}
    assert {
        "PMAX_POOR_AD_STRENGTH",
        "PMAX_DISAPPROVED_ASSETS",
        "PMAX_LIMITED_OR_INELIGIBLE_ASSETS",
        "PMAX_ASSET_GROUP_ZERO_CONVERSIONS_SPEND",
        "PMAX_HIGH_MOBILE_APP_IMPRESSIONS",
    } <= alert_codes
    assert pmax["coverage_complete"] is True
    assert len(pmax["search_term_insights"]) == 1

    shop = json.loads(
        mcp.tools["get_shopping_performance"](
            customer_id="1111111111",
            group_by="product",
            min_waste_spend=50.0,
        )
    )
    assert shop["summary"]["wasted_items_count"] == 1
    assert shop["summary"]["total_wasted_spend"] == 210.0
    assert shop["top_winners"][0]["key"] == "SKU-ALPINE-01"
    assert shop["wasted_products"][0]["key"] == "SKU-SOCK-99"

    plc = json.loads(
        mcp.tools["get_placements_and_video_performance"](customer_id="1111111111", days=30)
    )
    assert plc["placements_count"] == 2
    assert plc["exclusion_candidates_count"] == 1
    assert plc["exclusion_candidates"][0]["placement_type"] == "MOBILE_APPLICATION"


def test_auction_insights_csv_parses_english_and_spanish_exports(tmp_path):
    en_csv = tmp_path / "auction_en.csv"
    en_csv.write_text(
        "Auction insights report\n"
        "October 1, 2026 - October 30, 2026\n"
        "Display URL domain,Impression share,Overlap rate,Position above rate,Top of page rate,Abs. Top of page rate,Outranking share\n"
        "You,42.5%,--,--,78.0%,31.0%,--\n"
        "rival-big.com,58.2%,64.0%,62.5%,84.1%,45.0%,38.0%\n"
        "small-shop.com,< 10%,15.0%,20.0%,40.0%,10.0%,72.0%\n",
        encoding="utf-8",
    )

    mcp = _FakeMCP()
    ct.register_channel_tools(mcp)
    res_en = json.loads(mcp.tools["analyze_auction_insights_csv"](csv_path=str(en_csv)))
    assert res_en["source"] == "google_ads_ui_csv_export"
    assert res_en["you"]["impression_share"] == 0.425
    assert res_en["top_competitor"]["domain"] == "rival-big.com"
    assert len(res_en["high_pressure_competitors"]) == 1
    small = next(c for c in res_en["competitors"] if c["domain"] == "small-shop.com")
    assert small["impression_share"] == 0.05  # < 10% midpoint estimate

    es_csv = tmp_path / "auction_es.csv"
    es_csv.write_text(
        "Informe de comparativa de subastas\n"
        "1 oct 2026 - 30 oct 2026\n"
        'Dominio de la URL visible,Cuota de impresiones,Porcentaje de superposición,Porcentaje de posición superior,Cuota de ranking superior\n'
        'Tú,"35,0%",--,--,--\n'
        'competidor.es,"48,5%","52,0%","55,0%","40,0%"\n',
        encoding="utf-8",
    )
    res_es = json.loads(mcp.tools["analyze_auction_insights_csv"](csv_path=str(es_csv)))
    assert res_es["you"]["impression_share"] == 0.35
    assert res_es["top_competitor"]["domain"] == "competidor.es"
    assert res_es["top_competitor"]["impression_share"] == 0.485


def test_all_channel_modules_pass_multi_ai_schema_linter():
    try:
        configure_tool_catalog(
            server.mcp,
            modules="core,diagnostics,measurement,search,pmax,shopping,display,demand-gen",
            read_only=True,
        )
        tools = server.mcp._tool_manager.list_tools()
        names = {t.name for t in tools}
        assert {
            "get_pmax_diagnostics",
            "get_shopping_performance",
            "get_placements_and_video_performance",
            "analyze_auction_insights_csv",
        } <= names
        for tool in tools:
            defs = tool.parameters.get("$defs") or {}
            assert _collect_untyped_objects(tool.parameters, defs) == [], f"{tool.name} failed schema lint"
    finally:
        configure_tool_catalog(server.mcp, modules=None, preset=None, read_only=True)
