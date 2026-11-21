"""Tests for Option 2: Self-serve hosted remote MCP (encrypted store, dynamic worker pool, web OAuth onboarding, inline CSV)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet

from agency import channels_tools as ct
from agency.modules import OPERATIONS_TOOLS, resolve_modules
from agency.remote_gateway import RemoteConfig, create_remote_app
from agency.remote_onboarding import (
    GoogleOAuthAdapter,
    GoogleWebOAuthConfig,
    PortalOIDCAdapter,
    PortalOIDCConfig,
)
from agency.remote_store import (
    DisconnectEpochMismatchError,
    DynamicWorkerPool,
    EncryptedProfileStore,
    PoolCapacityError,
    TenantProfile,
    decrypt_profile_payload,
    encrypt_profile_payload,
)


class _FakeVerifier:
    async def subject(self, token: str) -> str | None:
        return token if token in {"alice", "bob"} else None


class _FakeProcess:
    def __init__(self) -> None:
        self.alive = True
        self.terminated = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self) -> None:
        self.alive = False
        self.terminated = True

    def wait(self, timeout=None) -> int:
        self.alive = False
        return 0


class _FakeMCP:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, **kwargs):
        def dec(fn):
            self.tools[fn.__name__] = fn
            return fn

        return dec


def _sample_creds(refresh: str = "1//super-secret-refresh-token-xyz") -> dict:
    return {
        "token": "ya29.fake-access",
        "refresh_token": refresh,
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "web-client-id",
        "client_secret": "web-client-secret",
        "scopes": ["https://www.googleapis.com/auth/adwords"],
    }


# ---------- 1. Encrypted store & Fernet profile envelope ----------


def test_encrypted_profile_store_never_writes_plaintext_refresh_token(tmp_path):
    master_key = Fernet.generate_key()
    store = EncryptedProfileStore(tmp_path / "state", master_key)
    secret_refresh = "1//ultra-secret-refresh-token-998877"

    tenant = store.upsert_tenant(
        subject="alice",
        credentials=_sample_creds(secret_refresh),
        customers=["1111111111", "2222222222"],
        manager="9999999999",
        preset="ecommerce",
        allow_writes=False,
        email="alice@example.com",
    )
    assert tenant.preset == "ecommerce"
    assert tenant.customers == ("1111111111", "2222222222")
    assert tenant.allow_writes is False

    # Verify raw SQLite bytes do NOT contain plaintext refresh token
    db_bytes = (tmp_path / "state" / "tenants.db").read_bytes()
    assert secret_refresh.encode("utf-8") not in db_bytes

    # Verify materialized worker profile is ALSO Fernet-encrypted on disk
    profile_path = store.materialize_encrypted_profile(tenant)
    raw_profile_text = profile_path.read_text(encoding="utf-8")
    assert secret_refresh not in raw_profile_text
    raw_json = json.loads(raw_profile_text)
    assert raw_json["format"] == "fernet-v1"
    decrypted = decrypt_profile_payload(raw_json, master_key=master_key)
    assert decrypted["credentials"]["refresh_token"] == secret_refresh
    assert decrypted["preset"] == "ecommerce"

    # Update preferences and revoke
    updated = store.update_preferences("alice", preset="leads")
    assert updated.preset == "leads"
    prior = store.revoke_tenant("alice")
    assert prior is not None and prior["refresh_token"] == secret_refresh
    assert store.get_tenant("alice") is None
    store.wipe_runtime_profile("alice")
    assert not profile_path.exists()


# ---------- 2. DynamicWorkerPool lifecycle (lazy spawn, concurrency, LRU, idle eviction) ----------


@pytest.mark.asyncio
async def test_dynamic_worker_pool_lazy_spawn_dedup_lru_and_idle_eviction(tmp_path):
    master_key = Fernet.generate_key()
    store = EncryptedProfileStore(tmp_path / "state", master_key)
    for subj, cid in (("alice", "1111111111"), ("bob", "2222222222"), ("carol", "3333333333")):
        store.upsert_tenant(subject=subj, credentials=_sample_creds(f"1//tok-{subj}"), customers=[cid], preset="leads")

    spawned: list[tuple[str, int, str]] = []
    procs: dict[str, list[_FakeProcess]] = {}

    def fake_spawner(tenant: TenantProfile, profile_path: Path, port: int, worker_key: str, mk: bytes):
        proc = _FakeProcess()
        spawned.append((tenant.subject, port, tenant.preset))
        procs.setdefault(tenant.subject, []).append(proc)
        return proc

    async def fake_probe(port: int, timeout: float) -> bool:
        return True

    pool = DynamicWorkerPool(
        store=store,
        worker_key="w" * 40,
        master_key=master_key,
        idle_timeout_seconds=5.0,
        max_active_workers=2,
        spawner=fake_spawner,
        readiness_probe=fake_probe,
    )

    # Concurrent requests for alice must spawn only ONE worker
    url1, url2 = await asyncio.gather(
        pool.resolve_worker_url("alice"),
        pool.resolve_worker_url("alice"),
    )
    assert url1 == url2 and url1 is not None
    assert len(spawned) == 1

    # Bob spawns 2nd worker (max_active_workers=2)
    bob_url = await pool.resolve_worker_url("bob")
    assert bob_url is not None and bob_url != url1
    assert pool.active_worker_count() == 2

    # Carol triggers LRU eviction of oldest worker (alice)
    carol_url = await pool.resolve_worker_url("carol")
    assert carol_url is not None
    assert procs["alice"][0].terminated is True
    assert pool.active_worker_count() == 2

    # Updating bob's preset invalidates old worker on next request
    store.update_preferences("bob", preset="ecommerce")
    bob_url_2 = await pool.resolve_worker_url("bob")
    assert bob_url_2 != bob_url
    assert procs["bob"][0].terminated is True
    assert spawned[-1][2] == "ecommerce"

    # Simulate idle timeout and evict
    for w in pool._workers.values():
        w.last_used -= 100.0
    evicted = await pool.evict_idle_workers()
    assert evicted == 2
    assert pool.active_worker_count() == 0


# ---------- 3. Full self-serve onboarding + gateway /mcp + security checks ----------


@pytest.mark.asyncio
async def test_self_serve_web_onboarding_gateway_and_revocation_flow(tmp_path):
    master_key = Fernet.generate_key()
    worker_key = "k" * 40
    store = EncryptedProfileStore(tmp_path / "state", master_key)
    revoked_tokens: list[str] = []

    def fake_google_http(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            return httpx.Response(
                200,
                json={"access_token": "ya29.live-demo", "refresh_token": "1//alice-refresh-999"},
            )
        if url.startswith("https://oauth2.googleapis.com/revoke"):
            revoked_tokens.append(request.url.params.get("token", ""))
            return httpx.Response(200, json={})
        if url.endswith("/customers:listAccessibleCustomers"):
            return httpx.Response(
                200,
                json={"resourceNames": ["customers/1111111111", "customers/9999999999"]},
            )
        if "/customers/1111111111/googleAds:search" in url:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "customer": {
                                "id": "1111111111",
                                "descriptiveName": "Alice Shop",
                                "currencyCode": "EUR",
                                "manager": False,
                            }
                        }
                    ]
                },
            )
        if "/customers/9999999999/googleAds:search" in url:
            body = json.loads(request.content.decode("utf-8"))
            if "FROM customer_client" in body.get("query", ""):
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "customerClient": {
                                    "id": "2222222222",
                                    "descriptiveName": "Child Leads",
                                    "currencyCode": "USD",
                                    "manager": False,
                                    "status": "ENABLED",
                                }
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "customer": {
                                "id": "9999999999",
                                "descriptiveName": "Alice MCC",
                                "currencyCode": "USD",
                                "manager": True,
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404)

    google_cfg = GoogleWebOAuthConfig(
        client_id="google-web-client-id",
        client_secret="google-web-client-secret",
        redirect_uri="https://ads.example.com/connect/google/callback",
        allow_user_writes=False,
    )
    adapter = GoogleOAuthAdapter(google_cfg, transport=httpx.MockTransport(fake_google_http))

    spawned_tenants: list[TenantProfile] = []

    def fake_spawner(tenant: TenantProfile, profile_path: Path, port: int, wk: str, mk: bytes):
        spawned_tenants.append(tenant)
        return _FakeProcess()

    async def fake_probe(port: int, timeout: float) -> bool:
        return True

    pool = DynamicWorkerPool(
        store=store,
        worker_key=worker_key,
        master_key=master_key,
        spawner=fake_spawner,
        readiness_probe=fake_probe,
    )

    def fake_worker_upstream(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-mcp-worker-key"] == worker_key
        return httpx.Response(200, json={"ok": True, "upstream_port": request.url.port})

    config = RemoteConfig(
        resource="https://ads.example.com/mcp",
        issuer="https://id.example.com",
        jwks_url="https://id.example.com/jwks",
        worker_key=worker_key,
        self_serve=True,
    )
    app = create_remote_app(
        config,
        verifier=_FakeVerifier(),
        transport=httpx.MockTransport(fake_worker_upstream),
        worker_pool=pool,
        store=store,
        master_key=master_key,
        google_oauth=google_cfg,
        oauth_adapter=adapter,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://ads.example.com"
    ) as client:
        # 1. Before connecting: /mcp returns 403 with one-click ticket connect_url
        pre = await client.post("/mcp", headers={"Authorization": "Bearer alice"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert pre.status_code == 403
        assert pre.json()["error"] == "google_ads_not_connected"
        assert pre.json()["connect_url"].startswith("https://ads.example.com/connect/ticket?t=")

        # 2. Redeem one-click browser ticket WITHOUT any Authorization header (exact Claude/Codex browser UX)
        ticket_path = urlsplit(pre.json()["connect_url"])
        redeem = await client.get(f"{ticket_path.path}?{ticket_path.query}", follow_redirects=False)
        assert redeem.status_code == 302
        assert redeem.headers["location"] == "/connect"
        assert "mcp_portal_session" in redeem.cookies

        # Replaying the same single-use ticket MUST fail with 400
        replay_ticket = await client.get(
            f"{ticket_path.path}?{ticket_path.query}",
            headers={"Accept": "application/json"},
        )
        assert replay_ticket.status_code == 400
        assert replay_ticket.json()["error"] == "invalid_or_expired_ticket"

        # 3. Start Google OAuth with PKCE + CSRF state
        start = await client.get("/connect/google/start", headers={"Accept": "application/json"})
        assert start.status_code == 200
        start_data = start.json()
        auth_qs = parse_qs(urlsplit(start_data["authorize_url"]).query)
        assert auth_qs["code_challenge_method"] == ["S256"]
        assert auth_qs["access_type"] == ["offline"]
        state = start_data["state"]

        # 4. Cross-subject state replay (bob using alice's state) MUST fail with 400
        replay = await client.get(
            f"/connect/google/callback?code=fake&state={state}",
            headers={"Authorization": "Bearer bob", "Accept": "application/json"},
        )
        assert replay.status_code == 400
        assert replay.json()["error"] == "invalid_or_expired_state"

        # 5. Re-start cleanly as alice and finish callback
        start2 = await client.get("/connect/google/start", headers={"Accept": "application/json"})
        state2 = start2.json()["state"]
        cb = await client.get(
            f"/connect/google/callback?code=auth-code-123&state={state2}",
            headers={"Accept": "application/json"},
        )
        assert cb.status_code == 200
        cb_data = cb.json()
        grant_token = cb_data["grant_token"]
        assert {a["id"] for a in cb_data["direct_accounts"]} == {"1111111111", "9999999999"}
        assert cb_data["mcc_children"]["9999999999"][0]["id"] == "2222222222"

        # 6. Trying to enable writes when host disables them MUST fail with 403
        deny_writes = await client.post(
            "/connect/configure",
            json={
                "grant_token": grant_token,
                "customers": ["1111111111"],
                "preset": "full",
                "allow_writes": True,
            },
        )
        assert deny_writes.status_code == 403
        assert deny_writes.json()["error"] == "writes_disabled_by_host"

        # 7. Trying to attach an unauthorized customer ID MUST fail with 403
        start3 = await client.get("/connect/google/start", headers={"Accept": "application/json"})
        cb3 = await client.get(
            f"/connect/google/callback?code=auth-code-456&state={start3.json()['state']}",
            headers={"Accept": "application/json"},
        )
        grant3 = cb3.json()["grant_token"]
        bad_acct = await client.post(
            "/connect/configure",
            json={"grant_token": grant3, "customers": ["5555555555"], "preset": "ecommerce"},
        )
        assert bad_acct.status_code == 403
        assert bad_acct.json()["error"] == "customer_not_authorized"

        # 8. Valid configure with ecommerce preset & MCC child account
        start4 = await client.get("/connect/google/start", headers={"Accept": "application/json"})
        cb4 = await client.get(
            f"/connect/google/callback?code=auth-code-789&state={start4.json()['state']}",
            headers={"Accept": "application/json"},
        )
        grant4 = cb4.json()["grant_token"]
        cfg_resp = await client.post(
            "/connect/configure",
            json={
                "grant_token": grant4,
                "customers": ["1111111111", "2222222222"],
                "manager": "9999999999",
                "preset": "ecommerce",
            },
        )
        assert cfg_resp.status_code == 200
        assert cfg_resp.json()["profile"]["preset"] == "ecommerce"
        assert cfg_resp.json()["profile"]["read_only_mode"] is True

        # 9. Now /mcp lazily spawns alice's worker and forwards the request!
        mcp_resp = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer alice"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert mcp_resp.status_code == 200
        assert mcp_resp.json()["ok"] is True
        assert len(spawned_tenants) == 1
        assert spawned_tenants[0].preset == "ecommerce"
        assert spawned_tenants[0].allow_writes is False

        # 10. Disconnect revokes Google token, stops worker, and blocks /mcp again
        disc = await client.post("/connect/disconnect", headers={"Accept": "application/json"})
        assert disc.status_code == 200
        assert disc.json()["disconnected"] is True
        assert disc.json()["worker_stopped"] is True
        assert disc.json()["google_token_revoked"] is True
        assert revoked_tokens == ["1//alice-refresh-999"]

        post_disc = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer alice"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert post_disc.status_code == 403
        assert post_disc.json()["error"] == "google_ads_not_connected"


# ---------- 4. Real loopback worker subprocess with encrypted profile + compact preset ----------


@pytest.mark.asyncio
async def test_real_worker_subprocess_decrypts_profile_and_respects_preset(tmp_path):
    master_key = Fernet.generate_key()
    worker_key = "r" * 40
    store = EncryptedProfileStore(tmp_path / "state", master_key)
    store.upsert_tenant(
        subject="alice",
        credentials=_sample_creds("1//real-worker-refresh-token"),
        customers=["1111111111"],
        preset="ecommerce",
        allow_writes=False,
    )
    pool = DynamicWorkerPool(
        store=store,
        worker_key=worker_key,
        master_key=master_key,
        startup_timeout_seconds=15.0,
    )
    try:
        worker_url = await pool.resolve_worker_url("alice")
        assert worker_url is not None and worker_url.startswith("http://127.0.0.1:")

        async with httpx.AsyncClient(trust_env=False, timeout=10) as client:
            # 1. Direct request without x-mcp-worker-key MUST be rejected with 401
            unauth = await client.post(
                worker_url,
                headers={"Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert unauth.status_code == 401

            # 2. Authenticated worker request returns ecommerce catalog (NOT forced to full, 0 writes)
            resp = await client.post(
                worker_url,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "x-mcp-worker-key": worker_key,
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
            assert resp.status_code == 200
            payload = resp.json()
            names = {tool["name"] for tool in payload["result"]["tools"]}
            assert "get_pmax_diagnostics" in names
            assert "get_shopping_performance" in names
            # Must respect ecommerce preset (should NOT include search-only or mcc-only tools!)
            assert "generate_keyword_ideas" not in names
            assert not (names & OPERATIONS_TOOLS)
    finally:
        await pool.shutdown_all()


# ---------- 5. Inline CSV support for remote Auction Insights ----------


def test_analyze_auction_insights_csv_supports_inline_content_for_remote_users():
    mcp = _FakeMCP()
    ct.register_channel_tools(mcp)

    csv_text = (
        "Auction insights report\n"
        "Display URL domain,Impression share,Overlap rate,Position above rate,Top of page rate,Abs. Top of page rate,Outranking share\n"
        "You,42.5%,--,--,75.0%,38.0%,--\n"
        "competitor-a.example.com,58.0%,62.0%,68.0%,84.0%,49.0%,35.0%\n"
    )
    out = json.loads(mcp.tools["analyze_auction_insights_csv"](csv_content=csv_text))
    assert out["file"] == "inline_csv"
    assert out["source"] == "google_ads_ui_csv_export"
    assert out["competitor_count"] == 1
    assert out["top_competitor"]["domain"] == "competitor-a.example.com"
    assert len(out["high_pressure_competitors"]) == 1


# ---------- 6. Portal OIDC browser login + mixed direct/multi-MCC routes + write alignment ----------


@pytest.mark.asyncio
async def test_portal_oidc_login_mixed_mcc_routes_and_write_preset_alignment(tmp_path, monkeypatch):
    master_key = Fernet.generate_key()
    worker_key = "m" * 40
    store = EncryptedProfileStore(tmp_path / "state", master_key)

    def fake_oidc_http(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": "https://id.example.com/authorize",
                    "token_endpoint": "https://id.example.com/oauth/token",
                },
            )
        if url == "https://id.example.com/oauth/token":
            return httpx.Response(200, json={"access_token": "alice"})
        return httpx.Response(404)

    def fake_google_mixed(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            return httpx.Response(200, json={"access_token": "ya29.mix", "refresh_token": "1//mix-refresh"})
        if url.endswith("/customers:listAccessibleCustomers"):
            return httpx.Response(
                200,
                json={"resourceNames": ["customers/1111111111", "customers/9999999999", "customers/8888888888"]},
            )
        if "/customers/1111111111/googleAds:search" in url:
            return httpx.Response(200, json={"results": [{"customer": {"id": "1111111111", "descriptiveName": "Direct", "currencyCode": "EUR", "manager": False}}]})
        if "/customers/9999999999/googleAds:search" in url:
            q = json.loads(request.content.decode("utf-8")).get("query", "")
            if "FROM customer_client" in q:
                return httpx.Response(200, json={"results": [{"customerClient": {"id": "2222222222", "descriptiveName": "MCC A Child", "currencyCode": "USD", "manager": False, "status": "ENABLED"}}]})
            return httpx.Response(200, json={"results": [{"customer": {"id": "9999999999", "descriptiveName": "MCC A", "currencyCode": "USD", "manager": True}}]})
        if "/customers/8888888888/googleAds:search" in url:
            q = json.loads(request.content.decode("utf-8")).get("query", "")
            if "FROM customer_client" in q:
                return httpx.Response(200, json={"results": [{"customerClient": {"id": "3333333333", "descriptiveName": "MCC B Child", "currencyCode": "EUR", "manager": False, "status": "ENABLED"}}]})
            return httpx.Response(200, json={"results": [{"customer": {"id": "8888888888", "descriptiveName": "MCC B", "currencyCode": "EUR", "manager": True}}]})
        return httpx.Response(404)

    google_cfg = GoogleWebOAuthConfig(
        client_id="g-id",
        client_secret="g-sec",
        redirect_uri="https://ads.example.com/connect/google/callback",
        allow_user_writes=True,
    )
    oidc_cfg = PortalOIDCConfig(
        issuer="https://id.example.com",
        client_id="portal-client",
        redirect_uri="https://ads.example.com/connect/oidc/callback",
        resource="https://ads.example.com/mcp",
    )
    pool = DynamicWorkerPool(
        store=store,
        worker_key=worker_key,
        master_key=master_key,
        spawner=lambda *a: _FakeProcess(),
        readiness_probe=lambda port, t: asyncio.sleep(0, result=True),
    )
    config = RemoteConfig(
        resource="https://ads.example.com/mcp",
        issuer="https://id.example.com",
        jwks_url="https://id.example.com/jwks",
        worker_key=worker_key,
        self_serve=True,
    )
    app = create_remote_app(
        config,
        verifier=_FakeVerifier(),
        worker_pool=pool,
        store=store,
        master_key=master_key,
        google_oauth=google_cfg,
        oauth_adapter=GoogleOAuthAdapter(google_cfg, transport=httpx.MockTransport(fake_google_mixed)),
        portal_oidc=oidc_cfg,
        portal_oidc_adapter=PortalOIDCAdapter(oidc_cfg, transport=httpx.MockTransport(fake_oidc_http)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://ads.example.com"
    ) as client:
        # 1. Unauthenticated /connect HTML offers browser sign-in button (not Bearer token instructions)
        landing = await client.get("/connect")
        assert landing.status_code == 200
        assert "/connect/login" in landing.text

        # 2. Complete OIDC browser login -> verify browser-binding cookie (RFC 9700)
        login_start = await client.get("/connect/login", headers={"Accept": "application/json"})
        assert login_start.status_code == 200
        assert "mcp_oidc_tx" in login_start.cookies
        oidc_state = login_start.json()["state"]

        # A DIFFERENT browser without the `mcp_oidc_tx` cookie MUST be rejected (blocks login-CSRF)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://ads.example.com"
        ) as attacker_browser:
            csrf_attempt = await attacker_browser.get(
                f"/connect/oidc/callback?code=oidc-code&state={oidc_state}",
                headers={"Accept": "application/json"},
            )
            assert csrf_attempt.status_code == 400
            assert csrf_attempt.json()["error"] == "oidc_browser_mismatch"

        # Original browser with `mcp_oidc_tx` completes cleanly
        login_cb = await client.get(
            f"/connect/oidc/callback?code=oidc-code&state={oidc_state}",
            follow_redirects=False,
        )
        assert login_cb.status_code == 302
        assert login_cb.headers.get("cache-control") == "no-store"
        assert login_cb.headers.get("referrer-policy") == "no-referrer"
        assert "mcp_portal_session" in login_cb.cookies

        # 3. Connect Google Ads with mixed direct + MCC A + MCC B accounts
        g_start = await client.get("/connect/google/start", headers={"Accept": "application/json"})
        g_cb = await client.get(
            f"/connect/google/callback?code=g-code&state={g_start.json()['state']}",
            headers={"Accept": "application/json"},
        )
        grant = g_cb.json()["grant_token"]
        assert g_cb.json()["authorized_routes"] == {
            "1111111111": [""],
            "9999999999": [""],
            "8888888888": [""],
            "2222222222": ["9999999999"],
            "3333333333": ["8888888888"],
        }

        # 4. Save mixed accounts + allow_writes=True with preset='core' -> operations MUST be auto-added!
        cfg = await client.post(
            "/connect/configure",
            json={
                "grant_token": grant,
                "customers": ["1111111111", "2222222222", "3333333333"],
                "preset": "core",
                "allow_writes": True,
            },
        )
        assert cfg.status_code == 200
        prof = cfg.json()["profile"]
        assert prof["customer_routes"] == {
            "1111111111": "",
            "2222222222": "9999999999",
            "3333333333": "8888888888",
        }
        assert prof["manager"] is None  # Mixed routes -> no global manager leak!
        assert prof["allow_writes"] is True
        _, allowed_tools, eff_ro = resolve_modules(
            preset=prof["preset"], modules=prof["modules"], read_only=False
        )
        assert eff_ro is False
        assert OPERATIONS_TOOLS <= allowed_tools

        # 5. Editing profile to inject an unvalidated manager MUST be blocked with 403!
        bad_mgr = await client.post(
            "/connect/configure",
            json={"preset": "agency", "manager": "7777777777"},
        )
        assert bad_mgr.status_code == 403
        assert bad_mgr.json()["error"] == "reconnect_required_for_route_change"

        bad_route = await client.post(
            "/connect/configure",
            json={"preset": "agency", "customer_routes": {"2222222222": "8888888888"}},
        )
        assert bad_route.status_code == 403
        assert bad_route.json()["error"] == "reconnect_required_for_route_change"

    # 6. Verify per-customer login-customer-id header routing in google_ads_server.py
    import google_ads_server as srv

    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_MAP", "1111111111:,2222222222:9999999999,3333333333:8888888888")

    kw_direct = {"headers": {"Authorization": "Bearer x", "login-customer-id": "9999999999"}}
    srv._apply_per_customer_login_header("https://googleads.googleapis.com/v25/customers/1111111111/googleAds:search", kw_direct)
    assert "login-customer-id" not in kw_direct["headers"]

    kw_mcc_a = {"headers": {"Authorization": "Bearer x"}}
    srv._apply_per_customer_login_header("https://googleads.googleapis.com/v25/customers/2222222222/googleAds:search", kw_mcc_a)
    assert kw_mcc_a["headers"]["login-customer-id"] == "9999999999"

    kw_mcc_b = {"headers": {"Authorization": "Bearer x"}}
    srv._apply_per_customer_login_header("https://googleads.googleapis.com/v25/customers/3333333333/googleAds:search", kw_mcc_b)
    assert kw_mcc_b["headers"]["login-customer-id"] == "8888888888"


# ---------- 7. Pool concurrency cap (never exceeds max_active_workers) & in-flight protection ----------


@pytest.mark.asyncio
async def test_worker_pool_strict_capacity_under_concurrency_and_inflight_protection(tmp_path):
    master_key = Fernet.generate_key()
    store = EncryptedProfileStore(tmp_path / "state", master_key)
    store.upsert_tenant(subject="alice", credentials=_sample_creds("1//a"), customers=["1111111111"], preset="core")
    store.upsert_tenant(subject="bob", credentials=_sample_creds("1//b"), customers=["2222222222"], preset="core")

    spawn_count = 0

    def counting_spawner(tenant: TenantProfile, profile_path: Path, port: int, wk: str, mk: bytes):
        nonlocal spawn_count
        spawn_count += 1
        return _FakeProcess()

    async def slow_probe(port: int, timeout: float) -> bool:
        await asyncio.sleep(0.12)
        return True

    pool = DynamicWorkerPool(
        store=store,
        worker_key="c" * 40,
        master_key=master_key,
        idle_timeout_seconds=5.0,
        max_active_workers=1,
        spawner=counting_spawner,
        readiness_probe=slow_probe,
    )

    # 1. Exact reproduction: 2 simultaneous acquisitions with max_active_workers=1
    res_a, res_b = await asyncio.gather(
        pool.acquire_worker_handle("alice"),
        pool.acquire_worker_handle("bob"),
        return_exceptions=True,
    )
    handles = [r for r in (res_a, res_b) if not isinstance(r, BaseException)]
    errors = [r for r in (res_a, res_b) if isinstance(r, PoolCapacityError)]
    assert len(handles) == 1
    assert len(errors) == 1
    assert spawn_count == 1
    assert pool.active_worker_count() == 1

    active_handle = handles[0]
    other_subject = "bob" if active_handle.subject == "alice" else "alice"

    # 2. While active_handle is in-flight, idle eviction MUST NOT kill it!
    active_handle.last_used -= 9999.0
    evicted = await pool.evict_idle_workers()
    assert evicted == 0
    assert pool.active_worker_count() == 1

    # 3. And another user MUST get PoolCapacityError rather than killing the in-flight worker!
    with pytest.raises(PoolCapacityError):
        await pool.acquire_worker_handle(other_subject)
    assert active_handle.process.terminated is False

    # 4. Once in-flight request finishes, the other user cleanly evicts the idle worker and spawns!
    await pool.release_worker_handle(active_handle)
    next_handle = await pool.acquire_worker_handle(other_subject)
    assert next_handle is not None
    assert active_handle.process.terminated is True
    assert pool.active_worker_count() == 1
    await pool.release_worker_handle(next_handle)
    await pool.shutdown_all()


# ---------- 8. Final hardening: runtime pinned route precedence, draining count/safety, host write policy ----------


def test_pinned_direct_route_in_login_customer_map_is_not_overridden_by_session_discovery(monkeypatch):
    from agency.runtime import clear_session_login_map, register_login_customer, resolve_login_customer_id

    clear_session_login_map()
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")
    monkeypatch.setenv("GOOGLE_ADS_LOGIN_CUSTOMER_MAP", "1111111111:,2222222222:9999999999")

    # Simulate MCC discovery seeing 1111111111 under MCC 9999999999: MUST NOT override pinned direct route!
    register_login_customer("1111111111", "9999999999")
    assert resolve_login_customer_id("1111111111") is None
    assert resolve_login_customer_id("2222222222") == "9999999999"
    clear_session_login_map()


@pytest.mark.asyncio
async def test_draining_worker_stays_counted_and_finishes_inflight_write_before_termination(tmp_path):
    master_key = Fernet.generate_key()
    store = EncryptedProfileStore(tmp_path / "state", master_key)
    store.upsert_tenant(subject="alice", credentials=_sample_creds("1//a"), customers=["1111111111"], preset="full", allow_writes=True)
    store.upsert_tenant(subject="bob", credentials=_sample_creds("1//b"), customers=["2222222222"], preset="core")

    spawned_writes_flags: list[bool] = []

    def spawner(tenant: TenantProfile, profile_path: Path, port: int, wk: str, mk: bytes):
        spawned_writes_flags.append(tenant.allow_writes)
        return _FakeProcess()

    pool = DynamicWorkerPool(
        store=store,
        worker_key="d" * 40,
        master_key=master_key,
        allow_user_writes=False,  # Host policy disabled writes globally!
        max_active_workers=1,
        drain_timeout_seconds=0.2,
        spawner=spawner,
        readiness_probe=lambda port, t: asyncio.sleep(0, result=True),
    )

    # 1. Host policy allow_user_writes=False MUST force allow_writes=False even though SQLite stored allow_writes=True!
    alice_handle = await pool.acquire_worker_handle("alice")
    assert alice_handle is not None
    assert spawned_writes_flags == [False]

    # 2. stop_worker while alice has in_flight=1 MUST NOT kill process on drain timeout AND MUST keep it counted!
    await pool.stop_worker("alice")
    assert alice_handle.process.terminated is False
    assert pool.active_worker_count() == 1

    # 3. Because alice's draining worker still occupies the 1 slot, bob MUST get PoolCapacityError!
    with pytest.raises(PoolCapacityError):
        await pool.acquire_worker_handle("bob")

    # 4. As soon as alice's in-flight request completes, the draining worker terminates immediately & bob can spawn!
    await pool.release_worker_handle(alice_handle)
    assert alice_handle.process.terminated is True
    assert pool.active_worker_count() == 0

    bob_handle = await pool.acquire_worker_handle("bob")
    assert bob_handle is not None
    assert pool.active_worker_count() == 1
    await pool.release_worker_handle(bob_handle)
    await pool.shutdown_all()


@pytest.mark.asyncio
async def test_disconnect_while_worker_draining_returns_202_and_defers_google_revocation(tmp_path):
    master_key = Fernet.generate_key()
    worker_key = "z" * 40
    store = EncryptedProfileStore(tmp_path / "state", master_key)
    store.upsert_tenant(
        subject="alice",
        credentials=_sample_creds("1//alice-inflight-token"),
        customers=["1111111111"],
        preset="core",
    )

    revoked_tokens: list[str] = []

    def fake_google_revoke(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://oauth2.googleapis.com/revoke"):
            revoked_tokens.append(request.url.params.get("token", ""))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    google_cfg = GoogleWebOAuthConfig(
        client_id="g-id",
        client_secret="g-sec",
        redirect_uri="https://ads.example.com/connect/google/callback",
    )
    pool = DynamicWorkerPool(
        store=store,
        worker_key=worker_key,
        master_key=master_key,
        drain_timeout_seconds=0.2,
        spawner=lambda *a: _FakeProcess(),
        readiness_probe=lambda port, t: asyncio.sleep(0, result=True),
    )
    config = RemoteConfig(
        resource="https://ads.example.com/mcp",
        issuer="https://id.example.com",
        jwks_url="https://id.example.com/jwks",
        worker_key=worker_key,
        self_serve=True,
    )
    app = create_remote_app(
        config,
        verifier=_FakeVerifier(),
        worker_pool=pool,
        store=store,
        master_key=master_key,
        google_oauth=google_cfg,
        oauth_adapter=GoogleOAuthAdapter(google_cfg, transport=httpx.MockTransport(fake_google_revoke)),
    )

    # Simulate an active in-flight MCP request for alice
    handle = await pool.acquire_worker_handle("alice")
    assert handle is not None
    assert handle.profile_path is not None
    profile_path = handle.profile_path
    assert profile_path.exists()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://ads.example.com"
    ) as client:
        # Disconnect while in-flight -> MUST return 202 'disconnecting' and DEFER Google token revocation!
        disc = await client.post(
            "/connect/disconnect",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert disc.status_code == 202
        body = disc.json()
        assert body["status"] == "disconnecting"
        assert body["disconnected"] is False
        assert body["disconnecting"] is True
        assert body["worker_draining"] is True
        assert body["worker_stopped"] is False
        assert body["google_token_revoked"] is False
        assert revoked_tokens == []
        assert handle.process.terminated is False
        assert profile_path.exists()

        # /connect/status also reflects disconnecting=True while draining
        st = await client.get(
            "/connect/status",
            headers={"Authorization": "Bearer alice"},
        )
        assert st.status_code == 200
        assert st.json()["connected"] is False
        assert st.json()["disconnecting"] is True
        assert st.json()["worker_draining"] is True

        # New /mcp requests are already blocked immediately (403)
        blocked = await client.post(
            "/mcp",
            headers={"Authorization": "Bearer alice"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert blocked.status_code == 403
        assert blocked.json()["error"] == "google_ads_not_connected"

    # Once the in-flight request completes, worker terminates, profile wipes, and Google token revokes!
    await pool.release_worker_handle(handle)
    assert handle.process.terminated is True
    assert not profile_path.exists()
    assert revoked_tokens == ["1//alice-inflight-token"]
    assert pool.active_worker_count() == 0


@pytest.mark.asyncio
async def test_repeated_disconnect_while_draining_stays_202_and_preserves_profile_until_drained(tmp_path):
    master_key = Fernet.generate_key()
    worker_key = "r" * 40
    store = EncryptedProfileStore(tmp_path / "state", master_key)
    store.upsert_tenant(
        subject="alice",
        credentials=_sample_creds("1//alice-repeat-token"),
        customers=["1111111111"],
        preset="core",
    )

    revoked_tokens: list[str] = []

    def fake_google_revoke(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://oauth2.googleapis.com/revoke"):
            revoked_tokens.append(request.url.params.get("token", ""))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    google_cfg = GoogleWebOAuthConfig(
        client_id="g-id",
        client_secret="g-sec",
        redirect_uri="https://ads.example.com/connect/google/callback",
    )
    pool = DynamicWorkerPool(
        store=store,
        worker_key=worker_key,
        master_key=master_key,
        drain_timeout_seconds=0.2,
        spawner=lambda *a: _FakeProcess(),
        readiness_probe=lambda port, t: asyncio.sleep(0, result=True),
    )
    config = RemoteConfig(
        resource="https://ads.example.com/mcp",
        issuer="https://id.example.com",
        jwks_url="https://id.example.com/jwks",
        worker_key=worker_key,
        self_serve=True,
    )
    app = create_remote_app(
        config,
        verifier=_FakeVerifier(),
        worker_pool=pool,
        store=store,
        master_key=master_key,
        google_oauth=google_cfg,
        oauth_adapter=GoogleOAuthAdapter(google_cfg, transport=httpx.MockTransport(fake_google_revoke)),
    )

    handle = await pool.acquire_worker_handle("alice")
    assert handle is not None and handle.profile_path is not None
    profile_path = handle.profile_path
    assert profile_path.exists()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://ads.example.com"
    ) as client:
        # 1st disconnect -> 202 disconnecting
        first = await client.post(
            "/connect/disconnect",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert first.status_code == 202
        assert first.json()["status"] == "disconnecting"
        assert profile_path.exists()

        # 2nd disconnect while STILL draining -> MUST STILL be 202 disconnecting and NOT wipe profile!
        second = await client.post(
            "/connect/disconnect",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert second.status_code == 202
        body2 = second.json()
        assert body2["status"] == "disconnecting"
        assert body2["disconnected"] is False
        assert body2["disconnecting"] is True
        assert body2["worker_draining"] is True
        assert profile_path.exists()
        assert handle.process.terminated is False
        assert revoked_tokens == []

        # Finish in-flight request -> worker terminates, profile deleted, token revoked ONCE
        await pool.release_worker_handle(handle)
        assert handle.process.terminated is True
        assert not profile_path.exists()
        assert revoked_tokens == ["1//alice-repeat-token"]
        assert pool.active_worker_count() == 0

        # 3rd disconnect after draining finished -> 200 disconnected
        third = await client.post(
            "/connect/disconnect",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert third.status_code == 200
        assert third.json()["status"] == "disconnected"
        assert third.json()["disconnected"] is True
        assert third.json()["worker_draining"] is False
        assert revoked_tokens == ["1//alice-repeat-token"]


@pytest.mark.asyncio
async def test_reconnect_while_previous_worker_drains_preserves_new_worker_profile(tmp_path):
    master_key = Fernet.generate_key()
    worker_key = "q" * 40
    store = EncryptedProfileStore(tmp_path / "state", master_key)
    store.upsert_tenant(
        subject="alice",
        credentials=_sample_creds("1//alice-old-token"),
        customers=["1111111111"],
        preset="core",
    )

    revoked_tokens: list[str] = []

    def fake_google_revoke(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://oauth2.googleapis.com/revoke"):
            revoked_tokens.append(request.url.params.get("token", ""))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    google_cfg = GoogleWebOAuthConfig(
        client_id="g-id",
        client_secret="g-sec",
        redirect_uri="https://ads.example.com/connect/google/callback",
    )
    pool = DynamicWorkerPool(
        store=store,
        worker_key=worker_key,
        master_key=master_key,
        max_active_workers=2,
        drain_timeout_seconds=0.2,
        spawner=lambda *a: _FakeProcess(),
        readiness_probe=lambda port, t: asyncio.sleep(0, result=True),
    )
    config = RemoteConfig(
        resource="https://ads.example.com/mcp",
        issuer="https://id.example.com",
        jwks_url="https://id.example.com/jwks",
        worker_key=worker_key,
        self_serve=True,
    )
    app = create_remote_app(
        config,
        verifier=_FakeVerifier(),
        worker_pool=pool,
        store=store,
        master_key=master_key,
        google_oauth=google_cfg,
        oauth_adapter=GoogleOAuthAdapter(google_cfg, transport=httpx.MockTransport(fake_google_revoke)),
    )

    # Worker A starts and holds an active in-flight request
    handle_a = await pool.acquire_worker_handle("alice")
    assert handle_a is not None and handle_a.profile_path is not None
    profile_a = handle_a.profile_path
    assert profile_a.exists()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://ads.example.com"
    ) as client:
        disc = await client.post(
            "/connect/disconnect",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert disc.status_code == 202
        assert profile_a.exists()

    # While Worker A is STILL draining, Alice reconnects with a fresh OAuth token and spawns Worker B
    store.upsert_tenant(
        subject="alice",
        credentials=_sample_creds("1//alice-new-token"),
        customers=["2222222222"],
        preset="full",
    )
    handle_b = await pool.acquire_worker_handle("alice")
    assert handle_b is not None and handle_b.profile_path is not None
    profile_b = handle_b.profile_path
    assert profile_b != profile_a
    assert profile_a.exists()
    assert profile_b.exists()
    assert pool.active_worker_count() == 2

    # Now Worker A finishes draining: ONLY Worker A's profile is deleted and ONLY the old token is revoked!
    await pool.release_worker_handle(handle_a)
    assert handle_a.process.terminated is True
    assert not profile_a.exists()
    assert revoked_tokens == ["1//alice-old-token"]

    # Worker B and its encrypted profile MUST survive completely intact!
    assert handle_b.process.terminated is False
    assert profile_b.exists()
    payload_b = decrypt_profile_payload(json.loads(profile_b.read_text(encoding="utf-8")), master_key)
    assert payload_b["credentials"]["refresh_token"] == "1//alice-new-token"
    assert payload_b["customers"] == ["2222222222"]
    assert pool.active_worker_count() == 1

    await pool.release_worker_handle(handle_b)
    await pool.shutdown_all()
    assert not profile_b.exists()
    assert pool.active_worker_count() == 0


@pytest.mark.asyncio
async def test_disconnect_revocation_in_flight_keeps_disconnecting_status_and_blocks_reconnect(tmp_path):
    master_key = Fernet.generate_key()
    worker_key = "v" * 40
    store = EncryptedProfileStore(tmp_path / "state", master_key)
    store.upsert_tenant(
        subject="alice",
        credentials=_sample_creds("1//alice-slow-revoke-token"),
        customers=["1111111111"],
        preset="core",
    )

    revoke_started = asyncio.Event()
    allow_revoke_finish = asyncio.Event()
    revoked_tokens: list[str] = []
    token_exchanges = 0

    async def slow_google_transport(request: httpx.Request) -> httpx.Response:
        nonlocal token_exchanges
        if str(request.url).startswith("https://oauth2.googleapis.com/revoke"):
            revoked_tokens.append(request.url.params.get("token", ""))
            revoke_started.set()
            await allow_revoke_finish.wait()
            return httpx.Response(200, json={})
        if str(request.url) == "https://oauth2.googleapis.com/token":
            token_exchanges += 1
            return httpx.Response(200, json={"access_token": "ya29.new", "refresh_token": "1//new"})
        return httpx.Response(404)

    google_cfg = GoogleWebOAuthConfig(
        client_id="g-id",
        client_secret="g-sec",
        redirect_uri="https://ads.example.com/connect/google/callback",
    )
    pool = DynamicWorkerPool(
        store=store,
        worker_key=worker_key,
        master_key=master_key,
        drain_timeout_seconds=0.2,
        spawner=lambda *a: _FakeProcess(),
        readiness_probe=lambda port, t: asyncio.sleep(0, result=True),
    )
    config = RemoteConfig(
        resource="https://ads.example.com/mcp",
        issuer="https://id.example.com",
        jwks_url="https://id.example.com/jwks",
        worker_key=worker_key,
        self_serve=True,
    )
    app = create_remote_app(
        config,
        verifier=_FakeVerifier(),
        worker_pool=pool,
        store=store,
        master_key=master_key,
        google_oauth=google_cfg,
        oauth_adapter=GoogleOAuthAdapter(google_cfg, transport=httpx.MockTransport(slow_google_transport)),
    )

    handle = await pool.acquire_worker_handle("alice")
    assert handle is not None and handle.profile_path is not None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://ads.example.com"
    ) as client:
        # 1. Disconnect while in-flight -> 202 disconnecting
        disc1 = await client.post(
            "/connect/disconnect",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert disc1.status_code == 202
        assert disc1.json()["disconnecting"] is True
        assert disc1.json()["worker_draining"] is True
        assert disc1.json()["revocation_pending"] is True

        # Reconnect while worker drains -> blocked with 409 disconnect_in_progress
        blocked_while_draining = await client.get(
            "/connect/google/start",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert blocked_while_draining.status_code == 409
        assert blocked_while_draining.json()["error"] == "disconnect_in_progress"

        # 2. Finish worker request; worker terminates, but Google /revoke is STILL in flight!
        release_task = asyncio.create_task(pool.release_worker_handle(handle))
        await asyncio.wait_for(revoke_started.wait(), timeout=2.0)
        assert handle.process.terminated is True
        assert not handle.profile_path.exists()
        assert pool.active_worker_count() == 0

        # /connect/status MUST STILL report disconnecting=True while Google hasn't answered!
        st = await client.get(
            "/connect/status",
            headers={"Authorization": "Bearer alice"},
        )
        assert st.status_code == 200
        st_body = st.json()
        assert st_body["connected"] is False
        assert st_body["disconnecting"] is True
        assert st_body["worker_draining"] is False
        assert st_body["revocation_pending"] is True

        # Retry POST /connect/disconnect MUST STILL return 202 (NEVER premature 200!)
        disc2 = await client.post(
            "/connect/disconnect",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert disc2.status_code == 202
        body2 = disc2.json()
        assert body2["status"] == "disconnecting"
        assert body2["disconnected"] is False
        assert body2["disconnecting"] is True
        assert body2["worker_draining"] is False
        assert body2["revocation_pending"] is True

        # ALL reconnect endpoints MUST be blocked with 409 until Google revocation completes!
        for method, path, kwargs in (
            ("GET", "/connect/google/start", {}),
            ("GET", "/connect/google/callback?code=fake&state=fake", {}),
            ("POST", "/connect/configure", {"json": {"customers": ["1111111111"], "preset": "core"}}),
        ):
            resp = await client.request(
                method,
                path,
                headers={"Authorization": "Bearer alice", "Accept": "application/json"},
                **kwargs,
            )
            assert resp.status_code == 409
            assert resp.json()["error"] == "disconnect_in_progress"
            assert resp.headers.get("retry-after") == "2"
        assert token_exchanges == 0

        # 3. Allow Google /revoke to finish -> disconnecting clears and reconnect succeeds!
        allow_revoke_finish.set()
        await asyncio.wait_for(release_task, timeout=2.0)
        assert revoked_tokens == ["1//alice-slow-revoke-token"]

        st_done = await client.get(
            "/connect/status",
            headers={"Authorization": "Bearer alice"},
        )
        assert st_done.status_code == 200
        assert st_done.json()["connected"] is False
        assert st_done.json()["disconnecting"] is False
        assert st_done.json()["revocation_pending"] is False

        disc3 = await client.post(
            "/connect/disconnect",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert disc3.status_code == 200
        assert disc3.json()["status"] == "disconnected"
        assert disc3.json()["disconnected"] is True

        reconnect_start = await client.get(
            "/connect/google/start",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert reconnect_start.status_code == 200
        assert "authorize_url" in reconnect_start.json()


@pytest.mark.asyncio
async def test_direct_disconnect_revocation_in_flight_without_worker_keeps_disconnecting_and_blocks_reconnect(tmp_path):
    master_key = Fernet.generate_key()
    worker_key = "w" * 40
    store = EncryptedProfileStore(tmp_path / "state", master_key)
    store.upsert_tenant(
        subject="alice",
        credentials=_sample_creds("1//alice-noworker-revoke-token"),
        customers=["1111111111"],
        preset="core",
    )

    revoke_started = asyncio.Event()
    allow_revoke_finish = asyncio.Event()

    async def slow_google_revoke(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith("https://oauth2.googleapis.com/revoke"):
            revoke_started.set()
            await allow_revoke_finish.wait()
            return httpx.Response(200, json={})
        return httpx.Response(404)

    google_cfg = GoogleWebOAuthConfig(
        client_id="g-id",
        client_secret="g-sec",
        redirect_uri="https://ads.example.com/connect/google/callback",
    )
    pool = DynamicWorkerPool(
        store=store,
        worker_key=worker_key,
        master_key=master_key,
        drain_timeout_seconds=0.2,
        spawner=lambda *a: _FakeProcess(),
        readiness_probe=lambda port, t: asyncio.sleep(0, result=True),
    )
    config = RemoteConfig(
        resource="https://ads.example.com/mcp",
        issuer="https://id.example.com",
        jwks_url="https://id.example.com/jwks",
        worker_key=worker_key,
        self_serve=True,
    )
    app = create_remote_app(
        config,
        verifier=_FakeVerifier(),
        worker_pool=pool,
        store=store,
        master_key=master_key,
        google_oauth=google_cfg,
        oauth_adapter=GoogleOAuthAdapter(google_cfg, transport=httpx.MockTransport(slow_google_revoke)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://ads.example.com"
    ) as client:
        # Start disconnect with NO active worker; Google revoke pauses in flight
        disc_task = asyncio.create_task(
            client.post(
                "/connect/disconnect",
                headers={"Authorization": "Bearer alice", "Accept": "application/json"},
            )
        )
        await asyncio.wait_for(revoke_started.wait(), timeout=2.0)

        # Concurrent /connect/status shows disconnecting=True and revocation_pending=True
        st = await client.get("/connect/status", headers={"Authorization": "Bearer alice"})
        assert st.status_code == 200
        assert st.json()["disconnecting"] is True
        assert st.json()["revocation_pending"] is True

        # Concurrent retry of /connect/disconnect returns 202 disconnecting (not 200)
        retry = await client.post(
            "/connect/disconnect",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert retry.status_code == 202
        assert retry.json()["status"] == "disconnecting"
        assert retry.json()["revocation_pending"] is True

        # Concurrent reconnect blocked with 409 disconnect_in_progress
        reconn = await client.get(
            "/connect/google/start",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert reconn.status_code == 409
        assert reconn.json()["error"] == "disconnect_in_progress"

        # Finish Google revoke -> initial disconnect resolves 200 and reconnect unblocks!
        allow_revoke_finish.set()
        disc_resp = await asyncio.wait_for(disc_task, timeout=2.0)
        assert disc_resp.status_code == 200
        assert disc_resp.json()["status"] == "disconnected"
        assert disc_resp.json()["google_token_revoked"] is True

        st_done = await client.get("/connect/status", headers={"Authorization": "Bearer alice"})
        assert st_done.json()["disconnecting"] is False
        assert st_done.json()["revocation_pending"] is False

        reconn_ok = await client.get(
            "/connect/google/start",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert reconn_ok.status_code == 200


@pytest.mark.asyncio
async def test_inflight_google_oauth_callback_is_rejected_when_disconnect_starts_mid_exchange(tmp_path):
    master_key = Fernet.generate_key()
    worker_key = "m" * 40
    store = EncryptedProfileStore(tmp_path / "state", master_key)
    store.upsert_tenant(
        subject="alice",
        credentials=_sample_creds("1//alice-initial-token"),
        customers=["1111111111"],
        preset="core",
    )

    token_started = asyncio.Event()
    allow_token_finish = asyncio.Event()
    revoke_started = asyncio.Event()
    allow_revoke_finish = asyncio.Event()
    pause_token = True
    pause_revoke = True

    async def google_transport(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://oauth2.googleapis.com/token":
            token_started.set()
            if pause_token:
                await allow_token_finish.wait()
            return httpx.Response(
                200,
                json={
                    "access_token": "ya29.fresh",
                    "refresh_token": "1//alice-exchanged-token",
                    "expires_in": 3600,
                },
            )
        if url.endswith("/customers:listAccessibleCustomers"):
            return httpx.Response(200, json={"resourceNames": ["customers/1111111111"]})
        if "/customers/1111111111/googleAds:search" in url:
            return httpx.Response(
                200,
                json={"results": [{"customer": {"id": "1111111111", "descriptiveName": "Direct", "manager": False}}]},
            )
        if url.startswith("https://oauth2.googleapis.com/revoke"):
            revoke_started.set()
            if pause_revoke:
                await allow_revoke_finish.wait()
            return httpx.Response(200, json={})
        return httpx.Response(404)

    google_cfg = GoogleWebOAuthConfig(
        client_id="g-id",
        client_secret="g-sec",
        redirect_uri="https://ads.example.com/connect/google/callback",
    )
    pool = DynamicWorkerPool(
        store=store,
        worker_key=worker_key,
        master_key=master_key,
        drain_timeout_seconds=0.2,
        spawner=lambda *a: _FakeProcess(),
        readiness_probe=lambda port, t: asyncio.sleep(0, result=True),
    )
    config = RemoteConfig(
        resource="https://ads.example.com/mcp",
        issuer="https://id.example.com",
        jwks_url="https://id.example.com/jwks",
        worker_key=worker_key,
        self_serve=True,
    )
    app = create_remote_app(
        config,
        verifier=_FakeVerifier(),
        worker_pool=pool,
        store=store,
        master_key=master_key,
        google_oauth=google_cfg,
        oauth_adapter=GoogleOAuthAdapter(google_cfg, transport=httpx.MockTransport(google_transport)),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://ads.example.com"
    ) as client:
        # Variant A: OAuth callback is ALREADY mid-exchange when disconnect starts,
        # and finishes exchanging WHILE Google revocation is still in flight.
        start_a = await client.get(
            "/connect/google/start",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert start_a.status_code == 200
        state_a = start_a.json()["state"]

        cb_task_a = asyncio.create_task(
            client.get(
                f"/connect/google/callback?code=code-a&state={state_a}",
                headers={"Authorization": "Bearer alice", "Accept": "application/json"},
            )
        )
        await asyncio.wait_for(token_started.wait(), timeout=2.0)

        # Disconnect begins while callback is awaiting Google /token
        disc_task_a = asyncio.create_task(
            client.post(
                "/connect/disconnect",
                headers={"Authorization": "Bearer alice", "Accept": "application/json"},
            )
        )
        await asyncio.wait_for(revoke_started.wait(), timeout=2.0)

        # Now let the in-flight OAuth callback complete DURING revocation:
        # it MUST return 409 and MUST NOT emit a grant_token!
        allow_token_finish.set()
        cb_resp_a = await asyncio.wait_for(cb_task_a, timeout=2.0)
        assert cb_resp_a.status_code == 409
        assert cb_resp_a.json()["error"] == "disconnect_in_progress"
        assert "grant_token" not in cb_resp_a.json()

        # Finish revocation
        allow_revoke_finish.set()
        disc_resp_a = await asyncio.wait_for(disc_task_a, timeout=2.0)
        assert disc_resp_a.status_code == 200
        assert store.get_tenant("alice") is None

        # Even if a stale grant with an older disconnect_epoch were submitted after revocation
        # finishes, /connect/configure MUST reject it with 409!
        stale_grant_token = "stale-grant-old-epoch"
        store.save_oauth_pending(
            stale_grant_token,
            "alice",
            {
                "kind": "grant",
                "disconnect_epoch": 0,
                "credentials": _sample_creds("1//stale-token"),
                "direct_accounts": [{"id": "1111111111", "name": "Direct", "manager": False}],
                "mcc_children": {},
                "authorized_routes": {"1111111111": [""]},
            },
        )
        stale_cfg = await client.post(
            "/connect/configure",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
            json={"grant_token": stale_grant_token, "customers": ["1111111111"], "preset": "core"},
        )
        assert stale_cfg.status_code == 409
        assert stale_cfg.json()["error"] == "oauth_flow_superseded_by_disconnect"
        assert store.get_tenant("alice") is None

        # Variant B: Disconnect starts AND finishes completely while an OAuth callback is still mid-exchange.
        token_started.clear()
        allow_token_finish.clear()
        pause_token = True
        pause_revoke = False

        start_b = await client.get(
            "/connect/google/start",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert start_b.status_code == 200
        state_b = start_b.json()["state"]

        cb_task_b = asyncio.create_task(
            client.get(
                f"/connect/google/callback?code=code-b&state={state_b}",
                headers={"Authorization": "Bearer alice", "Accept": "application/json"},
            )
        )
        await asyncio.wait_for(token_started.wait(), timeout=2.0)

        # Disconnect runs and completes immediately while callback B is still awaiting /token
        disc_resp_b = await client.post(
            "/connect/disconnect",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert disc_resp_b.status_code == 200

        allow_token_finish.set()
        cb_resp_b = await asyncio.wait_for(cb_task_b, timeout=2.0)
        assert cb_resp_b.status_code == 409
        assert cb_resp_b.json()["error"] == "oauth_flow_superseded_by_disconnect"
        assert "grant_token" not in cb_resp_b.json()

        # Fresh OAuth flow started AFTER disconnect completes succeeds cleanly end-to-end!
        pause_token = False
        start_c = await client.get(
            "/connect/google/start",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert start_c.status_code == 200
        cb_c = await client.get(
            f"/connect/google/callback?code=code-c&state={start_c.json()['state']}",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
        )
        assert cb_c.status_code == 200
        grant_c = cb_c.json()["grant_token"]
        cfg_c = await client.post(
            "/connect/configure",
            headers={"Authorization": "Bearer alice", "Accept": "application/json"},
            json={"grant_token": grant_c, "customers": ["1111111111"], "preset": "core"},
        )
        assert cfg_c.status_code == 200
        assert store.get_tenant("alice") is not None


def test_cross_instance_sqlite_disconnect_epoch_is_atomic_for_grant_and_upsert(tmp_path):
    """Two separate EncryptedProfileStore instances sharing tenants.db must serialize
    epoch check + write inside SQLite BEGIN IMMEDIATE transactions."""
    master_key = Fernet.generate_key()
    state_dir = tmp_path / "shared_state"
    store_a = EncryptedProfileStore(state_dir, master_key)
    store_b = EncryptedProfileStore(state_dir, master_key)

    # ---- 1. save_oauth_pending (instance A) vs revoke_tenant (instance B) ----
    read_epoch_a = threading.Event()
    release_a = threading.Event()
    revoke_done_b = threading.Event()
    orig_get_epoch = store_a._get_disconnect_epoch_unlocked

    def paused_get_epoch(conn, subject: str) -> int:
        val = orig_get_epoch(conn, subject)
        read_epoch_a.set()
        release_a.wait(timeout=3.0)
        return val

    store_a._get_disconnect_epoch_unlocked = paused_get_epoch  # type: ignore[method-assign]

    errors: list[BaseException] = []

    def writer_a_grant() -> None:
        try:
            store_a.save_oauth_pending(
                "grant-cross-1",
                "alice",
                {"kind": "grant", "disconnect_epoch": 0, "credentials": _sample_creds("1//grant-0")},
                expected_disconnect_epoch=0,
            )
        except BaseException as exc:
            errors.append(exc)

    def revoker_b() -> None:
        try:
            store_b.revoke_tenant("alice")
        except BaseException as exc:
            errors.append(exc)
        finally:
            revoke_done_b.set()

    t_a = threading.Thread(target=writer_a_grant)
    t_a.start()
    assert read_epoch_a.wait(timeout=2.0)

    # Launch instance B's revoke while instance A is paused AFTER reading epoch=0!
    t_b = threading.Thread(target=revoker_b)
    t_b.start()
    # Because instance A holds SQLite's BEGIN IMMEDIATE write lock, instance B MUST block!
    time.sleep(0.15)
    assert not revoke_done_b.is_set()

    release_a.set()
    t_a.join(timeout=5.0)
    t_b.join(timeout=5.0)
    assert errors == []

    # Serializability invariant: B ran AFTER A committed, bumped epoch to 1, and wiped A's grant!
    assert store_b.get_disconnect_epoch("alice") == 1
    assert store_a.pop_oauth_pending("grant-cross-1") is None

    # Any further attempt to save an epoch-0 grant fails immediately with DisconnectEpochMismatchError
    store_a._get_disconnect_epoch_unlocked = orig_get_epoch  # type: ignore[method-assign]
    with pytest.raises(DisconnectEpochMismatchError):
        store_a.save_oauth_pending(
            "grant-cross-stale",
            "alice",
            {"kind": "grant", "disconnect_epoch": 0},
            expected_disconnect_epoch=0,
        )

    # ---- 2. upsert_tenant (instance A) vs revoke_tenant (instance B) ----
    read_epoch_a2 = threading.Event()
    release_a2 = threading.Event()
    revoke_done_b2 = threading.Event()
    revoked_creds_holder: list[dict | None] = []

    def paused_get_epoch_2(conn, subject: str) -> int:
        val = orig_get_epoch(conn, subject)
        read_epoch_a2.set()
        release_a2.wait(timeout=3.0)
        return val

    store_a._get_disconnect_epoch_unlocked = paused_get_epoch_2  # type: ignore[method-assign]

    def writer_a_upsert() -> None:
        try:
            store_a.upsert_tenant(
                subject="alice",
                credentials=_sample_creds("1//alice-upsert-race"),
                customers=["1111111111"],
                preset="core",
                expected_disconnect_epoch=1,
            )
        except BaseException as exc:
            errors.append(exc)

    def revoker_b2() -> None:
        try:
            revoked_creds_holder.append(store_b.revoke_tenant("alice"))
        except BaseException as exc:
            errors.append(exc)
        finally:
            revoke_done_b2.set()

    t_a2 = threading.Thread(target=writer_a_upsert)
    t_a2.start()
    assert read_epoch_a2.wait(timeout=2.0)

    t_b2 = threading.Thread(target=revoker_b2)
    t_b2.start()
    time.sleep(0.15)
    assert not revoke_done_b2.is_set()

    release_a2.set()
    t_a2.join(timeout=5.0)
    t_b2.join(timeout=5.0)
    assert errors == []

    # Instance B serialized strictly AFTER instance A: tenant is revoked, epoch is 2,
    # and B even received the newly written credentials for Google revocation!
    assert store_b.get_tenant("alice") is None
    assert store_b.get_disconnect_epoch("alice") == 2
    assert revoked_creds_holder and revoked_creds_holder[0] is not None
    assert revoked_creds_holder[0]["refresh_token"] == "1//alice-upsert-race"

    # Subsequent upsert with stale epoch=1 raises DisconnectEpochMismatchError
    store_a._get_disconnect_epoch_unlocked = orig_get_epoch  # type: ignore[method-assign]
    with pytest.raises(DisconnectEpochMismatchError):
        store_a.upsert_tenant(
            subject="alice",
            credentials=_sample_creds("1//alice-stale-upsert"),
            customers=["1111111111"],
            preset="core",
            expected_disconnect_epoch=1,
        )



