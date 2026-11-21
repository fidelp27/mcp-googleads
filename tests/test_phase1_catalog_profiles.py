"""Phase 1 tests: modular catalog, customizable profiles (MD+TOML), context bounds, and multi-AI schema linter."""

from __future__ import annotations

import json

import pytest

import google_ads_server as server
from agency import analysis_profiles, api, config
from agency.modules import (
    CORE_TOOLS,
    MODULE_TOOLS,
    OPERATIONS_TOOLS,
    PRESETS,
    configure_tool_catalog,
    resolve_modules,
)


# ---------- Modular catalog & read-only enforcement ----------


def test_core_module_is_compact_and_has_zero_google_ads_mutations():
    assert len(CORE_TOOLS) <= 25
    assert CORE_TOOLS.isdisjoint(OPERATIONS_TOOLS)


def test_configure_tool_catalog_hides_write_tools_in_read_only_mode():
    try:
        info_default = configure_tool_catalog(server.mcp, modules=None, preset=None, read_only=True)
        assert info_default["modules"] == ["core"]
        assert info_default["tool_count"] <= 25
        exposed = {t.name for t in server.mcp._tool_manager.list_tools()}
        assert exposed.isdisjoint(OPERATIONS_TOOLS)

        # Even requesting operations or preset=full while read_only=True must strip operations
        info_full_ro = configure_tool_catalog(server.mcp, preset="full", read_only=True)
        assert "operations" not in info_full_ro["modules"]
        exposed_ro = {t.name for t in server.mcp._tool_manager.list_tools()}
        assert exposed_ro.isdisjoint(OPERATIONS_TOOLS)
        assert "get_conversion_actions" in exposed_ro
        assert "get_search_terms_report" in exposed_ro

        # Enabling writes exposes operations when requested
        info_full_rw = configure_tool_catalog(server.mcp, preset="full", read_only=False)
        assert "operations" in info_full_rw["modules"]
        exposed_rw = {t.name for t in server.mcp._tool_manager.list_tools()}
        assert "pause_campaign" in exposed_rw
        assert "update_campaign_budget" in exposed_rw
    finally:
        configure_tool_catalog(server.mcp, modules=None, preset=None, read_only=True)


def test_presets_and_invalid_modules():
    for preset_name in PRESETS:
        mods, tools, ro = resolve_modules(preset=preset_name, read_only=True)
        assert "core" in mods
        assert ro is True
        assert tools.isdisjoint(OPERATIONS_TOOLS)

    with pytest.raises(ValueError, match="Unknown preset"):
        resolve_modules(preset="nonexistent")

    with pytest.raises(ValueError, match="Unknown module"):
        resolve_modules(modules="core,unknown_mod")


# ---------- Customizable analysis profiles (Markdown + TOML) ----------


def test_builtin_analysis_profiles_load_with_toml_and_markdown():
    profiles = analysis_profiles.list_profiles()
    ids = {p["id"] for p in profiles}
    assert {"leads-audit", "ecommerce-audit"} <= ids

    leads = analysis_profiles.load_profile("leads-audit")
    assert leads["parameters"]["days"] == 30
    assert leads["parameters"]["target_cpa"] == 25.0
    assert leads["thresholds"]["max_cpa_micros"] == 25_000_000
    assert "Lead Generation Audit" in leads["instructions_markdown"]
    assert "core" in leads["recommended_modules"]


def test_user_profile_overrides_builtin_and_merges_alert_defaults(tmp_path, monkeypatch):
    user_profiles = tmp_path / "profiles"
    user_profiles.mkdir(parents=True)
    (user_profiles / "custom-b2b.toml").write_text(
        'id = "custom-b2b"\n'
        'name = "Custom B2B"\n'
        'recommended_modules = ["core", "measurement"]\n'
        "[parameters]\n"
        "days = 14\n"
        "target_cpa = 80.0\n"
        "[thresholds]\n"
        "max_cpa_micros = 80000000\n",
        encoding="utf-8",
    )
    (user_profiles / "custom-b2b.md").write_text(
        "# Custom B2B Audit\nFocus on pipeline value.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(analysis_profiles, "CONFIG_DIR", tmp_path)

    loaded = analysis_profiles.load_profile("custom-b2b")
    assert loaded["source"] == "user"
    assert loaded["parameters"]["days"] == 14
    assert loaded["thresholds"]["max_cpa_micros"] == 80_000_000
    # Inherits defaults from alerts.json when not overridden
    assert "min_ctr" in loaded["thresholds"]
    assert "Focus on pipeline value" in loaded["instructions_markdown"]


def test_analysis_profile_rejects_path_traversal():
    with pytest.raises(ValueError, match="profile_id"):
        analysis_profiles.load_profile("../secrets")


def test_analysis_profile_double_exposure_tool_and_prompt():
    class _MiniMCP:
        def __init__(self):
            self.tools = {}
            self.prompts = {}

        def tool(self, **kwargs):
            def dec(fn):
                self.tools[fn.__name__] = fn
                return fn

            return dec

        def prompt(self, **kwargs):
            def dec(fn):
                self.prompts[fn.__name__] = fn
                return fn

            return dec

    mini = _MiniMCP()
    analysis_profiles.register_analysis_profile_tools(mini)
    assert {"list_analysis_profiles", "get_analysis_profile"} <= mini.tools.keys()
    assert "analysis_profile_prompt" in mini.prompts

    raw = mini.tools["get_analysis_profile"]("leads-audit", "1234567890")
    data = json.loads(raw)
    assert data["reproducibility_header"]["profile_id"] == "leads-audit"
    assert data["reproducibility_header"]["customer_id"] == "1234567890"
    assert "READ_ONLY_MODE" in data["security_notice"]

    prompt_text = mini.prompts["analysis_profile_prompt"]("leads-audit", "1234567890")
    assert "leads-audit" in prompt_text
    assert "1234567890" in prompt_text
    assert "READ_ONLY_MODE" in prompt_text


# ---------- Context-window row cap (agency.api.search_with_meta) ----------


def test_search_with_meta_caps_rows_and_sets_truncated(monkeypatch):
    monkeypatch.delenv("GOOGLE_ADS_DEMO_MODE", raising=False)
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1234567890")

    pages = [
        {"results": [{"i": n} for n in range(10)], "nextPageToken": "p2"},
        {"results": [{"i": n} for n in range(10, 20)], "nextPageToken": "p3"},
    ]
    calls = []

    def fake_request(method, path, *, customer_id, body=None, **kwargs):
        calls.append(body)
        return pages[len(calls) - 1]

    monkeypatch.setattr(api, "request", fake_request)
    meta = api.search_with_meta("1234567890", "SELECT campaign.id FROM campaign", max_rows=12)
    assert meta["returned_rows"] == 12
    assert meta["truncated"] is True
    assert meta["max_rows"] == 12
    assert len(meta["rows"]) == 12
    assert len(calls) == 2  # stopped before fetching page 3


# ---------- Multi-AI JSON Schema compatibility linter ----------


def _collect_untyped_objects(schema: dict, defs: dict, path: str = "") -> list[str]:
    issues: list[str] = []
    if "$ref" in schema:
        ref = schema["$ref"]
        if ref.startswith("#/$defs/"):
            target = ref.split("/", 2)[-1]
            if target not in defs:
                issues.append(f"{path}: broken $ref {ref}")
        return issues
    if schema.get("type") == "object" and "properties" not in schema:
        issues.append(f"{path}: untyped object without explicit properties")
    for sub in schema.get("anyOf", []):
        if isinstance(sub, dict):
            issues.extend(_collect_untyped_objects(sub, defs, path))
    for key, prop in (schema.get("properties") or {}).items():
        if isinstance(prop, dict):
            issues.extend(_collect_untyped_objects(prop, defs, f"{path}.{key}" if path else key))
    items = schema.get("items")
    if isinstance(items, dict):
        issues.extend(_collect_untyped_objects(items, defs, f"{path}[]"))
    return issues


def test_core_and_read_modules_pass_strict_multi_ai_schema_linter():
    try:
        configure_tool_catalog(server.mcp, preset="core", read_only=True)
        core_tools = server.mcp._tool_manager.list_tools()
        assert len(core_tools) <= 25
        for tool in core_tools:
            assert tool.description and tool.description.strip(), f"{tool.name} missing description"
            assert tool.annotations is not None, f"{tool.name} missing ToolAnnotations"
            assert isinstance(tool.annotations.readOnlyHint, bool), f"{tool.name} readOnlyHint must be bool"
            schema = tool.parameters
            assert schema.get("type") == "object", f"{tool.name} top-level schema must be object"
            defs = schema.get("$defs") or {}
            untyped = _collect_untyped_objects(schema, defs)
            assert untyped == [], f"{tool.name} has non-portable JSON Schema for Gemini/DeepSeek/Kimi: {untyped}"
    finally:
        configure_tool_catalog(server.mcp, modules=None, preset=None, read_only=True)
