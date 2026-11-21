"""Browser OAuth, preflight diagnostics (doctor), and desktop MCP registration."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from oauthlib.oauth2 import OAuth2Error


NAME = "google_ads_mcp"
SCOPE = "https://www.googleapis.com/auth/adwords"
API_VERSION = os.environ.get("GOOGLE_ADS_API_VERSION", "v25")
SUPPORTED_CLIENTS = (
    "claude-desktop",
    "claude-code",
    "codex",
    "antigravity",
    "gemini-cli",
    "generic",
    "cli",
)
SUPPORTED_PRESETS = ("core", "leads", "ecommerce", "agency", "full")


def _validate_catalog_selection(preset: str | None, modules: str | None) -> tuple[str | None, str | None]:
    clean_preset = preset.strip().lower() if isinstance(preset, str) and preset.strip() else None
    clean_modules = (
        ",".join(part.strip().lower() for part in modules.split(",") if part.strip())
        if isinstance(modules, str) and modules.strip()
        else None
    )
    if clean_preset or clean_modules:
        from agency.modules import resolve_modules

        resolve_modules(preset=clean_preset, modules=clean_modules, read_only=True)
    return clean_preset, clean_modules


def default_profile() -> Path:
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "google-ads-mcp" / "profile.json"


def atomic_write(path: Path, contents: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(contents)
            handle.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def customer_id(value: str) -> str:
    clean = value.strip()
    if not re.fullmatch(r"[0-9]{10}|[0-9]{3}-[0-9]{3}-[0-9]{4}", clean):
        raise ValueError("Enter a 10-digit Google Ads customer ID.")
    return clean.replace("-", "")


def parse_customer_ids(raw: str) -> list[str]:
    parts = [item.strip() for item in raw.split(",") if item.strip()]
    if not parts:
        raise ValueError("Provide at least one 10-digit Google Ads customer ID.")
    return list(dict.fromkeys(customer_id(item) for item in parts))


def diagnose_google_ads_error(status_code: int, codes: list[str]) -> tuple[str, str]:
    """Return a normalized diagnostic code and actionable remediation step."""
    code_set = set(codes)
    if "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION" in code_set or "DEVELOPER_TOKEN_NOT_APPROVED" in code_set:
        return (
            "ACCESS_LEVEL_INSUFFICIENT",
            "Apply for production access in Google Cloud (Google Ads API page) or Google Ads API Center: "
            "https://developers.google.com/google-ads/api/docs/api-policy/access-levels",
        )
    if "DEVELOPER_TOKEN_PROHIBITED" in code_set:
        return (
            "DEVELOPER_TOKEN_PROHIBITED",
            "This Google Cloud project is bound to a different Developer Token. Create a dedicated Cloud project "
            "or use the Developer Token originally paired with this project.",
        )
    if "USER_PERMISSION_DENIED" in code_set or "CUSTOMER_NOT_ENABLED" in code_set:
        return (
            "USER_PERMISSION_DENIED",
            "Check your Google Ads role, verify the target customer ID, and pass --manager <MCC_ID> "
            "if you access this account through a Manager Account (MCC).",
        )
    if status_code == 401 or "OAUTH_TOKEN_INVALID" in code_set or "AUTHENTICATION_ERROR" in code_set:
        return (
            "INVALID_CREDENTIALS",
            "OAuth credentials are invalid or expired. Re-run connect to authorize again in your browser.",
        )
    if "CUSTOMER_NOT_FOUND" in code_set or status_code == 404:
        return (
            "ACCOUNT_NOT_FOUND",
            "Verify the 10-digit Google Ads customer ID (without typos) and ensure the account is active.",
        )
    return (
        "API_ERROR",
        "Inspect the Google Ads error codes above and confirm API enablement, OAuth consent user, and account permissions.",
    )


def ads_request(method: str, endpoint: str, headers: dict, **kwargs) -> dict:
    response = requests.request(
        method,
        f"https://googleads.googleapis.com/{API_VERSION}/{endpoint}",
        headers=headers,
        timeout=30,
        **kwargs,
    )
    if not response.ok:
        try:
            error = response.json().get("error", {})
            codes = [
                code
                for detail in error.get("details", [])
                for item in detail.get("errors", [])
                for code in item.get("errorCode", {}).values()
            ]
        except (ValueError, AttributeError, TypeError):
            codes = []
        _, hint = diagnose_google_ads_error(response.status_code, codes)
        message = f"Google Ads returned HTTP {response.status_code}"
        if codes:
            message += f" ({', '.join(codes)})"
        raise ValueError(f"{message}. {hint}")
    return response.json()


def discover_mcc_clients(manager_id: str, headers: dict[str, str]) -> list[dict[str, Any]]:
    """Return active non-manager child accounts under an MCC."""
    mcc_headers = {**headers, "login-customer-id": manager_id}
    result = ads_request(
        "POST",
        f"customers/{manager_id}/googleAds:search",
        mcc_headers,
        json={
            "query": (
                "SELECT customer_client.id, customer_client.descriptive_name, "
                "customer_client.currency_code, customer_client.manager, customer_client.status "
                "FROM customer_client "
                "WHERE customer_client.manager = FALSE AND customer_client.status = 'ENABLED' "
                "ORDER BY customer_client.descriptive_name"
            )
        },
    )
    clients: list[dict[str, Any]] = []
    for row in result.get("results", []):
        client = row.get("customerClient", {}) or {}
        cid = str(client.get("id") or "")
        if cid:
            clients.append(
                {
                    "id": customer_id(cid),
                    "name": client.get("descriptiveName") or cid,
                    "currency_code": client.get("currencyCode") or "UNKNOWN",
                }
            )
    return clients


def authorize(args: argparse.Namespace, profile: Path) -> None:
    source = args.oauth_client or os.environ.get("GOOGLE_ADS_DESKTOP_CLIENT_JSON")
    if not source:
        print(
            "Need a Desktop OAuth client JSON? Create one in Google Cloud:\n"
            "  1. Enable Google Ads API: https://console.cloud.google.com/apis/library/googleads.googleapis.com\n"
            "  2. Configure Google Auth Platform & add scope https://www.googleapis.com/auth/adwords\n"
            "  3. Create an OAuth Client ID of type 'Desktop app' and download the JSON.\n"
        )
        source = input("Path to your Google Cloud Desktop OAuth client JSON: ").strip().strip('"')
    config = json.loads(Path(source).expanduser().read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict) or not isinstance(config.get("installed"), dict):
        raise ValueError("Download an OAuth client JSON of type Desktop app from Google Cloud.")
    # Use Google's endpoints even if the imported document specifies other URLs.
    installed = config["installed"]
    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": installed["client_id"],
                "client_secret": installed["client_secret"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        [SCOPE],
        autogenerate_code_verifier=True,
    )
    try:
        credentials = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            timeout_seconds=180,
            prompt="consent",
            open_browser=not args.no_browser,
        )
    except (OAuth2Error, AttributeError, requests.RequestException):
        raise ValueError(
            "Google authorization failed or timed out. Run connect again and complete consent in your browser."
        ) from None
    if not credentials.refresh_token:
        raise ValueError("Google did not return a refresh token. Run connect again and grant access.")
    headers = {"Authorization": f"Bearer {credentials.token}"}
    accessible = ads_request("GET", "customers:listAccessibleCustomers", headers)
    accounts = [name.rsplit("/", 1)[-1] for name in accessible.get("resourceNames", [])]
    if not accounts:
        raise ValueError("The authorized Google user has no accessible Google Ads accounts.")
    manager = customer_id(args.manager) if args.manager else ""
    all_mcc_clients = bool(getattr(args, "all_mcc_clients", False))

    if getattr(args, "customer", None):
        targets = parse_customer_ids(args.customer)
    elif all_mcc_clients:
        if not manager:
            raise ValueError("--all-mcc-clients requires --manager <MCC_ID>.")
        discovered = discover_mcc_clients(manager, headers)
        if not discovered:
            raise ValueError(f"Manager {manager} returned no enabled client accounts.")
        targets = [item["id"] for item in discovered]
    elif len(accounts) == 1 and not manager:
        targets = [customer_id(accounts[0])]
    else:
        print("Directly accessible Google Ads IDs: " + ", ".join(accounts))
        targets = parse_customer_ids(
            input("Google Ads account ID(s) to connect (comma-separated for multiple): ").strip()
        )

    if manager:
        headers["login-customer-id"] = manager
    for target in targets:
        result = ads_request(
            "POST",
            f"customers/{target}/googleAds:search",
            headers,
            json={"query": "SELECT customer.id, customer.manager FROM customer LIMIT 1"},
        )
        if not result.get("results"):
            raise ValueError(f"The selected account {target} returned no customer information.")

    preset_val, modules_val = _validate_catalog_selection(
        getattr(args, "preset", None),
        getattr(args, "modules", None),
    )
    payload: dict[str, Any] = {
        "credentials": json.loads(credentials.to_json()),
        "customer": targets[0],
        "customers": targets,
        "manager": manager,
    }
    if preset_val:
        payload["preset"] = preset_val
    if modules_val:
        payload["modules"] = modules_val
    atomic_write(profile, json.dumps(payload, indent=2))
    print(f"Connected Google Ads account(s) {', '.join(targets)} in read-only mode.")


def server_command(
    profile: Path,
    *,
    preset: str | None = None,
    modules: str | None = None,
) -> list[str]:
    clean_preset, clean_modules = _validate_catalog_selection(preset, modules)
    cmd = [
        sys.executable,
        "-E",
        "-s",
        str(Path(__file__).resolve()),
        "--profile",
        str(profile),
        "serve",
    ]
    if clean_preset:
        cmd.extend(["--preset", clean_preset])
    if clean_modules:
        cmd.extend(["--modules", clean_modules])
    return cmd


def demo_server_command(
    *,
    preset: str | None = None,
    modules: str | None = None,
) -> list[str]:
    clean_preset, clean_modules = _validate_catalog_selection(preset, modules)
    cmd = [
        sys.executable,
        "-E",
        "-s",
        str(Path(__file__).resolve().with_name("google_ads_server.py")),
        "--transport",
        "stdio",
        "--demo",
    ]
    if clean_preset:
        cmd.extend(["--preset", clean_preset])
    if clean_modules:
        cmd.extend(["--modules", clean_modules])
    return cmd


def _register_json_mcp_servers(path: Path, entry: dict[str, Any]) -> None:
    config = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    servers = config.setdefault("mcpServers", {})
    if NAME in servers and servers[NAME] != entry:
        raise ValueError(f"{NAME} is already configured in {path}. Rename or remove that entry first.")
    if NAME not in servers:
        servers[NAME] = entry
        atomic_write(path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")


def register_client(
    client: str,
    profile: Path | None,
    *,
    demo: bool = False,
    preset: str | None = None,
    modules: str | None = None,
) -> None:
    if demo:
        command, *arguments = demo_server_command(preset=preset, modules=modules)
    else:
        if profile is None:
            raise ValueError("Profile path is required unless demo=True.")
        command, *arguments = server_command(profile, preset=preset, modules=modules)
    entry = {"command": command, "args": arguments}

    if client == "codex":
        path = Path(os.getenv("CODEX_HOME", Path.home() / ".codex")) / "config.toml"
        original = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        existing = tomllib.loads(original).get("mcp_servers", {}).get(NAME)
        if existing is not None:
            if all(existing.get(key) == value for key, value in entry.items()):
                return
            raise ValueError(f"{NAME} is already configured in {path}. Rename or remove that entry first.")
        updated = original.rstrip() + f"\n\n[mcp_servers.{NAME}]\n"
        updated += "".join(f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in entry.items())
        tomllib.loads(updated)
        atomic_write(path, updated)
    elif client == "claude-code":
        cli = shutil.which("claude.exe" if os.name == "nt" else "claude")
        if not cli:
            raise ValueError("Claude Code CLI is not on PATH. Run with --client generic for manual JSON config.")
        subprocess.run(
            [cli, "mcp", "add-json", "--scope", "user", NAME, json.dumps({"type": "stdio", **entry})],
            check=True,
        )
    elif client == "claude-desktop":
        if sys.platform == "win32":
            root = Path(os.getenv("APPDATA", Path.home() / "AppData/Roaming")) / "Claude"
        elif sys.platform == "darwin":
            root = Path.home() / "Library/Application Support/Claude"
        else:
            raise ValueError("Use --client generic to get the stdio configuration for your desktop client.")
        _register_json_mcp_servers(root / "claude_desktop_config.json", entry)
    elif client == "antigravity":
        root = Path(os.getenv("ANTIGRAVITY_CONFIG_DIR", Path.home() / ".gemini/antigravity"))
        _register_json_mcp_servers(root / "mcp_config.json", entry)
    elif client == "gemini-cli":
        root = Path(os.getenv("GEMINI_HOME", Path.home() / ".gemini"))
        _register_json_mcp_servers(root / "settings.json", entry)
    else:
        # "generic" and "cli": standard MCP JSON snippet usable in Cursor, VS Code, Cherry Studio, Cline, Kimi, etc.
        print(json.dumps({"mcpServers": {NAME: entry}}, indent=2, ensure_ascii=False))


def _profile_customers(config: dict[str, Any]) -> list[str]:
    if isinstance(config.get("customers"), list) and config["customers"]:
        return [customer_id(str(item)) for item in config["customers"]]
    if config.get("customer"):
        return [customer_id(str(config["customer"]))]
    return []


def run_doctor(profile: Path, *, offline: bool = False) -> dict[str, Any]:
    """Run non-destructive preflight checks for local profile or .env configuration."""
    checks: list[dict[str, Any]] = []

    def add(name: str, status: str, detail: str, *, code: str | None = None, next_step: str | None = None) -> None:
        item: dict[str, Any] = {"check": name, "status": status, "detail": detail}
        if code:
            item["code"] = code
        if next_step:
            item["next_step"] = next_step
        checks.append(item)

    # 1. Runtime & Python version
    py_ok = sys.version_info >= (3, 11)
    add(
        "python_version",
        "pass" if py_ok else "fail",
        f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        code=None if py_ok else "UNSUPPORTED_PYTHON",
        next_step=None if py_ok else "Install Python 3.11 or newer.",
    )

    # 2. Detect configuration mode (managed profile vs .env)
    mode = "managed_profile" if profile.is_file() else "env"
    creds: Credentials | None = None
    targets: list[str] = []
    manager = ""
    dev_token = os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN", "")

    if mode == "managed_profile":
        try:
            data = json.loads(profile.read_text(encoding="utf-8"))
            targets = _profile_customers(data)
            manager = customer_id(data["manager"]) if data.get("manager") else ""
            raw_creds = data.get("credentials") or {}
            if not raw_creds.get("refresh_token"):
                add(
                    "credentials",
                    "fail",
                    f"Managed profile at {profile} is missing a refresh_token.",
                    code="INVALID_CREDENTIALS",
                    next_step="Re-run `google-ads-mcp-server connect` and approve access in your browser.",
                )
            else:
                creds = Credentials.from_authorized_user_info(raw_creds, [SCOPE])
                add(
                    "credentials",
                    "pass",
                    f"Managed OAuth profile loaded ({len(targets)} account(s), manager={'yes' if manager else 'no'}).",
                )
        except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
            add(
                "credentials",
                "fail",
                f"Managed profile is unreadable or invalid: {exc}",
                code="INVALID_CREDENTIALS",
                next_step="Run `google-ads-mcp-server connect` to recreate the profile.",
            )
    else:
        auth_type = os.getenv("GOOGLE_ADS_AUTH_TYPE", "oauth").strip().lower()
        creds_path_raw = os.getenv("GOOGLE_ADS_CREDENTIALS_PATH", "")
        token_path_raw = os.getenv("GOOGLE_ADS_TOKEN_PATH", "")
        manager = customer_id(os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")) if os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID") else ""
        allowed_raw = os.getenv("ALLOWED_CUSTOMER_IDS", "")
        if allowed_raw:
            try:
                targets = parse_customer_ids(allowed_raw)
            except ValueError as exc:
                add(
                    "allowed_customer_ids",
                    "fail",
                    f"Invalid ALLOWED_CUSTOMER_IDS: {exc}",
                    code="ACCOUNT_OUT_OF_SCOPE",
                    next_step="Use comma-separated 10-digit Google Ads customer IDs.",
                )
        if not creds_path_raw:
            add(
                "credentials",
                "fail",
                "No managed profile found and GOOGLE_ADS_CREDENTIALS_PATH is not set.",
                code="MISSING_CREDENTIALS",
                next_step="Run `google-ads-mcp-server connect`, configure `.env`, or try `--demo` with no credentials.",
            )
        else:
            creds_path = Path(creds_path_raw).expanduser()
            token_path = Path(token_path_raw).expanduser() if token_path_raw else None
            if not creds_path.exists():
                add(
                    "credentials",
                    "fail",
                    "Configured GOOGLE_ADS_CREDENTIALS_PATH does not exist.",
                    code="INVALID_CREDENTIALS",
                    next_step="Verify the file path in `.env` or run `google-ads-mcp-server connect`.",
                )
            elif auth_type == "oauth" and token_path and token_path.resolve() == creds_path.resolve():
                add(
                    "credentials",
                    "fail",
                    "GOOGLE_ADS_TOKEN_PATH points to the same file as GOOGLE_ADS_CREDENTIALS_PATH.",
                    code="INVALID_CREDENTIALS",
                    next_step="Use separate files for the OAuth client JSON and the authorized user token JSON.",
                )
            else:
                try:
                    raw = json.loads(creds_path.read_text(encoding="utf-8"))
                    if auth_type == "oauth":
                        is_client_json = "installed" in raw or "web" in raw
                        if is_client_json:
                            effective_token = token_path or creds_path.with_name("google_ads_token.json")
                            if not effective_token.exists():
                                add(
                                    "credentials",
                                    "fail",
                                    "OAuth client JSON found, but authorized user token file does not exist yet.",
                                    code="INVALID_CREDENTIALS",
                                    next_step="Run `python scripts/authorize_oauth.py` or `google-ads-mcp-server connect`.",
                                )
                            else:
                                token_data = json.loads(effective_token.read_text(encoding="utf-8"))
                                creds = Credentials.from_authorized_user_info(token_data, [SCOPE])
                                add("credentials", "pass", "OAuth client and user token files are valid.")
                        else:
                            creds = Credentials.from_authorized_user_info(raw, [SCOPE])
                            add("credentials", "pass", "Authorized OAuth user token file is valid.")
                    elif auth_type == "service_account":
                        add("credentials", "pass", "Service account JSON file is present and valid JSON.")
                    else:
                        add(
                            "credentials",
                            "fail",
                            f"Unsupported GOOGLE_ADS_AUTH_TYPE={auth_type!r}.",
                            code="INVALID_CREDENTIALS",
                            next_step="Set GOOGLE_ADS_AUTH_TYPE to `oauth` or `service_account`.",
                        )
                except (OSError, json.JSONDecodeError, ValueError) as exc:
                    add(
                        "credentials",
                        "fail",
                        f"Credentials file is not valid JSON: {exc}",
                        code="INVALID_CREDENTIALS",
                        next_step="Re-download the credentials JSON from Google Cloud.",
                    )

    # 3. Guardrails status
    read_only = (
        True
        if mode == "managed_profile"
        else os.getenv("READ_ONLY_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}
    )
    add(
        "guardrails",
        "pass" if read_only or targets else "warn",
        f"read_only_mode={read_only}, scoped_accounts={len(targets)}",
        next_step=None
        if read_only or targets
        else "Configure ALLOWED_CUSTOMER_IDS before enabling write operations.",
    )

    # 4. Live API checks (skipped in --offline mode or when credentials are missing)
    if offline:
        add("live_api", "warn", "Skipped live Google Ads API checks (--offline).")
    elif creds is not None:
        try:
            if not creds.valid:
                creds.refresh(Request())
            headers: dict[str, str] = {"Authorization": f"Bearer {creds.token}"}
            if dev_token:
                headers["developer-token"] = dev_token
            accessible = ads_request("GET", "customers:listAccessibleCustomers", headers)
            accessible_ids = [
                name.rsplit("/", 1)[-1] for name in accessible.get("resourceNames", [])
            ]
            if not accessible_ids:
                add(
                    "accessible_accounts",
                    "fail",
                    "OAuth token is valid, but this Google user has no accessible Google Ads accounts.",
                    code="NO_ACCESSIBLE_ACCOUNTS",
                    next_step="Sign in with a Google account granted access in Google Ads > Admin > Access and security.",
                )
            else:
                add(
                    "accessible_accounts",
                    "pass",
                    f"Google Ads API reachable; {len(accessible_ids)} directly accessible account(s).",
                )
                check_targets = targets or [accessible_ids[0]]
                query_headers = dict(headers)
                if manager:
                    query_headers["login-customer-id"] = manager
                for cid in check_targets:
                    if cid == manager:
                        continue
                    try:
                        info = ads_request(
                            "POST",
                            f"customers/{cid}/googleAds:search",
                            query_headers,
                            json={
                                "query": (
                                    "SELECT customer.id, customer.descriptive_name, "
                                    "customer.currency_code, customer.time_zone, "
                                    "customer.manager, customer.test_account "
                                    "FROM customer LIMIT 1"
                                )
                            },
                        )
                        rows = info.get("results", [])
                        if not rows:
                            add(
                                f"account_{cid}",
                                "warn",
                                f"Account {cid} returned no customer row.",
                                code="NO_DATA",
                            )
                            continue
                        cust = rows[0].get("customer", {}) or {}
                        currency = cust.get("currencyCode", "UNKNOWN")
                        tz = cust.get("timeZone", "UNKNOWN")
                        is_test = bool(cust.get("testAccount"))
                        add(
                            f"account_{cid}",
                            "pass",
                            f"{cust.get('descriptiveName', cid)} ({currency}, {tz}, test_account={is_test})",
                        )
                        perf = ads_request(
                            "POST",
                            f"customers/{cid}/googleAds:search",
                            query_headers,
                            json={
                                "query": (
                                    "SELECT metrics.impressions, metrics.cost_micros "
                                    "FROM customer WHERE segments.date DURING LAST_7_DAYS"
                                )
                            },
                        )
                        perf_rows = perf.get("results", [])
                        if not perf_rows:
                            add(
                                f"account_{cid}_data",
                                "warn",
                                (
                                    f"Account {cid} is accessible, but has no delivery data in the last 7 days"
                                    + (" (expected for Google Ads test accounts)." if is_test else ".")
                                ),
                                code="NO_DATA",
                                next_step="Use `--demo` to test full reports with synthetic data, or connect an active account.",
                            )
                        else:
                            add(f"account_{cid}_data", "pass", f"Account {cid} returned recent 7-day metrics.")
                    except ValueError as err:
                        add(
                            f"account_{cid}",
                            "fail",
                            str(err),
                            code="ACCOUNT_ACCESS_FAILED",
                            next_step="Verify --customer, --manager (MCC), and Google Ads user role.",
                        )
        except RefreshError as err:
            add(
                "live_api",
                "fail",
                f"OAuth token refresh failed: {err}",
                code="INVALID_CREDENTIALS",
                next_step="Re-run `google-ads-mcp-server connect` to obtain a fresh refresh token.",
            )
        except (ValueError, requests.RequestException) as err:
            add(
                "live_api",
                "fail",
                str(err),
                code="API_ERROR",
                next_step="Check network access, Cloud project approval, and Google Ads permissions.",
            )

    ready = all(item["status"] != "fail" for item in checks)
    return {
        "ready": ready,
        "mode": mode,
        "api_version": API_VERSION,
        "checks": checks,
    }


def serve(
    profile: Path,
    *,
    preset: str | None = None,
    modules: str | None = None,
    worker_port: int | None = None,
    allow_writes: bool = False,
) -> None:
    if allow_writes and worker_port is None:
        raise ValueError("--allow-writes requires --worker-port.")
    raw_config = json.loads(profile.read_text(encoding="utf-8"))
    if isinstance(raw_config, dict) and "encrypted" in raw_config:
        from agency.remote_store import decrypt_profile_payload

        config = decrypt_profile_payload(raw_config)
    else:
        config = raw_config
    targets = _profile_customers(config)
    if not targets:
        raise ValueError("Profile does not include any Google Ads customer ID.")
    legacy_manager = customer_id(config["manager"]) if config.get("manager") else ""
    raw_routes = config.get("customer_routes")
    if isinstance(raw_routes, dict) and raw_routes:
        customer_routes = {
            customer_id(str(k)): (customer_id(str(v)) if str(v or "").strip() else "")
            for k, v in raw_routes.items()
        }
        targets = list(dict.fromkeys([*targets, *customer_routes.keys()]))
    else:
        customer_routes = {cid: legacy_manager for cid in targets}

    unique_managers = sorted({mgr for mgr in customer_routes.values() if mgr})
    # Only set global login-customer-id when ALL accounts use the same single MCC (no direct accounts mixed in)
    manager = (
        unique_managers[0]
        if len(unique_managers) == 1 and all(bool(m) for m in customer_routes.values())
        else ""
    )
    login_customer_map_env = ",".join(f"{cid}:{mgr}" for cid, mgr in customer_routes.items())

    effective_preset = preset if preset is not None else config.get("preset")
    effective_modules = modules if modules is not None else config.get("modules")
    clean_preset, clean_modules = _validate_catalog_selection(effective_preset, effective_modules)
    # Per-instance remote worker profiles (`profile-<instance_id>.json`) share the tenant's
    # persistent `profile-files`, `profile-data`, and `profile-config` directories.
    dir_prefix = "profile" if profile.stem.startswith("profile-") else profile.stem
    if worker_port is not None:
        if not 1 <= worker_port <= 65535:
            raise ValueError("--worker-port must be between 1 and 65535.")
        from agency.remote_gateway import require_worker_key

        worker_key = require_worker_key()
        os.environ["MCP_REMOTE_FILES_DIR"] = str(profile.parent / (dir_prefix + "-files"))
        os.environ["GOOGLE_ADS_DEMO_MODE"] = "false"
        # Respect explicit preset/modules; default to 'full' when --allow-writes is set with no preset,
        # and auto-include 'operations' if --allow-writes is combined with a non-'full' preset.
        if allow_writes:
            if not clean_preset and not clean_modules:
                clean_preset = "full"
            elif clean_preset != "full":
                mod_items = [p.strip().lower() for p in (clean_modules or "").split(",") if p.strip()]
                if "operations" not in mod_items:
                    mod_items.append("operations")
                clean_modules = ",".join(mod_items)
    read_only = not allow_writes
    # Select the profile before importing modules that read configuration at import time.
    os.environ.pop("GOOGLE_ADS_CREDENTIALS_PATH", None)
    os.environ.pop("GOOGLE_ADS_TOKEN_PATH", None)
    os.environ.update(
        {
            "GOOGLE_ADS_MANAGED_PROFILE": str(profile),
            "GOOGLE_ADS_AUTH_TYPE": "oauth",
            "GOOGLE_ADS_API_VERSION": API_VERSION,
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID": manager,
            "GOOGLE_ADS_LOGIN_CUSTOMER_MAP": login_customer_map_env,
            "GOOGLE_ADS_DEVELOPER_TOKEN": "",
            "GOOGLE_ADS_DATA_DIR": str(profile.parent / (dir_prefix + "-data")),
            "GOOGLE_ADS_CONFIG_DIR": str(profile.parent / (dir_prefix + "-config")),
            "GOOGLE_ADS_PRESET": clean_preset or "",
            "GOOGLE_ADS_MODULES": clean_modules or "",
            "READ_ONLY_MODE": "true" if read_only else "false",
            "ALLOWED_CUSTOMER_IDS": ",".join(dict.fromkeys(filter(None, (*targets, *unique_managers)))),
        }
    )
    import google_ads_server
    from agency.modules import configure_tool_catalog

    managed_creds = Credentials.from_authorized_user_info(config["credentials"], [SCOPE])
    google_ads_server.apply_managed_profile(
        profile,
        managed_creds,
        manager=manager,
        api_version=API_VERSION,
    )
    configure_tool_catalog(
        google_ads_server.mcp,
        preset=clean_preset,
        modules=clean_modules,
        read_only=read_only,
    )
    if worker_port is None:
        google_ads_server.mcp.run(transport="stdio")
    else:
        import uvicorn

        from agency.remote_gateway import protect_worker

        uvicorn.run(
            protect_worker(google_ads_server.mcp.streamable_http_app(), worker_key),
            host="127.0.0.1",
            port=worker_port,
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=default_profile())
    commands = parser.add_subparsers(dest="operation", required=True)

    connect = commands.add_parser("connect", help="Authorize Google in your browser and register an MCP client")
    connect.add_argument("--oauth-client", help="Path to a Desktop OAuth client JSON")
    connect.add_argument("--customer", help="10-digit customer ID (or comma-separated list)")
    connect.add_argument("--manager", help="Manager ID (MCC) when accessing client accounts through MCC")
    connect.add_argument(
        "--all-mcc-clients",
        action="store_true",
        help="Discover and scope all enabled non-manager accounts under --manager",
    )
    connect.add_argument("--no-browser", action="store_true")
    connect.add_argument("--client", choices=SUPPORTED_CLIENTS, default="generic")
    connect.add_argument(
        "--preset",
        choices=SUPPORTED_PRESETS,
        default=None,
        help="Optional tool catalog preset (core, leads, ecommerce, agency, full; read-only enforced)",
    )
    connect.add_argument(
        "--modules",
        default=None,
        help="Optional comma-separated opt-in modules (e.g. measurement,search,pmax)",
    )

    register = commands.add_parser("register", help="Register an existing profile (or --demo) in an MCP client")
    register.add_argument("--client", choices=SUPPORTED_CLIENTS, required=True)
    register.add_argument("--demo", action="store_true", help="Register zero-credential --demo mode instead of a live profile")
    register.add_argument(
        "--preset",
        choices=SUPPORTED_PRESETS,
        default=None,
        help="Optional tool catalog preset (core, leads, ecommerce, agency, full; read-only enforced)",
    )
    register.add_argument(
        "--modules",
        default=None,
        help="Optional comma-separated opt-in modules (e.g. measurement,search,pmax)",
    )

    doctor = commands.add_parser("doctor", help="Run non-destructive preflight checks for credentials and API access")
    doctor.add_argument("--offline", action="store_true", help="Validate local files without calling Google Ads API")
    doctor.add_argument("--json", action="store_true", dest="as_json", help="Emit JSON output")

    serve_parser = commands.add_parser("serve", help="Start the MCP using the selected local profile")
    serve_parser.add_argument(
        "--preset",
        choices=SUPPORTED_PRESETS,
        default=None,
        help="Optional tool catalog preset overriding profile default (read-only enforced)",
    )
    serve_parser.add_argument(
        "--modules",
        default=None,
        help="Optional comma-separated opt-in modules overriding profile default",
    )
    serve_parser.add_argument("--worker-port", type=int, help="Start a private loopback HTTP worker")
    serve_parser.add_argument("--allow-writes", action="store_true", help="Enable guarded writes in a private worker")

    args = parser.parse_args(argv)
    profile = args.profile.expanduser().resolve()
    try:
        if args.operation == "serve":
            serve(
                profile, preset=args.preset, modules=args.modules,
                worker_port=args.worker_port, allow_writes=args.allow_writes,
            )
        elif args.operation == "doctor":
            report = run_doctor(profile, offline=args.offline)
            if args.as_json:
                print(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                for item in report["checks"]:
                    tag = item["status"].upper()
                    print(f"[{tag}] {item['check']}: {item['detail']}")
                    if item.get("next_step"):
                        print(f"       -> {item['next_step']}")
                print("\nStatus:", "READY" if report["ready"] else "ACTION REQUIRED")
            if not report["ready"]:
                sys.exit(1)
        else:
            is_demo_register = args.operation == "register" and getattr(args, "demo", False)
            if args.operation == "connect":
                authorize(args, profile)
            elif not is_demo_register and not profile.is_file():
                raise ValueError("Profile is missing. Run the connect command first (or use --demo).")
            register_client(
                args.client,
                None if is_demo_register else profile,
                demo=is_demo_register,
                preset=args.preset,
                modules=args.modules,
            )
            if args.client not in {"cli", "generic"}:
                print(f"Configured {args.client}. Start a new session or restart the client to load the MCP.")
    except (
        ValueError,
        OSError,
        KeyError,
        EOFError,
        AttributeError,
        TypeError,
        requests.RequestException,
        subprocess.CalledProcessError,
    ) as error:
        parser.exit(1, f"Setup failed: {error}\n")


if __name__ == "__main__":
    main()
