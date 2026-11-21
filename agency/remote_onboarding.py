"""Self-serve web OAuth onboarding portal and API for hosted remote Google Ads MCP.

Routes mounted alongside the OAuth-protected `/mcp` gateway:
- `GET  /connect`                 : HTML onboarding & settings page (with web sign-in button)
- `GET  /connect/ticket`          : Redeems a single-use browser login ticket emitted by `/mcp`
- `GET  /connect/login`           : Starts OIDC/OAuth PKCE browser sign-in against `MCP_REMOTE_ISSUER`
- `GET  /connect/oidc/callback`   : Completes browser sign-in, verifying token via the same `OAuthVerifier`
- `POST /connect/session`         : Exchanges a valid Bearer JWT for an HttpOnly portal session cookie
- `GET  /connect/status`          : JSON status of the authenticated user's Google Ads connection
- `GET  /connect/google/start`    : Starts Google Ads OAuth 2.0 + PKCE (`S256`) flow bound to `subject`
- `GET  /connect/google/callback` : Exchanges authorization code, discovers direct accounts & MCC routes
- `POST /connect/configure`       : Persists validated per-account `customer_routes`, preset & write policy
- `POST /connect/disconnect`      : Revokes Google token, wipes encrypted store & drains/stops worker
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .modules import PRESETS, resolve_modules
from .remote_store import DisconnectEpochMismatchError, DynamicWorkerPool, EncryptedProfileStore


GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URI = "https://oauth2.googleapis.com/revoke"
GOOGLE_ADS_SCOPE = "https://www.googleapis.com/auth/adwords"
SESSION_COOKIE_NAME = "mcp_portal_session"
OIDC_TX_COOKIE_NAME = "mcp_oidc_tx"
SESSION_TTL_SECONDS = 3600
_NO_STORE_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
}

_PRESET_DESCRIPTIONS: dict[str, str] = {
    "core": "core — 25 read tools (overview, GAQL, trends, reports, waste, alerts, profiles)",
    "leads": "leads — 54 read tools (core + diagnostics + measurement + search)",
    "ecommerce": "ecommerce — 47 read tools (core + diagnostics + measurement + pmax + shopping)",
    "agency": "agency — 71 read tools (core + diagnostics + measurement + search + pmax + mcc)",
    "full": "full — 74 read tools (all read modules; 142 tools when guarded writes are enabled)",
}


@dataclass(frozen=True)
class GoogleWebOAuthConfig:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str
    api_version: str = "v25"
    allow_user_writes: bool = False

    def __post_init__(self) -> None:
        if not self.client_id or not self.client_secret:
            raise ValueError("Google OAuth client_id and client_secret are required for self-serve mode.")
        parsed = urlsplit(self.redirect_uri)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Google OAuth redirect_uri must use HTTPS.")

    @classmethod
    def from_env(cls, *, resource_url: str) -> "GoogleWebOAuthConfig":
        client_id = os.environ.get("MCP_REMOTE_GOOGLE_CLIENT_ID", "").strip()
        client_secret = os.environ.get("MCP_REMOTE_GOOGLE_CLIENT_SECRET", "").strip()
        json_path = os.environ.get("MCP_REMOTE_GOOGLE_CLIENT_JSON", "").strip()
        if (not client_id or not client_secret) and json_path:
            raw = json.loads(Path(json_path).expanduser().read_text(encoding="utf-8-sig"))
            section = raw.get("web") or raw.get("installed") or {}
            client_id = client_id or str(section.get("client_id") or "").strip()
            client_secret = client_secret or str(section.get("client_secret") or "").strip()
        parsed = urlsplit(resource_url)
        redirect_uri = urlunsplit((parsed.scheme, parsed.netloc, "/connect/google/callback", "", ""))
        allow_user_writes = os.environ.get("MCP_REMOTE_ALLOW_USER_WRITES", "false").strip().lower() in {
            "1", "true", "yes", "on"
        }
        api_version = os.environ.get("GOOGLE_ADS_API_VERSION", "v25").strip() or "v25"
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            api_version=api_version,
            allow_user_writes=allow_user_writes,
        )


@dataclass(frozen=True)
class PortalOIDCConfig:
    """Optional browser OIDC/OAuth login config against the same `MCP_REMOTE_ISSUER`."""

    issuer: str
    client_id: str
    redirect_uri: str
    resource: str
    client_secret: str = field(default="", repr=False)
    authorization_endpoint: str | None = None
    token_endpoint: str | None = None
    scopes: str = "openid mcp:access"

    def __post_init__(self) -> None:
        for url in filter(None, (self.issuer, self.redirect_uri, self.authorization_endpoint, self.token_endpoint)):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("Portal OIDC URLs must use HTTPS.")
        if not self.client_id:
            raise ValueError("Portal OIDC client_id is required.")

    @classmethod
    def from_env(cls, *, issuer: str, resource_url: str) -> "PortalOIDCConfig | None":
        client_id = os.environ.get("MCP_REMOTE_PORTAL_CLIENT_ID", "").strip()
        if not client_id:
            return None
        parsed = urlsplit(resource_url)
        redirect_uri = urlunsplit((parsed.scheme, parsed.netloc, "/connect/oidc/callback", "", ""))
        return cls(
            issuer=issuer,
            client_id=client_id,
            client_secret=os.environ.get("MCP_REMOTE_PORTAL_CLIENT_SECRET", "").strip(),
            redirect_uri=redirect_uri,
            resource=resource_url,
            authorization_endpoint=os.environ.get("MCP_REMOTE_PORTAL_AUTH_URL", "").strip() or None,
            token_endpoint=os.environ.get("MCP_REMOTE_PORTAL_TOKEN_URL", "").strip() or None,
            scopes=os.environ.get("MCP_REMOTE_PORTAL_SCOPES", "openid mcp:access").strip() or "openid mcp:access",
        )


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _normalize_cid(raw: str) -> str:
    digits = "".join(ch for ch in str(raw or "").strip() if ch.isdigit())
    if len(digits) != 10:
        raise ValueError(f"Invalid 10-digit Google Ads customer ID: {raw!r}")
    return digits


def _merge_modules_with_operations(preset: str, modules: str | None, allow_writes: bool) -> tuple[str, str | None]:
    """Ensure `allow_writes=True` never leaves the user with 0 write tools."""
    if not allow_writes or preset == "full":
        return preset, modules
    items = [p.strip().lower() for p in (modules or "").split(",") if p.strip()]
    if "operations" not in items:
        items.append("operations")
    return preset, ",".join(items)


def _build_authorized_routes(discovered: dict[str, Any]) -> dict[str, list[str]]:
    """Return `customer_id -> list of allowed manager IDs` (`''` means direct access)."""
    routes: dict[str, list[str]] = {}
    for acct in discovered.get("direct_accounts", []):
        cid = str(acct.get("id") or "")
        if cid:
            routes.setdefault(cid, [])
            if "" not in routes[cid]:
                routes[cid].append("")
    for mgr_id, children in (discovered.get("mcc_children") or {}).items():
        mgr_clean = str(mgr_id)
        for child in children:
            child_id = str(child.get("id") or "")
            if child_id:
                routes.setdefault(child_id, [])
                if mgr_clean not in routes[child_id]:
                    routes[child_id].append(mgr_clean)
    return routes


class GoogleOAuthAdapter:
    """Async HTTP client wrapper for Google OAuth exchange, revocation, and account discovery."""

    def __init__(self, config: GoogleWebOAuthConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=25, follow_redirects=False)

    async def exchange_code(self, *, code: str, code_verifier: str) -> dict[str, Any]:
        async with self._client() as client:
            resp = await client.post(
                GOOGLE_TOKEN_URI,
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "code": code,
                    "code_verifier": code_verifier,
                    "grant_type": "authorization_code",
                    "redirect_uri": self.config.redirect_uri,
                },
            )
        if resp.status_code != 200:
            raise ValueError(f"Google token exchange failed (HTTP {resp.status_code}).")
        data = resp.json()
        refresh_token = data.get("refresh_token")
        access_token = data.get("access_token")
        if not refresh_token or not access_token:
            raise ValueError(
                "Google did not return a refresh_token. Revoke prior app access in your Google Account "
                "and reconnect granting offline access."
            )
        return {
            "token": access_token,
            "refresh_token": refresh_token,
            "token_uri": GOOGLE_TOKEN_URI,
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "scopes": [GOOGLE_ADS_SCOPE],
        }

    async def discover_accounts(self, access_token: str) -> dict[str, Any]:
        """Discover directly accessible Google Ads accounts and enabled child accounts under MCCs."""
        base = f"https://googleads.googleapis.com/{self.config.api_version}"
        headers = {"Authorization": f"Bearer {access_token}"}
        async with self._client() as client:
            resp = await client.get(f"{base}/customers:listAccessibleCustomers", headers=headers)
            if resp.status_code != 200:
                raise ValueError(
                    f"Unable to list accessible Google Ads accounts (HTTP {resp.status_code})."
                )
            resource_names = resp.json().get("resourceNames", [])
            direct_ids = [name.rsplit("/", 1)[-1] for name in resource_names if "/" in str(name)]

            accounts: list[dict[str, Any]] = []
            managers: dict[str, list[dict[str, Any]]] = {}
            for cid in direct_ids:
                info_resp = await client.post(
                    f"{base}/customers/{cid}/googleAds:search",
                    headers=headers,
                    json={
                        "query": (
                            "SELECT customer.id, customer.descriptive_name, "
                            "customer.currency_code, customer.manager "
                            "FROM customer LIMIT 1"
                        )
                    },
                )
                if info_resp.status_code != 200:
                    accounts.append({"id": cid, "name": cid, "currency_code": "UNKNOWN", "manager": False})
                    continue
                rows = info_resp.json().get("results", [])
                cust = (rows[0].get("customer") or {}) if rows else {}
                is_mgr = bool(cust.get("manager"))
                accounts.append(
                    {
                        "id": cid,
                        "name": cust.get("descriptiveName") or cid,
                        "currency_code": cust.get("currencyCode") or "UNKNOWN",
                        "manager": is_mgr,
                    }
                )
                if is_mgr:
                    child_resp = await client.post(
                        f"{base}/customers/{cid}/googleAds:search",
                        headers={**headers, "login-customer-id": cid},
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
                    if child_resp.status_code == 200:
                        children = []
                        for row in child_resp.json().get("results", []):
                            cc = row.get("customerClient") or {}
                            child_id = str(cc.get("id") or "")
                            if child_id:
                                children.append(
                                    {
                                        "id": child_id,
                                        "name": cc.get("descriptiveName") or child_id,
                                        "currency_code": cc.get("currencyCode") or "UNKNOWN",
                                        "manager_id": cid,
                                    }
                                )
                        managers[cid] = children
        return {"direct_accounts": accounts, "mcc_children": managers}

    async def revoke_token(self, refresh_token: str | None) -> bool:
        if not refresh_token:
            return False
        try:
            async with self._client() as client:
                resp = await client.post(GOOGLE_REVOKE_URI, params={"token": refresh_token})
            return resp.status_code == 200
        except httpx.RequestError:
            return False


class PortalOIDCAdapter:
    """Async OIDC discovery + PKCE code exchange for browser sign-in on `/connect`."""

    def __init__(self, config: PortalOIDCConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._transport = transport
        self._endpoints: tuple[str, str] | None = (
            (config.authorization_endpoint, config.token_endpoint)
            if config.authorization_endpoint and config.token_endpoint
            else None
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=15, follow_redirects=False)

    async def endpoints(self) -> tuple[str, str]:
        if self._endpoints is not None:
            return self._endpoints
        discovery_url = self.config.issuer.rstrip("/") + "/.well-known/openid-configuration"
        async with self._client() as client:
            resp = await client.get(discovery_url)
        if resp.status_code != 200:
            raise ValueError(f"OIDC discovery failed at {discovery_url} (HTTP {resp.status_code}).")
        data = resp.json()
        auth_url = str(data.get("authorization_endpoint") or "")
        token_url = str(data.get("token_endpoint") or "")
        if not auth_url.startswith("https://") or not token_url.startswith("https://"):
            raise ValueError("OIDC discovery returned invalid HTTPS endpoints.")
        self._endpoints = (auth_url, token_url)
        return self._endpoints

    async def exchange_code(self, *, code: str, code_verifier: str) -> str:
        _, token_url = await self.endpoints()
        form: dict[str, str] = {
            "grant_type": "authorization_code",
            "client_id": self.config.client_id,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": self.config.redirect_uri,
            "resource": self.config.resource,
        }
        if self.config.client_secret:
            form["client_secret"] = self.config.client_secret
        async with self._client() as client:
            resp = await client.post(token_url, data=form)
        if resp.status_code != 200:
            raise ValueError(f"Portal OIDC token exchange failed (HTTP {resp.status_code}).")
        token = resp.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError("Portal OIDC token response did not include an access_token.")
        return token


def _render_portal_html(
    *,
    resource_url: str,
    subject: str | None,
    tenant_summary: dict[str, Any] | None,
    allow_user_writes: bool,
    oidc_enabled: bool = False,
    pending_grant: dict[str, Any] | None = None,
    grant_token: str | None = None,
    flash_message: str | None = None,
    error_message: str | None = None,
) -> str:
    default_preset = (
        tenant_summary.get("preset")
        if tenant_summary and tenant_summary.get("preset")
        else ("full" if allow_user_writes else "core")
    )
    presets_html = "".join(
        f'<option value="{p}" {"selected" if default_preset == p else ""}>'
        f'{html.escape(_PRESET_DESCRIPTIONS.get(p, p))}</option>'
        for p in sorted(PRESETS)
    )
    flash = f'<div class="banner ok">{html.escape(flash_message)}</div>' if flash_message else ""
    err = f'<div class="banner err">{html.escape(error_message)}</div>' if error_message else ""

    if not subject:
        login_cta = (
            '<p><a class="btn" href="/connect/login">Sign in to connect Google Ads</a></p>'
            if oidc_enabled
            else (
                "<p>Open the one-click <code>connect_url</code> returned by your MCP client "
                "(Claude Desktop / Codex / Antigravity) to sign in automatically in your browser.</p>"
            )
        )
        body = f"""
        <p>Connect or manage your Google Ads account for <code>{html.escape(resource_url)}</code>.</p>
        {login_cta}
        """
    elif pending_grant and grant_token:
        accounts_list: list[str] = []
        for acct in pending_grant.get("direct_accounts", []):
            if acct.get("manager"):
                continue
            val = f'{acct["id"]}:'
            accounts_list.append(
                f'<label class="acct"><input type="checkbox" name="route_entries" value="{html.escape(val)}" checked> '
                f'<strong>{html.escape(acct["id"])}</strong> — {html.escape(str(acct.get("name", "")))} '
                f'({html.escape(str(acct.get("currency_code", "UNKNOWN")))}) · Direct account</label>'
            )
        for mgr_id, children in (pending_grant.get("mcc_children") or {}).items():
            for child in children:
                val = f'{child["id"]}:{mgr_id}'
                accounts_list.append(
                    f'<label class="acct"><input type="checkbox" name="route_entries" value="{html.escape(val)}" checked> '
                    f'<strong>{html.escape(child["id"])}</strong> — {html.escape(str(child.get("name", "")))} '
                    f'({html.escape(str(child.get("currency_code", "UNKNOWN")))}) · via MCC {html.escape(mgr_id)}</label>'
                )
        write_opt = (
            '<label class="acct"><input type="checkbox" name="allow_writes" value="true"> '
            "Enable guarded write operations (opt-in; automatically adds the <code>operations</code> module — 68 guarded tools)</label>"
            if allow_user_writes
            else "<p class='muted'>Mode: <strong>Read-only enforced</strong> by host policy.</p>"
        )
        body = f"""
        <h2>Select Google Ads Accounts &amp; Tool Preset</h2>
        <p class="muted">Each account preserves its own access route (direct vs. specific MCC), so mixed direct and multi-MCC accounts work without header conflicts.</p>
        <form method="post" action="/connect/configure">
          <input type="hidden" name="grant_token" value="{html.escape(grant_token)}">
          <div class="box">{''.join(accounts_list) or '<p>No non-manager accounts returned.</p>'}</div>
          <label>Tool Catalog Preset:<br><select name="preset">{presets_html}</select></label><br><br>
          {write_opt}
          <button type="submit">Save &amp; Activate MCP Connection</button>
        </form>
        """
    elif tenant_summary and tenant_summary.get("connected"):
        routes = tenant_summary.get("customer_routes") or {}
        routes_html = "".join(
            f"<li><code>{html.escape(cid)}</code> — "
            f"{'via MCC <code>' + html.escape(mgr) + '</code>' if mgr else 'Direct account'}</li>"
            for cid, mgr in routes.items()
        )
        mode = "Read + Guarded Writes" if tenant_summary.get("allow_writes") else "Read-only"
        eff_modules = tenant_summary.get("modules") or "preset default"
        write_toggle = (
            f'<label class="acct"><input type="checkbox" name="allow_writes" value="true" '
            f'{"checked" if tenant_summary.get("allow_writes") else ""}> '
            "Enable guarded write operations (adds <code>operations</code> module)</label>"
            if allow_user_writes
            else ""
        )
        body = f"""
        <div class="box">
          <p><strong>Status:</strong> Connected</p>
          <p><strong>MCP Endpoint URL:</strong> <code>{html.escape(resource_url)}</code></p>
          <p><strong>Authorized Accounts &amp; Routes:</strong></p>
          <ul>{routes_html}</ul>
          <p><strong>Active Preset:</strong> <code>{html.escape(str(tenant_summary.get('preset', 'core')))}</code>
          (extra modules: <code>{html.escape(str(eff_modules))}</code> · {html.escape(mode)})</p>
        </div>
        <h3>Update Preset &amp; Mode</h3>
        <form method="post" action="/connect/configure">
          <label>Preset:<br><select name="preset">{presets_html}</select></label><br><br>
          {write_toggle}
          <button type="submit">Update Settings</button>
        </form>
        <h3>Add/Change Accounts or Revoke</h3>
        <p><a class="btn" href="/connect/google/start">Reconnect Google Ads Account</a></p>
        <form method="post" action="/connect/disconnect">
          <button type="submit" class="danger">Disconnect &amp; Revoke Google Ads Access</button>
        </form>
        """
    else:
        body = f"""
        <p>Signed in as <code>{html.escape(subject)}</code>. Connect your Google Ads account to activate your
        private MCP endpoint at <code>{html.escape(resource_url)}</code>.</p>
        <p><a class="btn" href="/connect/google/start">Connect Google Ads Account</a></p>
        <p class="muted">Credentials are encrypted at rest with Fernet. Read-only mode is enabled by default.</p>
        """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Google Ads MCP — Connect</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 760px; margin: 40px auto; padding: 0 20px; color: #1f2937; }}
    .box {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; margin: 16px 0; }}
    .banner {{ padding: 12px 16px; border-radius: 8px; margin: 12px 0; }}
    .ok {{ background: #ecfdf5; border: 1px solid #10b981; color: #065f46; }}
    .err {{ background: #fef2f2; border: 1px solid #ef4444; color: #991b1b; }}
    .acct {{ display: block; margin: 8px 0; }}
    .muted {{ color: #64748b; font-size: 0.92rem; }}
    select {{ max-width: 100%; padding: 6px 8px; margin-top: 4px; }}
    button, .btn {{ display: inline-block; background: #1a73e8; color: #fff; border: 0; border-radius: 6px; padding: 10px 16px; text-decoration: none; cursor: pointer; font-weight: 600; }}
    button.danger {{ background: #dc2626; margin-top: 8px; }}
  </style>
</head>
<body>
  <h1>Google Ads MCP — Self-Serve Connection</h1>
  {flash}{err}
  {body}
</body>
</html>"""


def _wants_json(request: Request) -> bool:
    accept = request.headers.get("accept", "").lower()
    ctype = request.headers.get("content-type", "").lower()
    return "application/json" in accept or "application/json" in ctype


def create_onboarding_routes(
    *,
    resource_url: str,
    verifier: Any,
    store: EncryptedProfileStore,
    pool: DynamicWorkerPool,
    master_key: bytes,
    google_oauth: GoogleWebOAuthConfig,
    oauth_adapter: GoogleOAuthAdapter | None = None,
    portal_oidc: PortalOIDCConfig | None = None,
    portal_oidc_adapter: PortalOIDCAdapter | None = None,
) -> list[Route]:
    """Build Starlette routes for browser login, Google Ads OAuth onboarding, and revocation."""
    fernet = Fernet(master_key)
    adapter = oauth_adapter if oauth_adapter is not None else GoogleOAuthAdapter(google_oauth)
    oidc_adapter = (
        portal_oidc_adapter
        if portal_oidc_adapter is not None
        else (PortalOIDCAdapter(portal_oidc) if portal_oidc is not None else None)
    )

    def encode_session(subject: str) -> str:
        payload = {"sub": subject, "exp": int(time.time()) + SESSION_TTL_SECONDS}
        return fernet.encrypt(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")

    def decode_session(cookie_value: str | None) -> str | None:
        if not cookie_value:
            return None
        try:
            raw = fernet.decrypt(cookie_value.encode("ascii"), ttl=SESSION_TTL_SECONDS)
            data = json.loads(raw.decode("utf-8"))
        except (InvalidToken, ValueError, TypeError):
            return None
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        sub = data.get("sub")
        return str(sub) if isinstance(sub, str) and sub else None

    def set_session_cookie(resp: Response, subject: str) -> Response:
        resp.set_cookie(
            SESSION_COOKIE_NAME,
            encode_session(subject),
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/connect",
        )
        return resp

    async def resolve_subject(request: Request) -> str | None:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and token:
            return await verifier.subject(token)
        return decode_session(request.cookies.get(SESSION_COOKIE_NAME))

    # ---------- Browser login entrypoints ----------

    async def redeem_ticket(request: Request) -> Response:
        """One-click browser login from a single-use ticket issued by `/mcp`."""
        ticket = request.query_params.get("t", "").strip()
        if not ticket:
            return JSONResponse({"error": "missing_ticket"}, status_code=400, headers=_NO_STORE_HEADERS)
        record = store.pop_oauth_pending(ticket)
        if record is None or record.get("kind") != "portal_ticket":
            if _wants_json(request):
                return JSONResponse(
                    {"error": "invalid_or_expired_ticket"},
                    status_code=400,
                    headers=_NO_STORE_HEADERS,
                )
            return HTMLResponse(
                _render_portal_html(
                    resource_url=resource_url,
                    subject=None,
                    tenant_summary=None,
                    allow_user_writes=google_oauth.allow_user_writes,
                    oidc_enabled=oidc_adapter is not None,
                    error_message="This sign-in link has expired or was already used. Retry from your MCP client or sign in below.",
                ),
                status_code=400,
                headers=_NO_STORE_HEADERS,
            )
        subject = str(record["_subject"])
        resp = RedirectResponse("/connect", status_code=302, headers=_NO_STORE_HEADERS)
        return set_session_cookie(resp, subject)

    async def oidc_login(request: Request) -> Response:
        """Start OIDC/OAuth 2.0 PKCE browser login bound to the initiating browser (RFC 9700)."""
        if oidc_adapter is None or portal_oidc is None:
            return JSONResponse({"error": "portal_oidc_not_configured"}, status_code=404)
        try:
            auth_url, _ = await oidc_adapter.endpoints()
        except ValueError as exc:
            return JSONResponse({"error": "oidc_discovery_failed", "detail": str(exc)}, status_code=502)
        state = secrets.token_urlsafe(32)
        tx_nonce = secrets.token_urlsafe(24)
        code_verifier = secrets.token_urlsafe(64)
        store.save_oauth_pending(
            state,
            "__oidc_login__",
            {"kind": "oidc_login", "code_verifier": code_verifier, "tx_nonce": tx_nonce},
            ttl_seconds=600,
        )
        params = urlencode(
            {
                "client_id": portal_oidc.client_id,
                "redirect_uri": portal_oidc.redirect_uri,
                "response_type": "code",
                "scope": portal_oidc.scopes,
                "resource": portal_oidc.resource,
                "state": state,
                "code_challenge": _pkce_challenge(code_verifier),
                "code_challenge_method": "S256",
            }
        )
        authorize_url = f"{auth_url}?{params}"
        tx_cookie = fernet.encrypt(
            json.dumps({"state": state, "tx_nonce": tx_nonce}, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        resp: Response = (
            JSONResponse({"authorize_url": authorize_url, "state": state}, headers=_NO_STORE_HEADERS)
            if _wants_json(request)
            else RedirectResponse(authorize_url, status_code=302, headers=_NO_STORE_HEADERS)
        )
        resp.set_cookie(
            OIDC_TX_COOKIE_NAME,
            tx_cookie,
            max_age=600,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/connect/oidc/callback",
        )
        return resp

    async def oidc_callback(request: Request) -> Response:
        """Complete OIDC browser sign-in, enforcing browser binding cookie and verifying token."""
        if oidc_adapter is None:
            return JSONResponse({"error": "portal_oidc_not_configured"}, status_code=404)
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        if not code or not state:
            return JSONResponse({"error": "missing_code_or_state"}, status_code=400, headers=_NO_STORE_HEADERS)

        # Verify initiating-browser binding cookie BEFORE consuming pending state (blocks login-CSRF)
        raw_tx_cookie = request.cookies.get(OIDC_TX_COOKIE_NAME)
        tx_data: dict[str, Any] | None = None
        if raw_tx_cookie:
            try:
                tx_data = json.loads(fernet.decrypt(raw_tx_cookie.encode("ascii"), ttl=600).decode("utf-8"))
            except (InvalidToken, ValueError, TypeError):
                tx_data = None
        if not isinstance(tx_data, dict) or tx_data.get("state") != state or not tx_data.get("tx_nonce"):
            resp_err = JSONResponse(
                {
                    "error": "oidc_browser_mismatch",
                    "detail": "Sign-in callback must complete in the same browser that started login.",
                },
                status_code=400,
                headers=_NO_STORE_HEADERS,
            )
            resp_err.delete_cookie(OIDC_TX_COOKIE_NAME, path="/connect/oidc/callback")
            return resp_err

        pending = store.pop_oauth_pending(state, expected_subject="__oidc_login__")
        if (
            pending is None
            or pending.get("kind") != "oidc_login"
            or pending.get("tx_nonce") != tx_data.get("tx_nonce")
        ):
            resp_err = JSONResponse({"error": "invalid_or_expired_state"}, status_code=400, headers=_NO_STORE_HEADERS)
            resp_err.delete_cookie(OIDC_TX_COOKIE_NAME, path="/connect/oidc/callback")
            return resp_err
        try:
            access_token = await oidc_adapter.exchange_code(
                code=code, code_verifier=str(pending["code_verifier"])
            )
        except ValueError as exc:
            resp_err = JSONResponse(
                {"error": "oidc_exchange_failed", "detail": str(exc)},
                status_code=400,
                headers=_NO_STORE_HEADERS,
            )
            resp_err.delete_cookie(OIDC_TX_COOKIE_NAME, path="/connect/oidc/callback")
            return resp_err
        subject = await verifier.subject(access_token)
        if subject is None:
            resp_err = JSONResponse({"error": "invalid_oidc_access_token"}, status_code=401, headers=_NO_STORE_HEADERS)
            resp_err.delete_cookie(OIDC_TX_COOKIE_NAME, path="/connect/oidc/callback")
            return resp_err
        resp = RedirectResponse("/connect", status_code=302, headers=_NO_STORE_HEADERS)
        resp.delete_cookie(OIDC_TX_COOKIE_NAME, path="/connect/oidc/callback")
        return set_session_cookie(resp, subject)

    async def create_session(request: Request) -> Response:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        subject = await verifier.subject(token) if scheme.lower() == "bearer" and token else None
        if subject is None:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        resp = JSONResponse({"ok": True, "subject": subject, "connect_url": "/connect"})
        return set_session_cookie(resp, subject)

    # ---------- Portal pages & Google Ads OAuth ----------

    def _effective_summary(tenant_obj: Any) -> dict[str, Any] | None:
        if tenant_obj is None:
            return None
        info = tenant_obj.public_summary()
        eff_writes = bool(info.get("allow_writes") and google_oauth.allow_user_writes)
        info["allow_writes"] = eff_writes
        info["read_only_mode"] = not eff_writes
        return info

    def _disconnect_in_progress_response(request: Request, subject: str) -> Response:
        draining = pool.is_draining(subject)
        headers = {**_NO_STORE_HEADERS, "Retry-After": "2"}
        detail = (
            "Previous disconnection and Google token revocation are still in progress. "
            "Wait until disconnecting completes before reconnecting."
        )
        if _wants_json(request):
            return JSONResponse(
                {
                    "error": "disconnect_in_progress",
                    "detail": detail,
                    "disconnecting": True,
                    "worker_draining": draining,
                    "revocation_pending": True,
                },
                status_code=409,
                headers=headers,
            )
        return HTMLResponse(
            _render_portal_html(
                resource_url=resource_url,
                subject=subject,
                tenant_summary=None,
                allow_user_writes=google_oauth.allow_user_writes,
                oidc_enabled=oidc_adapter is not None,
                error_message=detail,
            ),
            status_code=409,
            headers=headers,
        )

    def _oauth_superseded_or_disconnecting_response(request: Request, subject: str) -> Response:
        if pool.is_disconnecting(subject):
            return _disconnect_in_progress_response(request, subject)
        detail = (
            "A disconnection occurred while this Google OAuth authorization was in progress. "
            "Please start a fresh connection."
        )
        if _wants_json(request):
            return JSONResponse(
                {
                    "error": "oauth_flow_superseded_by_disconnect",
                    "detail": detail,
                    "disconnecting": False,
                },
                status_code=409,
                headers=_NO_STORE_HEADERS,
            )
        return HTMLResponse(
            _render_portal_html(
                resource_url=resource_url,
                subject=subject,
                tenant_summary=None,
                allow_user_writes=google_oauth.allow_user_writes,
                oidc_enabled=oidc_adapter is not None,
                error_message=detail,
            ),
            status_code=409,
            headers=_NO_STORE_HEADERS,
        )

    async def connect_page(request: Request) -> Response:
        subject = await resolve_subject(request)
        tenant = store.get_tenant(subject) if subject else None
        summary = _effective_summary(tenant)
        draining = pool.is_draining(subject) if subject else False
        revoking = pool.is_disconnecting(subject) if subject else False
        disconnecting = summary is None and revoking
        if _wants_json(request):
            if not subject:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return JSONResponse(
                {
                    "resource_url": resource_url,
                    "connected": bool(summary),
                    "disconnecting": disconnecting,
                    "worker_draining": draining,
                    "revocation_pending": revoking,
                    "profile": summary,
                    "allow_user_writes": google_oauth.allow_user_writes,
                    "supported_presets": sorted(PRESETS),
                    "preset_descriptions": _PRESET_DESCRIPTIONS,
                },
                headers=_NO_STORE_HEADERS,
            )
        return HTMLResponse(
            _render_portal_html(
                resource_url=resource_url,
                subject=subject,
                tenant_summary=summary,
                allow_user_writes=google_oauth.allow_user_writes,
                oidc_enabled=oidc_adapter is not None,
                flash_message=(
                    "Disconnection and Google token revocation are still finishing. "
                    "Please refresh in a moment before reconnecting."
                    if disconnecting
                    else None
                ),
            ),
            headers=_NO_STORE_HEADERS,
        )

    async def connect_status(request: Request) -> Response:
        subject = await resolve_subject(request)
        if not subject:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        tenant = store.get_tenant(subject)
        draining = pool.is_draining(subject)
        revoking = pool.is_disconnecting(subject)
        return JSONResponse(
            {
                "resource_url": resource_url,
                "connected": tenant is not None,
                "disconnecting": (tenant is None and revoking),
                "worker_draining": draining,
                "revocation_pending": revoking,
                "profile": _effective_summary(tenant),
                "allow_user_writes": google_oauth.allow_user_writes,
                "supported_presets": sorted(PRESETS),
                "preset_descriptions": _PRESET_DESCRIPTIONS,
            },
            headers=_NO_STORE_HEADERS,
        )

    async def google_start(request: Request) -> Response:
        subject = await resolve_subject(request)
        if not subject:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if pool.is_disconnecting(subject):
            return _disconnect_in_progress_response(request, subject)
        start_epoch = store.get_disconnect_epoch(subject)
        state = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        try:
            store.save_oauth_pending(
                state,
                subject,
                {
                    "kind": "oauth_start",
                    "code_verifier": code_verifier,
                    "disconnect_epoch": start_epoch,
                },
                ttl_seconds=600,
                expected_disconnect_epoch=start_epoch,
            )
        except DisconnectEpochMismatchError:
            return _oauth_superseded_or_disconnecting_response(request, subject)
        params = urlencode(
            {
                "client_id": google_oauth.client_id,
                "redirect_uri": google_oauth.redirect_uri,
                "response_type": "code",
                "scope": GOOGLE_ADS_SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "code_challenge": _pkce_challenge(code_verifier),
                "code_challenge_method": "S256",
            }
        )
        authorize_url = f"{GOOGLE_AUTH_URI}?{params}"
        if _wants_json(request):
            return JSONResponse({"authorize_url": authorize_url, "state": state})
        return RedirectResponse(authorize_url, status_code=302)

    async def google_callback(request: Request) -> Response:
        subject = await resolve_subject(request)
        if not subject:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if pool.is_disconnecting(subject):
            return _disconnect_in_progress_response(request, subject)
        error_param = request.query_params.get("error")
        if error_param:
            return HTMLResponse(
                _render_portal_html(
                    resource_url=resource_url,
                    subject=subject,
                    tenant_summary=None,
                    allow_user_writes=google_oauth.allow_user_writes,
                    oidc_enabled=oidc_adapter is not None,
                    error_message=f"Google authorization cancelled or denied: {error_param}",
                ),
                status_code=400,
            )
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        if not code or not state:
            return JSONResponse({"error": "missing_code_or_state"}, status_code=400)

        pending = store.pop_oauth_pending(state, expected_subject=subject)
        if pending is None or pending.get("kind") != "oauth_start":
            return JSONResponse({"error": "invalid_or_expired_state"}, status_code=400)

        start_epoch = (
            int(pending["disconnect_epoch"])
            if "disconnect_epoch" in pending
            else store.get_disconnect_epoch(subject)
        )
        if pool.is_disconnecting(subject) or store.get_disconnect_epoch(subject) != start_epoch:
            return _oauth_superseded_or_disconnecting_response(request, subject)

        try:
            credentials = await adapter.exchange_code(
                code=code,
                code_verifier=str(pending["code_verifier"]),
            )
            discovered = await adapter.discover_accounts(str(credentials["token"]))
        except ValueError as exc:
            if _wants_json(request):
                return JSONResponse({"error": "oauth_exchange_failed", "detail": str(exc)}, status_code=400)
            return HTMLResponse(
                _render_portal_html(
                    resource_url=resource_url,
                    subject=subject,
                    tenant_summary=None,
                    allow_user_writes=google_oauth.allow_user_writes,
                    oidc_enabled=oidc_adapter is not None,
                    error_message=str(exc),
                ),
                status_code=400,
            )

        # Re-check disconnect/epoch AFTER async Google calls: if a disconnect started or
        # finished while the code was being exchanged, NEVER emit a grant_token!
        if pool.is_disconnecting(subject) or store.get_disconnect_epoch(subject) != start_epoch:
            return _oauth_superseded_or_disconnecting_response(request, subject)

        authorized_routes = _build_authorized_routes(discovered)
        grant_token = secrets.token_urlsafe(32)
        try:
            store.save_oauth_pending(
                grant_token,
                subject,
                {
                    "kind": "grant",
                    "disconnect_epoch": start_epoch,
                    "credentials": credentials,
                    "direct_accounts": discovered["direct_accounts"],
                    "mcc_children": discovered["mcc_children"],
                    "authorized_routes": authorized_routes,
                },
                ttl_seconds=600,
                expected_disconnect_epoch=start_epoch,
            )
        except DisconnectEpochMismatchError:
            return _oauth_superseded_or_disconnecting_response(request, subject)
        if _wants_json(request):
            return JSONResponse(
                {
                    "grant_token": grant_token,
                    "direct_accounts": discovered["direct_accounts"],
                    "mcc_children": discovered["mcc_children"],
                    "authorized_routes": authorized_routes,
                    "supported_presets": sorted(PRESETS),
                    "preset_descriptions": _PRESET_DESCRIPTIONS,
                }
            )
        return HTMLResponse(
            _render_portal_html(
                resource_url=resource_url,
                subject=subject,
                tenant_summary=None,
                allow_user_writes=google_oauth.allow_user_writes,
                oidc_enabled=oidc_adapter is not None,
                pending_grant=discovered,
                grant_token=grant_token,
                flash_message="Google account authorized. Select the Google Ads account(s) and preset to activate.",
            )
        )

    async def configure(request: Request) -> Response:
        subject = await resolve_subject(request)
        if not subject:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if pool.is_disconnecting(subject):
            return _disconnect_in_progress_response(request, subject)

        ctype = request.headers.get("content-type", "").lower()
        if "application/json" in ctype:
            try:
                data = await request.json()
            except Exception:
                return JSONResponse({"error": "invalid_json"}, status_code=400)
            grant_token = data.get("grant_token")
            raw_customers = data.get("customers")
            raw_customer_routes = data.get("customer_routes")
            raw_manager = data.get("manager")
            raw_preset = data.get("preset")
            raw_modules = data.get("modules")
            raw_allow_writes = data.get("allow_writes")
            raw_route_entries = None
        else:
            form = await request.form()
            grant_token = form.get("grant_token")
            raw_customers = form.getlist("customers") or None
            raw_route_entries = form.getlist("route_entries") or None
            raw_customer_routes = None
            raw_manager = form.get("manager")
            raw_preset = form.get("preset")
            raw_modules = form.get("modules")
            raw_allow_writes = form.get("allow_writes")

        # Parse HTML checkbox `route_entries` (`cid:` or `cid:mcc_id`) into `customer_routes`
        if raw_route_entries:
            parsed_routes: dict[str, str] = {}
            for entry in raw_route_entries:
                text = str(entry).strip()
                if ":" in text:
                    cid_part, mgr_part = text.split(":", 1)
                    parsed_routes[cid_part.strip()] = mgr_part.strip()
                else:
                    parsed_routes[text] = ""
            raw_customer_routes = parsed_routes

        allow_writes = (
            raw_allow_writes
            if isinstance(raw_allow_writes, bool)
            else str(raw_allow_writes or "").strip().lower() in {"1", "true", "yes", "on"}
        )
        if allow_writes and not google_oauth.allow_user_writes:
            return JSONResponse(
                {
                    "error": "writes_disabled_by_host",
                    "detail": "This remote deployment enforces read-only mode (MCP_REMOTE_ALLOW_USER_WRITES=false).",
                },
                status_code=403,
            )

        # Align preset & operations module when allow_writes is enabled!
        if raw_preset is None or not str(raw_preset).strip():
            preset = "full" if allow_writes else "core"
        else:
            preset = str(raw_preset).strip().lower()
        modules = str(raw_modules).strip() if raw_modules else None
        preset, modules = _merge_modules_with_operations(preset, modules, allow_writes)
        try:
            resolve_modules(preset=preset, modules=modules, read_only=not allow_writes)
        except ValueError as exc:
            return JSONResponse({"error": "invalid_catalog_selection", "detail": str(exc)}, status_code=400)

        explicit_manager: str | None = None
        if raw_manager is not None and str(raw_manager).strip():
            try:
                explicit_manager = _normalize_cid(str(raw_manager))
            except ValueError as exc:
                return JSONResponse({"error": "invalid_manager_id", "detail": str(exc)}, status_code=400)

        if grant_token:
            grant = store.pop_oauth_pending(str(grant_token), expected_subject=subject)
            if grant is None or grant.get("kind") != "grant":
                return JSONResponse({"error": "invalid_or_expired_grant"}, status_code=400)
            grant_epoch = (
                int(grant["disconnect_epoch"])
                if "disconnect_epoch" in grant
                else store.get_disconnect_epoch(subject)
            )
            if pool.is_disconnecting(subject) or store.get_disconnect_epoch(subject) != grant_epoch:
                return _oauth_superseded_or_disconnecting_response(request, subject)

            authorized_routes: dict[str, list[str]] = grant.get("authorized_routes") or _build_authorized_routes(grant)
            selected_routes: dict[str, str] = {}

            if isinstance(raw_customer_routes, dict) and raw_customer_routes:
                for raw_cid, raw_mgr in raw_customer_routes.items():
                    try:
                        cid = _normalize_cid(str(raw_cid))
                        mgr = _normalize_cid(str(raw_mgr)) if str(raw_mgr or "").strip() else ""
                    except ValueError as exc:
                        return JSONResponse({"error": "invalid_customer_id", "detail": str(exc)}, status_code=400)
                    allowed_mgrs = authorized_routes.get(cid)
                    if allowed_mgrs is None or mgr not in allowed_mgrs:
                        return JSONResponse(
                            {
                                "error": "customer_route_not_authorized",
                                "detail": f"Route ({cid}, manager={mgr or 'direct'}) is not in authorized Google grant.",
                            },
                            status_code=403,
                        )
                    selected_routes[cid] = mgr
            elif raw_customers:
                for raw_cid in raw_customers:
                    try:
                        cid = _normalize_cid(str(raw_cid))
                    except ValueError as exc:
                        return JSONResponse({"error": "invalid_customer_id", "detail": str(exc)}, status_code=400)
                    allowed_mgrs = authorized_routes.get(cid)
                    if allowed_mgrs is None:
                        return JSONResponse(
                            {
                                "error": "customer_not_authorized",
                                "detail": f"Account {cid} is not present in authorized Google grant.",
                            },
                            status_code=403,
                        )
                    if explicit_manager is not None:
                        # Prefer explicit manager when valid for this account; allow direct if this account is direct
                        if explicit_manager in allowed_mgrs:
                            selected_routes[cid] = explicit_manager
                        elif "" in allowed_mgrs:
                            selected_routes[cid] = ""
                        else:
                            return JSONResponse(
                                {
                                    "error": "manager_not_authorized",
                                    "detail": f"Manager {explicit_manager} is not authorized for account {cid}.",
                                },
                                status_code=403,
                            )
                    else:
                        # Auto-pick validated route: direct if direct, else discovered MCC manager
                        selected_routes[cid] = "" if "" in allowed_mgrs else allowed_mgrs[0]
            else:
                return JSONResponse({"error": "customers_required"}, status_code=400)

            if explicit_manager is not None:
                all_authorized_managers = {
                    mgr for mgrs in authorized_routes.values() for mgr in mgrs if mgr
                }
                if explicit_manager not in all_authorized_managers and explicit_manager not in authorized_routes:
                    return JSONResponse(
                        {
                            "error": "manager_not_authorized",
                            "detail": f"Manager {explicit_manager} is not present in authorized Google grant.",
                        },
                        status_code=403,
                    )

            try:
                tenant = store.upsert_tenant(
                    subject=subject,
                    credentials=grant["credentials"],
                    customers=list(selected_routes.keys()),
                    customer_routes=selected_routes,
                    preset=preset,
                    modules=modules,
                    allow_writes=allow_writes,
                    expected_disconnect_epoch=grant_epoch,
                )
            except DisconnectEpochMismatchError:
                return _oauth_superseded_or_disconnecting_response(request, subject)
        else:
            existing = store.get_tenant(subject)
            if existing is None:
                return JSONResponse({"error": "not_connected"}, status_code=400)
            existing_routes = existing.effective_routes()

            # Reject ANY attempt to inject an unvalidated manager or unvalidated customer route without re-authorizing!
            if explicit_manager is not None and explicit_manager not in set(existing_routes.values()):
                return JSONResponse(
                    {
                        "error": "reconnect_required_for_route_change",
                        "detail": "Re-authorize via /connect/google/start to add or change MCC managers.",
                    },
                    status_code=403,
                )

            updated_routes: dict[str, str] | None = None
            if isinstance(raw_customer_routes, dict) and raw_customer_routes:
                updated_routes = {}
                for raw_cid, raw_mgr in raw_customer_routes.items():
                    try:
                        cid = _normalize_cid(str(raw_cid))
                        mgr = _normalize_cid(str(raw_mgr)) if str(raw_mgr or "").strip() else ""
                    except ValueError as exc:
                        return JSONResponse({"error": "invalid_customer_id", "detail": str(exc)}, status_code=400)
                    if cid not in existing_routes or existing_routes[cid] != mgr:
                        return JSONResponse(
                            {
                                "error": "reconnect_required_for_route_change",
                                "detail": f"Re-authorize via /connect/google/start to modify route for {cid}.",
                            },
                            status_code=403,
                        )
                    updated_routes[cid] = mgr
            elif raw_customers:
                updated_routes = {}
                for raw_cid in raw_customers:
                    try:
                        cid = _normalize_cid(str(raw_cid))
                    except ValueError as exc:
                        return JSONResponse({"error": "invalid_customer_id", "detail": str(exc)}, status_code=400)
                    if cid not in existing_routes:
                        return JSONResponse(
                            {
                                "error": "reconnect_required_for_new_customers",
                                "detail": "Re-authorize via /connect/google/start to add new Google Ads accounts.",
                            },
                            status_code=403,
                        )
                    updated_routes[cid] = existing_routes[cid]

            effective_allow_writes = allow_writes if raw_allow_writes is not None else existing.allow_writes
            effective_preset, effective_modules = _merge_modules_with_operations(
                preset,
                modules if raw_modules is not None else existing.modules,
                effective_allow_writes,
            )
            tenant = store.update_preferences(
                subject,
                customers=list(updated_routes.keys()) if updated_routes is not None else None,
                customer_routes=updated_routes,
                preset=effective_preset,
                modules=effective_modules,
                allow_writes=effective_allow_writes,
            )

        # Drain & stop worker so next MCP request boots cleanly with updated settings
        await pool.stop_worker(subject)
        summary = tenant.public_summary()
        if _wants_json(request):
            return JSONResponse({"ok": True, "resource_url": resource_url, "profile": summary})
        return HTMLResponse(
            _render_portal_html(
                resource_url=resource_url,
                subject=subject,
                tenant_summary=summary,
                allow_user_writes=google_oauth.allow_user_writes,
                oidc_enabled=oidc_adapter is not None,
                flash_message="Connection saved! Your private MCP worker will load these settings on the next request.",
            )
        )

    async def disconnect(request: Request) -> Response:
        subject = await resolve_subject(request)
        if not subject:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        prior_creds = store.revoke_tenant(subject)
        refresh_token = (prior_creds or {}).get("refresh_token")

        revoke_state = {"revoked": False}

        async def _deferred_revoke() -> None:
            revoke_state["revoked"] = await adapter.revoke_token(refresh_token)

        worker_stopped, still_disconnecting = await pool.disconnect_worker(
            subject,
            wipe_profile=True,
            on_drained=_deferred_revoke if refresh_token else None,
            mark_disconnecting=True,
        )

        if still_disconnecting:
            draining = pool.is_draining(subject)
            msg = (
                "New MCP requests are blocked. Waiting for the in-flight request to finish "
                "before terminating the worker and revoking the Google Ads token."
                if draining
                else (
                    "Worker terminated and new MCP requests are blocked. Waiting for Google Ads "
                    "token revocation to complete before allowing reconnection."
                )
            )
            if _wants_json(request):
                return JSONResponse(
                    {
                        "ok": True,
                        "status": "disconnecting",
                        "disconnected": False,
                        "disconnecting": True,
                        "worker_stopped": not draining,
                        "worker_draining": draining,
                        "revocation_pending": True,
                        "google_token_revoked": False,
                        "message": msg,
                    },
                    status_code=202,
                    headers=_NO_STORE_HEADERS,
                )
            return HTMLResponse(
                _render_portal_html(
                    resource_url=resource_url,
                    subject=subject,
                    tenant_summary=None,
                    allow_user_writes=google_oauth.allow_user_writes,
                    oidc_enabled=oidc_adapter is not None,
                    flash_message=f"Disconnecting: {msg}",
                ),
                status_code=202,
                headers=_NO_STORE_HEADERS,
            )

        # Worker(s) and Google revocation (when a token existed) have finished cleanly.
        google_revoked = revoke_state["revoked"]
        if _wants_json(request):
            return JSONResponse(
                {
                    "ok": True,
                    "status": "disconnected",
                    "disconnected": True,
                    "disconnecting": False,
                    "worker_stopped": worker_stopped,
                    "worker_draining": False,
                    "revocation_pending": False,
                    "google_token_revoked": google_revoked,
                },
                headers=_NO_STORE_HEADERS,
            )
        return HTMLResponse(
            _render_portal_html(
                resource_url=resource_url,
                subject=subject,
                tenant_summary=None,
                allow_user_writes=google_oauth.allow_user_writes,
                oidc_enabled=oidc_adapter is not None,
                flash_message="Google Ads access revoked and private worker terminated.",
            ),
            headers=_NO_STORE_HEADERS,
        )

    return [
        Route("/connect", connect_page, methods=["GET"]),
        Route("/connect/ticket", redeem_ticket, methods=["GET"]),
        Route("/connect/login", oidc_login, methods=["GET"]),
        Route("/connect/oidc/callback", oidc_callback, methods=["GET"]),
        Route("/connect/session", create_session, methods=["POST"]),
        Route("/connect/status", connect_status, methods=["GET"]),
        Route("/connect/google/start", google_start, methods=["GET"]),
        Route("/connect/google/callback", google_callback, methods=["GET"]),
        Route("/connect/configure", configure, methods=["POST"]),
        Route("/connect/disconnect", disconnect, methods=["POST"]),
    ]
