import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

jwt = pytest.importorskip("jwt")

from cryptography.hazmat.primitives.asymmetric import rsa

from agency.config import resolve_worker_file
from agency.remote_gateway import OAuthVerifier, RemoteConfig, create_remote_app, protect_worker


def make_config():
    return RemoteConfig(
        resource="https://ads.example.com/mcp", issuer="https://id.example.com",
        jwks_url="https://id.example.com/jwks",
        workers={"alice": "http://127.0.0.1:8101/mcp", "bob": "http://127.0.0.1:8102/mcp"},
        worker_key="testkey" * 8,
    )


def test_verifier_checks_signature_issuer_audience_expiry_and_scope(monkeypatch):
    config = make_config()
    verifier = OAuthVerifier(config)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(
        verifier.keys, "get_signing_key_from_jwt", lambda _: SimpleNamespace(key=key.public_key())
    )
    claims = {
        "sub": "alice", "iss": config.issuer, "aud": config.resource,
        "exp": int(time.time()) + 60, "scope": "mcp:access",
    }
    assert verifier._subject(jwt.encode(claims, key, algorithm="RS256")) == "alice"
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert verifier._subject(jwt.encode(claims, other_key, algorithm="RS256")) is None
    for override in (
        {"aud": "https://other.example.com/mcp"}, {"iss": "https://other.example.com"},
        {"exp": 0}, {"scope": "profile"}, {"sub": None},
    ):
        assert verifier._subject(jwt.encode({**claims, **override}, key, algorithm="RS256")) is None


def test_remote_subjects_cannot_share_a_worker_port():
    config = make_config()
    with pytest.raises(ValueError, match="distinct private"):
        replace(config, workers={**config.workers, "bob": "http://127.0.0.1:08101/mcp"})


@pytest.mark.asyncio
async def test_gateway_isolates_workers_and_forwards_tool_calls():
    config = make_config()

    class Verifier:
        async def subject(self, token):
            return token if token in {"alice", "bob", "unregistered"} else None

    async def worker(request):
        assert "authorization" not in request.headers
        assert request.headers["x-mcp-worker-key"] == config.worker_key
        return httpx.Response(200, json={"port": request.url.port, "request": request.content.decode()})

    app = create_remote_app(config, verifier=Verifier(), transport=httpx.MockTransport(worker))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://ads.example.com") as client:
        assert (await client.post("/mcp")).status_code == 401
        assert (await client.get("/.well-known/oauth-protected-resource/mcp")).json()["resource"] == config.resource
        assert (await client.post("/mcp", headers={"Authorization": "Bearer unregistered"})).status_code == 403
        assert (await client.post("/mcp", headers={"Origin": "https://other.example.com"})).status_code == 403

        async def call(subject):
            return await client.post("/mcp", headers={"Authorization": f"Bearer {subject}"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "pause_campaign", "arguments": {"dry_run": True}},
            })

        alice, bob = await asyncio.gather(call("alice"), call("bob"))
        assert alice.json()["port"] == 8101
        assert bob.json()["port"] == 8102
        assert "pause_campaign" in alice.json()["request"]


@pytest.mark.asyncio
async def test_worker_rejects_direct_requests_without_its_key():
    from starlette.responses import Response

    config = make_config()
    app = protect_worker(Response(status_code=204), config.worker_key)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
        assert (await client.post("/mcp")).status_code == 401
        assert (await client.post("/mcp", headers={"x-mcp-worker-key": config.worker_key})).status_code == 204


def test_remote_file_arguments_cannot_escape_profile_directory(tmp_path, monkeypatch):
    directory = tmp_path / "profile-files"
    monkeypatch.setenv("MCP_REMOTE_FILES_DIR", str(directory))
    assert resolve_worker_file(directory / "report.csv") == directory / "report.csv"
    with pytest.raises(ValueError, match="file directory"):
        resolve_worker_file(tmp_path / "profile.json")
