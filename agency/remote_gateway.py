"""Route authenticated Streamable HTTP requests to private MCP workers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
from secrets import compare_digest
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
import jwt
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


_REQUEST_HEADERS = (
    "accept", "content-type", "mcp-protocol-version", "last-event-id", "mcp-session-id",
)
_RESPONSE_HEADERS = ("content-type", "cache-control", "mcp-session-id")
_WORKER_HEADER = "x-mcp-worker-key"


def require_worker_key() -> str:
    key = os.environ.get("MCP_REMOTE_WORKER_KEY", "")
    if len(key) < 32 or not key.isascii() or not key.isalnum():
        raise ValueError("MCP_REMOTE_WORKER_KEY must be at least 32 ASCII alphanumeric characters.")
    return key


def protect_worker(app, key: str):
    async def protected(scope, receive, send):
        if scope["type"] == "http":
            provided = dict(scope["headers"]).get(_WORKER_HEADER.encode(), b"")
            if not compare_digest(provided, key.encode()):
                await Response(status_code=401)(scope, receive, send)
                return
        await app(scope, receive, send)

    return protected


@dataclass(frozen=True)
class RemoteConfig:
    resource: str
    issuer: str
    jwks_url: str
    workers: dict[str, str] = field(default_factory=dict)
    worker_key: str = field(default="", repr=False)
    allowed_origins: frozenset[str] = frozenset()
    self_serve: bool = False

    def __post_init__(self) -> None:
        for url in (self.resource, self.issuer, self.jwks_url):
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.query or parsed.fragment
            ):
                raise ValueError("Remote resource, issuer, and JWKS URLs must use HTTPS.")
        if urlsplit(self.resource).path != "/mcp":
            raise ValueError("MCP_REMOTE_RESOURCE_URL must end in /mcp.")
        if len(self.worker_key) < 32 or not self.worker_key.isascii() or not self.worker_key.isalnum():
            raise ValueError("Invalid worker authentication key.")
        if not self.workers and not self.self_serve:
            raise ValueError("Configure one distinct private MCP worker per OAuth subject.")
        worker_ports: set[int] = set()
        for subject, url in self.workers.items():
            parsed = urlsplit(url)
            port = parsed.port
            if (
                not isinstance(subject, str) or not subject or parsed.scheme != "http"
                or parsed.hostname != "127.0.0.1" or not port
                or parsed.username is not None or parsed.query or parsed.fragment
                or parsed.path != "/mcp"
            ):
                raise ValueError("Each worker must use http://127.0.0.1:<port>/mcp.")
            if port in worker_ports:
                raise ValueError("Configure one distinct private MCP worker per OAuth subject.")
            worker_ports.add(port)


class OAuthVerifier:
    def __init__(self, config: RemoteConfig) -> None:
        self.config = config
        self.keys = jwt.PyJWKClient(config.jwks_url, timeout=5)

    async def subject(self, token: str) -> str | None:
        return await run_in_threadpool(self._subject, token)

    def _subject(self, token: str) -> str | None:
        try:
            key = self.keys.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.config.resource,
                issuer=self.config.issuer,
                options={"require": ["iss", "aud", "exp", "sub"]},
            )
        except jwt.PyJWTError:
            return None
        scopes = claims.get("scope", "")
        return claims["sub"] if isinstance(scopes, str) and "mcp:access" in scopes.split() else None


def create_remote_app(
    config: RemoteConfig,
    *,
    verifier: Any = None,
    transport: Any = None,
    worker_pool: Any = None,
    store: Any = None,
    master_key: bytes | None = None,
    google_oauth: Any = None,
    oauth_adapter: Any = None,
    portal_oidc: Any = None,
    portal_oidc_adapter: Any = None,
    eviction_interval_seconds: float = 60.0,
) -> Starlette:
    verifier = verifier if verifier is not None else OAuthVerifier(config)
    resource = urlsplit(config.resource)
    metadata_path = "/.well-known/oauth-protected-resource/mcp"
    metadata_url = urlunsplit((resource.scheme, resource.netloc, metadata_path, "", ""))
    connect_base_url = urlunsplit((resource.scheme, resource.netloc, "/connect", "", ""))

    async def metadata(_request: Request) -> Response:
        return JSONResponse(
            {
                "resource": config.resource,
                "authorization_servers": [config.issuer],
                "scopes_supported": ["mcp:access"],
                "bearer_methods_supported": ["header"],
            }
        )

    async def forward(request: Request) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and origin not in config.allowed_origins:
            return Response(status_code=403)
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        subject = await verifier.subject(token) if scheme.lower() == "bearer" and token else None
        if subject is None:
            return Response(
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{metadata_url}", scope="mcp:access"'
                },
            )
        worker_handle = None
        upstream = config.workers.get(subject)
        if upstream is None and worker_pool is not None:
            from .remote_store import PoolCapacityError

            try:
                if hasattr(worker_pool, "acquire_worker_handle"):
                    worker_handle = await worker_pool.acquire_worker_handle(subject)
                    upstream = worker_handle.url if worker_handle is not None else None
                else:
                    upstream = await worker_pool.resolve_worker_url(subject)
            except PoolCapacityError:
                return JSONResponse(
                    {
                        "error": "worker_pool_busy",
                        "message": "Server worker capacity is temporarily busy. Retry shortly.",
                    },
                    status_code=503,
                    headers={"Retry-After": "2"},
                )
            except RuntimeError:
                return JSONResponse({"error": "worker_startup_failed"}, status_code=502)
        if upstream is None:
            if config.self_serve or worker_pool is not None:
                connect_url = connect_base_url
                if store is not None and hasattr(store, "save_oauth_pending"):
                    ticket = secrets.token_urlsafe(32)
                    store.save_oauth_pending(
                        ticket,
                        subject,
                        {"kind": "portal_ticket"},
                        ttl_seconds=600,
                    )
                    connect_url = urlunsplit(
                        (
                            resource.scheme,
                            resource.netloc,
                            "/connect/ticket",
                            urlencode({"t": ticket}),
                            "",
                        )
                    )
                return JSONResponse(
                    {
                        "error": "google_ads_not_connected",
                        "message": "Open connect_url in your browser to connect your Google Ads account.",
                        "connect_url": connect_url,
                    },
                    status_code=403,
                )
            return Response(status_code=403)

        async def _release_worker() -> None:
            nonlocal worker_handle
            if worker_handle is not None and worker_pool is not None:
                handle = worker_handle
                worker_handle = None
                await worker_pool.release_worker_handle(handle)

        headers = {key: request.headers[key] for key in _REQUEST_HEADERS if key in request.headers}
        headers[_WORKER_HEADER] = config.worker_key
        client = httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False, timeout=60
        )
        try:
            outgoing = client.build_request(
                request.method, upstream, headers=headers,
                content=request.stream() if request.method == "POST" else None,
            )
            response = await client.send(outgoing, stream=True)
        except httpx.RequestError:
            await client.aclose()
            await _release_worker()
            return Response(status_code=502)
        except BaseException:
            await client.aclose()
            await _release_worker()
            raise

        async def body():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()
                await _release_worker()

        response_headers = {
            key: response.headers[key] for key in _RESPONSE_HEADERS if key in response.headers
        }
        return StreamingResponse(body(), status_code=response.status_code, headers=response_headers)

    routes: list[Route] = [
        Route(metadata_path, metadata),
        Route("/.well-known/oauth-protected-resource", metadata),
        Route("/mcp", forward, methods=["GET", "POST", "DELETE"]),
    ]
    if store is not None and worker_pool is not None and master_key is not None and google_oauth is not None:
        from .remote_onboarding import create_onboarding_routes

        routes.extend(
            create_onboarding_routes(
                resource_url=config.resource,
                verifier=verifier,
                store=store,
                pool=worker_pool,
                master_key=master_key,
                google_oauth=google_oauth,
                oauth_adapter=oauth_adapter,
                portal_oidc=portal_oidc,
                portal_oidc_adapter=portal_oidc_adapter,
            )
        )

    @asynccontextmanager
    async def lifespan(_app: Starlette):
        reaper: asyncio.Task[Any] | None = None
        if worker_pool is not None:
            async def _reap_loop() -> None:
                while True:
                    await asyncio.sleep(max(1.0, eviction_interval_seconds))
                    await worker_pool.evict_idle_workers()

            reaper = asyncio.create_task(_reap_loop())
        try:
            yield
        finally:
            if reaper is not None:
                reaper.cancel()
                try:
                    await reaper
                except asyncio.CancelledError:
                    pass
            if worker_pool is not None:
                await worker_pool.shutdown_all()

    return Starlette(routes=routes, lifespan=lifespan)


def main() -> None:
    import uvicorn

    worker_key = require_worker_key()
    state_dir_raw = os.environ.get("MCP_REMOTE_STATE_DIR", "").strip()
    workers_raw = os.environ.get("MCP_REMOTE_WORKERS", "").strip()
    workers = json.loads(workers_raw) if workers_raw else {}
    self_serve = bool(state_dir_raw)

    config = RemoteConfig(
        resource=os.environ["MCP_REMOTE_RESOURCE_URL"],
        issuer=os.environ["MCP_REMOTE_ISSUER"],
        jwks_url=os.environ["MCP_REMOTE_JWKS_URL"],
        workers=workers,
        worker_key=worker_key,
        allowed_origins=frozenset(os.environ.get("MCP_REMOTE_ALLOWED_ORIGINS", "").split()),
        self_serve=self_serve,
    )

    store = None
    pool = None
    master_key = None
    google_oauth = None
    portal_oidc = None
    if self_serve:
        from .remote_onboarding import GoogleWebOAuthConfig, PortalOIDCConfig
        from .remote_store import DynamicWorkerPool, EncryptedProfileStore, require_master_key

        master_key = require_master_key()
        store = EncryptedProfileStore(Path(state_dir_raw), master_key)
        google_oauth = GoogleWebOAuthConfig.from_env(resource_url=config.resource)
        pool = DynamicWorkerPool(
            store=store,
            worker_key=worker_key,
            master_key=master_key,
            allow_user_writes=google_oauth.allow_user_writes,
            idle_timeout_seconds=float(os.environ.get("MCP_REMOTE_IDLE_TIMEOUT_SECONDS", "900")),
            max_active_workers=int(os.environ.get("MCP_REMOTE_MAX_WORKERS", "50")),
        )
        portal_oidc = PortalOIDCConfig.from_env(issuer=config.issuer, resource_url=config.resource)

    app = create_remote_app(
        config,
        worker_pool=pool,
        store=store,
        master_key=master_key,
        google_oauth=google_oauth,
        portal_oidc=portal_oidc,
    )
    # Disable default uvicorn access_log so single-use ?t=<ticket> and ?code=... query strings never hit logs.
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.getenv("PORT", "8080")),
        access_log=False,
    )
