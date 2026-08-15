"""Shared fixtures for agency guardrail and dry-run tests."""

import pytest


@pytest.fixture
def agency_data(tmp_path, monkeypatch):
    """Isolate audit/approval state on disk and reset guardrail environment.

    Redirects the append-only logs to a temporary directory and clears every
    guardrail environment variable so each test starts from documented defaults.
    """
    import agency.config as config
    import agency.governance as governance

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(governance, "DATA_DIR", data_dir)
    monkeypatch.setattr(governance, "AUDIT_PATH", data_dir / "audit.jsonl")
    monkeypatch.setattr(governance, "APPROVAL_PATH", data_dir / "pending_approvals.jsonl")

    for name in (
        "READ_ONLY_MODE",
        "ALLOWED_CUSTOMER_IDS",
        "BLOCKED_TOOLS",
        "MAX_DAILY_BUDGET_INCREASE_MICROS",
        "MAX_DAILY_BUDGET_INCREASE_USD",
        "MAX_BUDGET_INCREASE_PCT",
        "MAX_BID_MICROS",
        "CLIENT_APPROVAL_REQUIRED",
        "APPROVAL_TTL_MINUTES",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPERATOR_NAME", "tester")
    return data_dir
