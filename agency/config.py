"""Runtime configuration for agency features."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = Path(os.getenv("GOOGLE_ADS_CONFIG_DIR", PROJECT_ROOT / "config")).expanduser()
DATA_DIR = Path(os.getenv("GOOGLE_ADS_DATA_DIR", Path.cwd() / "data")).expanduser()
_context_lock = RLock()
BUILTIN_CONFIGS: dict[str, Any] = {
    "clients.json": {},
    "templates.json": {
        "local_search": {
            "channel_type": "SEARCH",
            "bidding_strategy": "MANUAL_CPC",
            "geo_targets": ["2032"],
            "language_codes": ["1003"],
        }
    },
    "presets.json": {
        "preset_ar": {
            "geo_targets": ["2032"],
            "language_codes": ["1003"],
            "currency": "ARS",
        },
        "preset_es": {
            "geo_targets": ["2724"],
            "language_codes": ["1003"],
            "currency": "EUR",
        },
        "preset_mx": {
            "geo_targets": ["2484"],
            "language_codes": ["1003"],
            "currency": "MXN",
        },
    },
    "alerts.json": {"defaults": {}},
    "branding.json": {
        "agency_name": "Your Agency",
        "report_title": "Google Ads Performance Report",
        "primary_color": "#1A73E8",
    },
}


def resolve_worker_file(path: Path) -> Path:
    """Confine file arguments from remote tools to the profile's file directory."""
    directory = os.getenv("MCP_REMOTE_FILES_DIR")
    if not directory:
        return path
    root = Path(directory).resolve()
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("File paths must stay within the remote worker's file directory.")
    return resolved


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _csv_env(name: str) -> frozenset[str]:
    return frozenset(item.strip() for item in os.getenv(name, "").split(",") if item.strip())


def load_json_config(filename: str, default: Any) -> Any:
    path = CONFIG_DIR / filename
    if not path.exists():
        data = BUILTIN_CONFIGS.get(filename, default)
    else:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid configuration file {path}: {error}") from error
    if filename == "clients.json" and not data:
        from .demo import demo_clients_config, is_demo_mode

        if is_demo_mode():
            return demo_clients_config()
    return data


@dataclass(frozen=True)
class Settings:
    operator_name: str
    read_only_mode: bool
    allowed_customer_ids: frozenset[str]
    blocked_tools: frozenset[str]
    max_daily_budget_increase_micros: int
    max_budget_increase_pct: float
    max_bid_micros: int
    rate_limit_per_minute: int
    approval_ttl_minutes: int
    client_approval_required: bool
    audit_retention_days: int


def get_settings() -> Settings:
    from .demo import DEMO_CUSTOMER_IDS, is_demo_mode

    demo = is_demo_mode()
    legacy_budget_units = float(os.getenv("MAX_DAILY_BUDGET_INCREASE_USD", "0") or 0)
    budget_cap_micros = int(
        os.getenv(
            "MAX_DAILY_BUDGET_INCREASE_MICROS",
            str(int(legacy_budget_units * 1_000_000)),
        )
    )
    allowed = frozenset(DEMO_CUSTOMER_IDS) if demo else _csv_env("ALLOWED_CUSTOMER_IDS")
    return Settings(
        operator_name=os.getenv("OPERATOR_NAME", "demo" if demo else "unknown"),
        read_only_mode=True if demo else _bool_env("READ_ONLY_MODE", True),
        allowed_customer_ids=allowed,
        blocked_tools=_csv_env("BLOCKED_TOOLS"),
        max_daily_budget_increase_micros=budget_cap_micros,
        max_budget_increase_pct=float(os.getenv("MAX_BUDGET_INCREASE_PCT", "100")),
        max_bid_micros=int(os.getenv("MAX_BID_MICROS", "10000000")),
        rate_limit_per_minute=max(1, int(os.getenv("GOOGLE_ADS_RATE_LIMIT_PER_MINUTE", "60"))),
        approval_ttl_minutes=max(1, int(os.getenv("APPROVAL_TTL_MINUTES", "60"))),
        client_approval_required=_bool_env("CLIENT_APPROVAL_REQUIRED", False),
        audit_retention_days=max(1, int(os.getenv("AUDIT_RETENTION_DAYS", "365"))),
    )


def normalize_customer_id(customer_id: str) -> str:
    digits = "".join(char for char in str(customer_id) if char.isdigit())
    if len(digits) != 10:
        raise ValueError("customer_id must contain exactly 10 digits")
    return digits


def ensure_customer_allowed(customer_id: str) -> str:
    normalized = normalize_customer_id(customer_id)
    allowed = get_settings().allowed_customer_ids
    if allowed:
        normalized_allowed = {normalize_customer_id(value) for value in allowed}
        if normalized not in normalized_allowed:
            raise PermissionError(f"Customer {normalized} is not included in ALLOWED_CUSTOMER_IDS")
    return normalized


def ensure_tool_allowed(tool_name: str, write: bool = False) -> None:
    settings = get_settings()
    if tool_name in settings.blocked_tools:
        raise PermissionError(f"Tool {tool_name} is disabled by BLOCKED_TOOLS")
    if write and settings.read_only_mode:
        raise PermissionError("Write tools are disabled because READ_ONLY_MODE=true")


def get_active_customer_id() -> str | None:
    with _context_lock:
        path = DATA_DIR / "active_context.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = data.get("customer_id")
            return normalize_customer_id(value) if value else None
        except (OSError, json.JSONDecodeError, ValueError):
            return None


def set_active_customer_id(customer_id: str) -> str:
    normalized = ensure_customer_allowed(customer_id)
    with _context_lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / "active_context.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"customer_id": normalized}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
    return normalized


def client_metadata(customer_id: str) -> dict[str, Any]:
    normalized = normalize_customer_id(customer_id)
    clients = load_json_config("clients.json", {})
    return dict(clients.get(normalized, {}))


def client_display_name(customer_id: str) -> str:
    metadata = client_metadata(customer_id)
    return str(metadata.get("name") or f"Google Ads customer {normalize_customer_id(customer_id)}")
