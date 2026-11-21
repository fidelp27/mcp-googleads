"""Repository privacy and public-artifact hygiene tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_default_config_files_contain_no_private_client_data():
    clients = json.loads((PROJECT_ROOT / "config" / "clients.json").read_text(encoding="utf-8"))
    assert clients == {}, "config/clients.json tracked in Git must remain empty; use clients.example.json"

    branding = json.loads((PROJECT_ROOT / "config" / "branding.json").read_text(encoding="utf-8"))
    assert branding.get("agency_name") == "Your Agency"


def test_example_config_templates_are_valid_json_with_placeholder_domains():
    clients_example = json.loads(
        (PROJECT_ROOT / "config" / "clients.example.json").read_text(encoding="utf-8")
    )
    assert "1234567890" in clients_example
    entry = clients_example["1234567890"]
    assert entry["agency_contact"].endswith("@example.com")
    assert "example.com" in entry["website"]

    branding_example = json.loads(
        (PROJECT_ROOT / "config" / "branding.example.json").read_text(encoding="utf-8")
    )
    assert branding_example["agency_name"] == "Your Agency"
    assert "example.com" in branding_example["logo_url"]


def test_no_secrets_or_output_artifacts_tracked_in_git():
    if not (PROJECT_ROOT / ".git").exists():
        return
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    forbidden_prefixes = ("output/", "data/", "tmp/", "generated/")
    forbidden_exact = {".env", "credentials.json", "token.json", "google_ads_token.json"}
    violations = [
        path
        for path in tracked
        if path.startswith(forbidden_prefixes)
        or Path(path).name in forbidden_exact
        or path.endswith("client_secret.json")
    ]
    assert violations == [], f"Sensitive or runtime artifacts must not be tracked in Git: {violations}"
