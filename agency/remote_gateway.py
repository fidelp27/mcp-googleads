"""Route authenticated Streamable HTTP requests to private MCP workers."""

from __future__ import annotations

import json
import os
from secrets import compare_digest
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

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
    workers: dict[str, str]
    worker_key: str = field(repr=False)
    allowed_origins: frozenset[str] = frozenset()

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
        if not self.workers:
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


def create_remote_app(config: RemoteConfig, *, verifier=None, transport=None) -> Starlette:
    verifier = verifier if verifier is not None else OAuthVerifier(config)
    resource = urlsplit(config.resource)
    metadata_path = "/.well-known/oauth-protected-resource/mcp"
    metadata_url = urlunsplit((resource.scheme, resource.netloc, metadata_path, "", ""))

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
        upstream = config.workers.get(subject)
        if upstream is None:
            return Response(status_code=403)
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
            return Response(status_code=502)

        async def body():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        response_headers = {
            key: response.headers[key] for key in _RESPONSE_HEADERS if key in response.headers
        }
        return StreamingResponse(body(), status_code=response.status_code, headers=response_headers)

    return Starlette(
        routes=[
            Route(metadata_path, metadata),
            Route("/.well-known/oauth-protected-resource", metadata),
            Route("/mcp", forward, methods=["GET", "POST", "DELETE"]),
        ]
    )


def main() -> None:
    import uvicorn

    config = RemoteConfig(
        resource=os.environ["MCP_REMOTE_RESOURCE_URL"],
        issuer=os.environ["MCP_REMOTE_ISSUER"],
        jwks_url=os.environ["MCP_REMOTE_JWKS_URL"],
        workers=json.loads(os.environ["MCP_REMOTE_WORKERS"]),
        worker_key=require_worker_key(),
        allowed_origins=frozenset(os.environ.get("MCP_REMOTE_ALLOWED_ORIGINS", "").split()),
    )
    uvicorn.run(create_remote_app(config), host="127.0.0.1", port=int(os.getenv("PORT", "8080")))
