"""Phase 4 tests: optimistic locking, conversion-goal/placement/account-negative writes, partial bulk retries, and expanded revert."""

from __future__ import annotations

import json

import pytest

import google_ads_server as server
from agency import governance, governance_tools, mcc_tools, mutations, operations_tools
from agency.modules import OPERATIONS_TOOLS, configure_tool_catalog
from tests.test_phase1_catalog_profiles import _collect_untyped_objects


class _FakeMCP:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, **kwargs):
        def dec(fn):
            self.tools[fn.__name__] = fn
            return fn

        return dec


def test_optimistic_locking_rejects_stale_approval_when_live_state_changed(agency_data, monkeypatch):
    monkeypatch.setenv("READ_ONLY_MODE", "false")
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1234567890")

    live_state = {"primaryForGoal": True, "status": "ENABLED"}
    mutate_calls = []

    def fake_search(cid, query):
        return [
            {
                "conversionAction": {
                    "resourceName": f"customers/{cid}/conversionActions/1001",
                    "name": "Purchase Web",
                    "status": live_state["status"],
                    "category": "PURCHASE",
                    "primaryForGoal": live_state["primaryForGoal"],
                }
            }
        ]

    def fake_mutate(cid, service, operations, *, validate_only, partial_failure=False):
        mutate_calls.append((validate_only, service, operations))
        return {"results": [{}]}

    monkeypatch.setattr(operations_tools, "search", fake_search)
    monkeypatch.setattr(mutations, "mutate", fake_mutate)

    mcp = _FakeMCP()
    operations_tools.register_operations_tools(mcp)

    preview = json.loads(
        mcp.tools["set_conversion_action_primary"](
            customer_id="1234567890",
            conversion_action_id="1001",
            primary_for_goal=False,
            dry_run=True,
        )
    )
    assert preview["status"] == "validated"
    assert len(mutate_calls) == 1 and mutate_calls[0][0] is True

    # Simulate someone modifying the conversion action in the Google Ads UI before execution!
    live_state["primaryForGoal"] = False

    with pytest.raises(PermissionError, match="Stale approval"):
        mcp.tools["set_conversion_action_primary"](
            customer_id="1234567890",
            conversion_action_id="1001",
            primary_for_goal=False,
            dry_run=False,
            approval_id=preview["approval_id"],
            confirmation_text="Purchase Web",
        )

    # Live mutate must NEVER have been called
    assert len(mutate_calls) == 1
    assert governance.get_approval(preview["approval_id"])["status"] == "stale"


def test_conversion_goal_and_primary_writes_execute_and_revert(agency_data, monkeypatch):
    monkeypatch.setenv("READ_ONLY_MODE", "false")
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1234567890")

    mutate_calls = []
    monkeypatch.setattr(
        mutations,
        "mutate",
        lambda cid, service, ops, *, validate_only, partial_failure=False: (
            mutate_calls.append((validate_only, service, ops)) or {"results": [{}]}
        ),
    )
    monkeypatch.setattr(
        operations_tools,
        "search",
        lambda cid, query: (
            [
                {
                    "campaignConversionGoal": {
                        "resourceName": f"customers/{cid}/campaignConversionGoals/55~PURCHASE~WEBSITE",
                        "category": "PURCHASE",
                        "origin": "WEBSITE",
                        "biddable": True,
                    }
                }
            ]
            if "campaign_conversion_goal" in query
            else [
                {
                    "conversionAction": {
                        "resourceName": f"customers/{cid}/conversionActions/1001",
                        "name": "Purchase Web",
                        "status": "ENABLED",
                        "category": "PURCHASE",
                        "primaryForGoal": True,
                    }
                }
            ]
        ),
    )

    mcp = _FakeMCP()
    operations_tools.register_operations_tools(mcp)
    governance_tools.register_governance_tools(mcp)

    # 1. update_campaign_conversion_goal -> execute -> revert
    p1 = json.loads(
        mcp.tools["update_campaign_conversion_goal"](
            customer_id="1234567890",
            campaign_id="55",
            category="PURCHASE",
            origin="WEBSITE",
            biddable=False,
            dry_run=True,
        )
    )
    e1 = json.loads(
        mcp.tools["update_campaign_conversion_goal"](
            customer_id="1234567890",
            campaign_id="55",
            category="PURCHASE",
            origin="WEBSITE",
            biddable=False,
            dry_run=False,
            approval_id=p1["approval_id"],
            confirmation_text=p1["expected_confirmation"],
        )
    )
    assert e1["executed"] is True

    rev1 = json.loads(
        mcp.tools["revert_operation"](operation_id=e1["audit_id"], dry_run=True)
    )
    assert rev1["preview"]["service"] == "campaignConversionGoals"
    assert rev1["preview"]["operations"][0]["update"]["biddable"] is True

    # 2. set_conversion_action_primary -> execute -> revert
    p2 = json.loads(
        mcp.tools["set_conversion_action_primary"](
            customer_id="1234567890",
            conversion_action_id="1001",
            primary_for_goal=False,
            dry_run=True,
        )
    )
    e2 = json.loads(
        mcp.tools["set_conversion_action_primary"](
            customer_id="1234567890",
            conversion_action_id="1001",
            primary_for_goal=False,
            dry_run=False,
            approval_id=p2["approval_id"],
            confirmation_text="Purchase Web",
        )
    )
    assert e2["executed"] is True

    rev2 = json.loads(
        mcp.tools["revert_operation"](operation_id=e2["audit_id"], dry_run=True)
    )
    assert rev2["preview"]["service"] == "conversionActions"
    assert rev2["preview"]["operations"][0]["update"]["primaryForGoal"] is True


def test_exclude_placements_and_account_negative_keywords_validate(agency_data, monkeypatch):
    monkeypatch.setenv("READ_ONLY_MODE", "false")
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "1234567890")
    monkeypatch.setattr(
        mutations,
        "mutate",
        lambda cid, service, ops, *, validate_only, partial_failure=False: {"results": [{} for _ in ops]},
    )

    mcp = _FakeMCP()
    operations_tools.register_operations_tools(mcp)

    p_app = json.loads(
        mcp.tools["exclude_placements"](
            customer_id="1234567890",
            placements=["2-com.example.game", "2-com.example.game"],
            level="CAMPAIGN",
            campaign_id="101",
            placement_type="MOBILE_APPLICATION",
            dry_run=True,
        )
    )
    assert p_app["preview"]["service"] == "campaignCriteria"
    assert len(p_app["preview"]["operations"]) == 1  # deduplicated
    assert p_app["preview"]["operations"][0]["create"]["mobileApplication"]["appId"] == "2-com.example.game"

    p_acct = json.loads(
        mcp.tools["exclude_placements"](
            customer_id="1234567890",
            placements=["spam.example.com"],
            level="ACCOUNT",
            placement_type="PLACEMENT",
            dry_run=True,
        )
    )
    assert p_acct["preview"]["service"] == "customerNegativeCriteria"

    p_neg = json.loads(
        mcp.tools["add_account_negative_keywords"](
            customer_id="1234567890",
            keywords=["gratis", "empleo"],
            match_type="PHRASE",
            dry_run=True,
        )
    )
    assert p_neg["preview"]["service"] == "customerNegativeCriteria"
    assert len(p_neg["preview"]["operations"]) == 2


def test_mcc_bulk_apply_retries_only_failed_targets(agency_data, monkeypatch):
    monkeypatch.setenv("READ_ONLY_MODE", "false")
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "9999999999")
    monkeypatch.setenv("ALLOWED_CUSTOMER_IDS", "9999999999,1111111111,2222222222")

    monkeypatch.setattr(
        mcc_tools,
        "search",
        lambda cid, query: [{"campaign": {"status": "ENABLED", "name": f"Camp {cid}"}}],
    )

    executed_calls: list[str] = []
    fail_second = {"fail": True}

    def fake_mutate(cid, service, ops, *, validate_only, partial_failure=False):
        if not validate_only:
            executed_calls.append(cid)
            if cid == "2222222222" and fail_second["fail"]:
                raise RuntimeError("Transient API error on 2222222222")
        return {"results": [{}]}

    monkeypatch.setattr(mcc_tools, "mutate", fake_mutate)

    mcp = _FakeMCP()
    mcc_tools.register_mcc_tools(mcp)

    targets = {"1111111111": {"campaign_id": "10"}, "2222222222": {"campaign_id": "20"}}
    prev1 = json.loads(mcp.tools["mcc_bulk_apply"]("pause_campaign", targets, dry_run=True))
    run1 = json.loads(
        mcp.tools["mcc_bulk_apply"](
            "pause_campaign",
            targets,
            dry_run=False,
            approval_id=prev1["approval_id"],
            confirmation_text=prev1["expected_confirmation"],
        )
    )
    assert run1["status"] == "partial"
    assert run1["executed_count"] == 1
    assert run1["failed_count"] == 1
    assert run1["retryable_customer_ids"] == ["2222222222"]
    assert executed_calls == ["1111111111", "2222222222"]

    # Now retry ONLY failed targets
    fail_second["fail"] = False
    executed_calls.clear()
    prev2 = json.loads(
        mcp.tools["mcc_bulk_apply"](
            "pause_campaign",
            targets,
            dry_run=True,
            retry_failed_from_approval_id=prev1["approval_id"],
        )
    )
    assert prev2["target_count"] == 1
    assert prev2["previews"][0]["customer_id"] == "2222222222"

    run2 = json.loads(
        mcp.tools["mcc_bulk_apply"](
            "pause_campaign",
            targets,
            dry_run=False,
            approval_id=prev2["approval_id"],
            confirmation_text=prev2["expected_confirmation"],
            retry_failed_from_approval_id=prev1["approval_id"],
        )
    )
    assert run2["status"] == "completed"
    assert executed_calls == ["2222222222"]  # 1111111111 was NOT duplicated!
    assert run2["skipped_already_executed"] == ["1111111111"]


def test_phase4_operations_tools_hidden_in_read_only_and_pass_schema_lint():
    phase4_tools = {
        "update_campaign_conversion_goal",
        "set_conversion_action_primary",
        "exclude_placements",
        "add_account_negative_keywords",
    }
    assert phase4_tools <= OPERATIONS_TOOLS
    try:
        configure_tool_catalog(server.mcp, preset="full", read_only=True)
        ro_names = {t.name for t in server.mcp._tool_manager.list_tools()}
        assert phase4_tools.isdisjoint(ro_names)

        configure_tool_catalog(server.mcp, preset="full", read_only=False)
        for tool in server.mcp._tool_manager.list_tools():
            if tool.name in phase4_tools:
                defs = tool.parameters.get("$defs") or {}
                assert _collect_untyped_objects(tool.parameters, defs) == []
    finally:
        configure_tool_catalog(server.mcp, modules=None, preset=None, read_only=True)
