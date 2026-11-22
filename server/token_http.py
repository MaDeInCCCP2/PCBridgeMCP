"""Local, URL-token-compatible Streamable HTTP adapter for PC Bridge.

For MCP clients that expose only a URL, use:
    http://127.0.0.1:8770/<TOKEN.txt contents>/mcp

The upstream PC Bridge remains protected by Authorization: Bearer.
This adapter binds only to loopback, never expose it through a tunnel.
"""
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.background import BackgroundTask

from mcp_protocol import MCP_REQUEST_LIMIT

ROOT = Path(__file__).resolve().parent.parent
PROXY_HOST = "127.0.0.1"
PROXY_PORT = int(os.environ.get("PCBRIDGE_TOKEN_HTTP_PORT", "8770"))
MAX_REQUEST_BYTES = MCP_REQUEST_LIMIT

# No credentials in access logs, errors, query strings or response bodies.
logging.getLogger("uvicorn.access").disabled = True
HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host",
    "content-length", "authorization", "cookie", "forwarded", "origin",
    "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
})
RETURN_HEADERS = frozenset({
    "content-type", "cache-control", "mcp-session-id", "mcp-protocol-version",
    "www-authenticate", "retry-after",
})


def current_token(root: Path) -> str:
    """Read at request time so rotations take effect without a proxy restart."""
    token = (root / "TOKEN.txt").read_text(encoding="utf-8-sig").strip()
    if not token or any(c.isspace() for c in token):
        raise ValueError("TOKEN.txt has no valid token")
    return token


def upstream_url(root: Path) -> str:
    cfg = json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
    port = int(cfg.get("port", 8765))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid PC Bridge port")
    # Use the existing header-auth endpoint; never weaken the main server.
    return f"http://127.0.0.1:{port}/mcp"


def make_app(root: Path = ROOT, transport=None) -> Starlette:
    root = Path(root)

    @asynccontextmanager
    async def lifespan(app):
        timeout = httpx.Timeout(connect=10, read=None, write=60, pool=10)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, transport=transport) as client:
            app.state.upstream_client = client
            yield

    async def proxy(request: Request):
        if request.method not in ("GET", "POST", "DELETE"):
            return Response(status_code=405)

        # The URL is a secret bearer credential, but only on localhost.
        origin = request.headers.get("origin")
        if origin and origin not in (
            f"http://127.0.0.1:{PROXY_PORT}",
            f"http://localhost:{PROXY_PORT}",
        ):
            return Response(status_code=403)

        try:
            token = current_token(root)
        except (OSError, ValueError):
            return JSONResponse({"error": "Token is not configured"}, status_code=503)

        supplied = request.path_params["token"]
        if not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
            return Response(status_code=401)

        incoming = {key: value for key, value in request.headers.items()
                    if key.lower() not in HOP_HEADERS and not key.lower().startswith("proxy-")}
        incoming["authorization"] = "Bearer " + token
        incoming["accept-encoding"] = "identity"
        try:
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > MAX_REQUEST_BYTES:
                    return Response(status_code=413)
            upstream_req = request.app.state.upstream_client.build_request(
                request.method, upstream_url(root), headers=incoming,
                content=body if request.method == "POST" else None,
            )
            upstream_response = await request.app.state.upstream_client.send(upstream_req, stream=True)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logging.getLogger("pcbridge.token_http").warning("Proxy request failed: %s", type(exc).__name__)
            return JSONResponse({"error": "PC Bridge backend is unavailable"}, status_code=502)

        response_headers = {key: value for key, value in upstream_response.headers.items()
                            if key.lower() in RETURN_HEADERS}
        return StreamingResponse(
            upstream_response.aiter_bytes(),
            status_code=upstream_response.status_code,
            headers=response_headers,
            background=BackgroundTask(upstream_response.aclose),
        )

    async def health(request: Request):
        # Local-only status; never include a token or configuration secrets.
        try:
            current_token(root)
            upstream_url(root)
        except (OSError, ValueError, json.JSONDecodeError):
            return JSONResponse({"ok": False}, status_code=503)
        return JSONResponse({"ok": True, "transport": "streamable-http", "auth": "url-token"})

    return Starlette(routes=[
        Route("/health", health, methods=["GET"]),
        Route("/{token}/mcp", proxy, methods=["GET", "POST", "DELETE"]),
        Route("/{token}/mcp/", proxy, methods=["GET", "POST", "DELETE"]),
    ], lifespan=lifespan)


app = make_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, access_log=False, log_level="warning")
