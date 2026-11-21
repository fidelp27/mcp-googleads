"""Tests for list_expiring_approvals — quickly detect approvals about to expire."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from mcp.server.fastmcp import FastMCP

from agency import governance, governance_tools


def _register_and_call(monkeypatch, approvals_dict, minutes_until_expiry=15):
    """Register the tool on a fresh FastMCP + call it with the given approvals map."""
    monkeypatch.setattr(governance_tools, "approvals", lambda: approvals_dict)
    mcp = FastMCP("test")
    governance_tools.register_governance_tools(mcp)
    result = asyncio.run(
        mcp.call_tool("list_expiring_approvals", {"minutes_until_expiry": minutes_until_expiry})
    )
    content = result[0] if isinstance(result, tuple) else result
    text = "\n".join(getattr(c, "text", "") for c in content if getattr(c, "text", None))
    return json.loads(text)


def _approval(minutes_from_now, status="pending", **extra):
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=minutes_from_now)
    return {
        "approval_id": extra.get("approval_id", "aid"),
        "tool_name": extra.get("tool_name", "update_campaign_budget"),
        "customer_id": extra.get("customer_id", "1234567890"),
        "client_name": extra.get("client_name", "Demo Account"),
        "operator": extra.get("operator", "fidel"),
        "created_at": (now - timedelta(minutes=5)).isoformat(),
        "expires_at": expires.isoformat(),
        "status": status,
        "expected_confirmation": extra.get("expected_confirmation", "confirm"),
        "client_approval_required": extra.get("client_approval_required", False),
        "client_approved": extra.get("client_approved", True),
    }


# ---------- filtering ----------


def test_finds_approvals_expiring_within_window(monkeypatch):
    approvals_dict = {
        "soon": _approval(minutes_from_now=5, approval_id="soon"),
        "later": _approval(minutes_from_now=60, approval_id="later"),
    }
    report = _register_and_call(monkeypatch, approvals_dict, minutes_until_expiry=15)
    ids = [a["approval_id"] for a in report["approvals"]]
    assert ids == ["soon"]


def test_excludes_non_pending_approvals(monkeypatch):
    approvals_dict = {
        "executed": _approval(minutes_from_now=5, status="executed", approval_id="executed"),
        "expired": _approval(minutes_from_now=-1, status="expired", approval_id="expired"),
        "pending_soon": _approval(minutes_from_now=5, approval_id="pending_soon"),
    }
    report = _register_and_call(monkeypatch, approvals_dict, minutes_until_expiry=15)
    ids = [a["approval_id"] for a in report["approvals"]]
    assert ids == ["pending_soon"]


def test_zero_matches_when_all_have_room(monkeypatch):
    approvals_dict = {
        "far": _approval(minutes_from_now=59, approval_id="far"),
    }
    report = _register_and_call(monkeypatch, approvals_dict, minutes_until_expiry=15)
    assert report["count"] == 0


# ---------- expiration semantics ----------


def test_already_expired_flagged_and_included(monkeypatch):
    approvals_dict = {
        "past": _approval(minutes_from_now=-2, approval_id="past"),
    }
    report = _register_and_call(monkeypatch, approvals_dict, minutes_until_expiry=15)
    assert report["already_expired_count"] == 1
    assert report["approvals"][0]["already_expired"] is True
    assert report["approvals"][0]["seconds_until_expiry"] < 0


def test_seconds_until_expiry_is_positive_for_future(monkeypatch):
    approvals_dict = {
        "future": _approval(minutes_from_now=10, approval_id="future"),
    }
    report = _register_and_call(monkeypatch, approvals_dict, minutes_until_expiry=15)
    seconds = report["approvals"][0]["seconds_until_expiry"]
    assert 590 <= seconds <= 610  # ~600s, allowing for a few seconds of jitter


# ---------- sort order ----------


def test_sorted_by_shortest_remaining_first(monkeypatch):
    approvals_dict = {
        "b": _approval(minutes_from_now=10, approval_id="b"),
        "a": _approval(minutes_from_now=2, approval_id="a"),
        "c": _approval(minutes_from_now=14, approval_id="c"),
    }
    report = _register_and_call(monkeypatch, approvals_dict, minutes_until_expiry=15)
    ids = [a["approval_id"] for a in report["approvals"]]
    assert ids == ["a", "b", "c"]


# ---------- edge cases ----------


def test_bad_expires_at_is_skipped(monkeypatch):
    good = _approval(minutes_from_now=5, approval_id="ok")
    broken = _approval(minutes_from_now=5, approval_id="broken")
    broken["expires_at"] = "not-a-date"
    approvals_dict = {"ok": good, "broken": broken}
    report = _register_and_call(monkeypatch, approvals_dict, minutes_until_expiry=15)
    ids = [a["approval_id"] for a in report["approvals"]]
    assert ids == ["ok"]


def test_missing_expires_at_is_skipped(monkeypatch):
    ok = _approval(minutes_from_now=5, approval_id="ok")
    broken = _approval(minutes_from_now=5, approval_id="broken")
    del broken["expires_at"]
    approvals_dict = {"ok": ok, "broken": broken}
    report = _register_and_call(monkeypatch, approvals_dict, minutes_until_expiry=15)
    ids = [a["approval_id"] for a in report["approvals"]]
    assert ids == ["ok"]


def test_report_shape():
    now = datetime.now(timezone.utc)
    approvals_dict = {"a": _approval(minutes_from_now=5, approval_id="a")}
    import pytest as _p
    import unittest.mock as m
    with m.patch.object(governance_tools, "approvals", return_value=approvals_dict):
        mcp = FastMCP("test")
        governance_tools.register_governance_tools(mcp)
        result = asyncio.run(
            mcp.call_tool("list_expiring_approvals", {"minutes_until_expiry": 15})
        )
        content = result[0] if isinstance(result, tuple) else result
        text = "\n".join(getattr(c, "text", "") for c in content if getattr(c, "text", None))
        report = json.loads(text)
    assert set(report.keys()) == {
        "as_of",
        "minutes_until_expiry",
        "count",
        "already_expired_count",
        "approvals",
    }


def test_rejects_negative_minutes(monkeypatch):
    monkeypatch.setattr(governance_tools, "approvals", lambda: {})
    mcp = FastMCP("test")
    governance_tools.register_governance_tools(mcp)
    with pytest.raises(Exception):
        asyncio.run(
            mcp.call_tool("list_expiring_approvals", {"minutes_until_expiry": -1})
        )
