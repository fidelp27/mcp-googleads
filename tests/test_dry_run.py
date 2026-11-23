"""Dry-run and approval workflow tests for the uniform mutation pipeline."""

import json

import pytest

from agency.mutations import execute_mutation


CUSTOMER_ID = "1234567890"
OPERATIONS = [{"create": {"name": "Launch campaign budget"}}]


@pytest.fixture
def fake_api(monkeypatch):
    """Replace the Google Ads mutate seam and record validate-only flags."""
    calls = []

    def fake_mutate(customer_id, service, operations, *, validate_only, partial_failure=False):
        calls.append(validate_only)
        if validate_only:
            return {"results": [{"resourceName": "customers/1/campaignBudgets/1"}]}
        return {
            "requestId": "req-123",
            "results": [{"resourceName": "customers/1/campaignBudgets/1"}],
        }

    monkeypatch.setattr("agency.mutations.mutate", fake_mutate)
    monkeypatch.setenv("READ_ONLY_MODE", "false")
    return calls


def _dry_run(expected_confirmation=None, operations=OPERATIONS):
    return json.loads(
        execute_mutation(
            tool_name="create_campaign_budget",
            customer_id=CUSTOMER_ID,
            service="campaignBudgets",
            operations=operations,
            dry_run=True,
            approval_id=None,
            confirmation_text=None,
            expected_confirmation=expected_confirmation,
        )
    )


def _execute(approval_id, confirmation_text=None, operations=OPERATIONS):
    return json.loads(
        execute_mutation(
            tool_name="create_campaign_budget",
            customer_id=CUSTOMER_ID,
            service="campaignBudgets",
            operations=operations,
            dry_run=False,
            approval_id=approval_id,
            confirmation_text=confirmation_text,
        )
    )


def test_dry_run_validates_without_executing(agency_data, fake_api):
    result = _dry_run()
    assert result["status"] == "validated"
    assert result["executed"] is False
    assert result["approval_id"]
    assert result["client_name"]
    # Only a validate-only call reached Google Ads; nothing was executed.
    assert fake_api == [True]


def test_execute_requires_approval_id(agency_data, fake_api):
    with pytest.raises(PermissionError):
        _execute(approval_id=None)
    assert fake_api == []


def test_dry_run_then_execute_applies_change(agency_data, fake_api):
    approval_id = _dry_run(expected_confirmation="Launch")["approval_id"]
    result = _execute(approval_id, confirmation_text="Launch")
    assert result["status"] == "executed"
    assert result["executed"] is True
    assert result["audit_id"]
    # One validate-only call, then one real mutation.
    assert fake_api == [True, False]


def test_execute_rejects_wrong_confirmation_text(agency_data, fake_api):
    approval_id = _dry_run(expected_confirmation="Launch")["approval_id"]
    with pytest.raises(PermissionError):
        _execute(approval_id, confirmation_text="wrong")
    # The real mutation was never attempted.
    assert fake_api == [True]


def test_execute_rejects_changed_payload(agency_data, fake_api):
    approval_id = _dry_run()["approval_id"]
    with pytest.raises(PermissionError):
        _execute(approval_id, operations=[{"create": {"name": "Different payload"}}])
    assert fake_api == [True]


def test_execute_is_idempotent_across_cycles(agency_data, fake_api):
    # First full cycle actually executes the mutation.
    first_approval = _dry_run()["approval_id"]
    first = _execute(first_approval)
    assert first["status"] == "executed"

    # A second identical cycle reuses the recorded result instead of re-mutating.
    second_approval = _dry_run()["approval_id"]
    second = _execute(second_approval)
    assert second["status"] == "already_executed"
    assert second["executed"] is True

    # The real mutation ran exactly once despite two execute calls.
    assert fake_api.count(False) == 1


def test_read_only_mode_blocks_dry_run(agency_data, monkeypatch):
    monkeypatch.setenv("READ_ONLY_MODE", "true")
    called = []
    monkeypatch.setattr("agency.mutations.mutate", lambda *a, **k: called.append(True) or {})
    with pytest.raises(PermissionError):
        _dry_run()
    assert called == []
