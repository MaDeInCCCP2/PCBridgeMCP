"""Validate the effective HTTP authority before OAuth or MCP handles a request."""
import json
import re
from urllib.parse import urlsplit

from mcp_protocol import MCP_REQUEST_LIMIT


_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?|\[::1\])(?::[0-9]{1,5})?", re.ASCII)
PROXY_HEADERS = {"forwarded", "x-original-host", "x-real-ip", "ngrok-agent-ips"}


def request_headers(scope):
    return {name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope.get("headers", [])}


def public_origin(value):
    """Public endpoints must be explicitly configured HTTPS origins."""
    value = str(value or "").strip().lower()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    try:
        url = urlsplit(value)
        if (url.scheme == "https" and _HOST.fullmatch(url.netloc)
                and url.path in ("", "/") and not (url.query or url.fragment)
                and (url.port is None or 1 <= url.port <= 65535)):
            authority = url.netloc.removesuffix(":443") if url.port == 443 else url.netloc
            return "https://" + authority
    except ValueError:
        pass
    raise ValueError("PCBridge public origin must be an HTTPS origin")


async def send_json(send, data, status=200, extra_headers=()):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
        *extra_headers,
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class HTTPPolicy:
    def __init__(self, app, port, configured_origin="", tunnel_file=None, transport_security=None):
        self.app = app
        self.port = port
        self.configured_origin = public_origin(configured_origin)
        self.tunnel_file = tunnel_file
        self.transport_security = transport_security
        self.local_hosts = {f"{host}:{port}" for host in ("localhost", "127.0.0.1", "[::1]")}
        if port == 80:
            self.local_hosts.update(("localhost", "127.0.0.1", "[::1]"))

    def origins(self):
        by_host = {host: f"http://{host}" for host in self.local_hosts}
        try:
            tunnel = public_origin(self.tunnel_file.read_text(encoding="utf-8")) if self.tunnel_file else ""
        except (OSError, ValueError):
            tunnel = ""
        for origin in (self.configured_origin, tunnel):
            if origin:
                by_host.setdefault(origin.removeprefix("https://"), origin)
        return by_host

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        raw_headers = [(k.lower(), v) for k, v in scope.get("headers", [])]
        hosts = [v.decode("latin-1").lower() for k, v in raw_headers if k == b"host"]
        origins = self.origins()
        if len(hosts) != 1 or hosts[0] not in origins:
            return await send_json(send, {"error": "invalid_host"}, 421)

        base = origins[hosts[0]]
        browser_origins = [v.decode("latin-1") for k, v in raw_headers if k == b"origin"]
        if browser_origins and browser_origins != [base]:
            return await send_json(send, {"error": "invalid_origin"}, 403)

        headers = []
        for key, value in raw_headers:
            name = key.decode("latin-1")
            if name.startswith("x-forwarded-") or name in PROXY_HEADERS:
                continue
            headers.append((key, hosts[0].encode("ascii") if name == "host" else value))

        # The SDK performs a second check using the same locally sourced allowlist.
        if self.transport_security is not None:
            self.transport_security.allowed_hosts[:] = origins
            self.transport_security.allowed_origins[:] = origins.values()
        scope = dict(scope, headers=headers, pcbridge_origin=base)
        if scope.get("method") != "POST":
            return await self.app(scope, receive, send)

        limit = 64 * 1024 if scope.get("path", "").startswith("/oauth/") else MCP_REQUEST_LIMIT
        lengths = [v for k, v in headers if k == b"content-length"]
        if lengths:
            if len(lengths) != 1 or not lengths[0].isdigit():
                return await send_json(send, {"error": "invalid_content_length"}, 400)
            if len(lengths[0]) > 12 or int(lengths[0]) > limit:
                return await send_json(send, {"error": "request_too_large"}, 413)

        # Enforce the same limit for chunked uploads before any handler has side effects.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                return await send_json(send, {"error": "request_too_large"}, 413)
            if not message.get("more_body"):
                break

        pending = True

        async def buffered_receive():
            nonlocal pending
            if pending:
                pending = False
                return {"type": "http.request", "body": bytes(body)}
            return await receive()

        return await self.app(scope, buffered_receive, send)
