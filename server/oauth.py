"""
PC Bridge OAuth 2.1 Provider (RFC 8414, RFC 7591, RFC 7636, RFC 6749)
Enables seamless integration with modern MCP clients (Tembo, Claude, Cursor, Codex, etc.)
supporting:
  * Dynamic Client Registration (RFC 7591)
  * Authorization Code Grant with PKCE S256 (RFC 7636)
  * Client Credentials Grant (M2M)
  * OAuth 2.0 Authorization Server Metadata (RFC 8414)
  * OAuth 2.0 Protected Resource Metadata (RFC 9728)
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.parse

log = logging.getLogger("pcbridge.oauth")

# In-memory storage for registered clients and authorization codes
CLIENTS = {
    # Default pre-registered client for convenience
    "pcbridge": {
        "client_name": "PC Bridge Client",
        "client_secret": None,  # Will match server master secret
        "redirect_uris": [],
        "created_at": time.time(),
    }
}
AUTH_CODES = {}  # code -> {client_id, redirect_uri, code_challenge, code_challenge_method, expires_at}


def verify_pkce(code_verifier: str, code_challenge: str, method: str = "S256") -> bool:
    if not code_challenge:
        return True
    if not code_verifier:
        return False
    method = (method or "S256").upper()
    if method == "PLAIN":
        return hmac.compare_digest(code_verifier, code_challenge)
    elif method == "S256":
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return hmac.compare_digest(computed, code_challenge.rstrip("="))
    return False


async def read_asgi_body(receive) -> bytes:
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.request":
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        elif message["type"] == "http.disconnect":
            break
    return bytes(body)


async def send_json(send, data: dict, status: int = 200, extra_headers: list = None):
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"access-control-allow-origin", b"*"),
        (b"access-control-allow-headers", b"*"),
        (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
        (b"cache-control", b"no-store"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def send_html(send, html: str, status: int = 200):
    body = html.encode("utf-8")
    headers = [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def send_redirect(send, url: str):
    headers = [
        (b"location", url.encode("latin-1")),
        (b"content-length", b"0"),
    ]
    await send({"type": "http.response.start", "status": 302, "headers": headers})
    await send({"type": "http.response.body", "body": b""})


class OAuthProvider:
    """ASGI middleware handling OAuth 2.1 metadata, registration, authorization, and token endpoints."""

    def __init__(self, app, secret: str, cfg: dict = None):
        self.app = app
        self.secret = secret
        self.cfg = cfg or {}

    def get_base_url(self, scope, headers: dict) -> str:
        # 1. Prefer X-Forwarded-Host or X-Original-Host
        host = headers.get("x-forwarded-host") or headers.get("x-original-host")
        # 2. If request comes through ngrok tunnel, use ngrok_domain from config
        if not host:
            ngrok_dom = str(self.cfg.get("ngrok_domain") or "").strip()
            if ngrok_dom and (headers.get("x-forwarded-proto") == "https" or headers.get("ngrok-agent-ips")):
                host = ngrok_dom.replace("https://", "").replace("http://", "").rstrip("/")
        # 3. Fallback to Host header or scope
        if not host:
            host = headers.get("host")
        if not host:
            server = scope.get("server")
            host = f"{server[0]}:{server[1]}" if server else "localhost:8765"

        proto = headers.get("x-forwarded-proto") or ("https" if "ngrok" in host or ":443" in host else "http")
        return f"{proto}://{host}"

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        base_url = self.get_base_url(scope, headers)

        # CORS preflight
        if method == "OPTIONS":
            await send({"type": "http.response.start", "status": 204, "headers": [
                (b"access-control-allow-origin", b"*"),
                (b"access-control-allow-headers", b"*"),
                (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
            ]})
            await send({"type": "http.response.body", "body": b""})
            return

        # 1. OAuth Server Metadata (RFC 8414 / OpenID Connect)
        if path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
            metadata = {
                "issuer": base_url,
                "authorization_endpoint": f"{base_url}/oauth/authorize",
                "token_endpoint": f"{base_url}/oauth/token",
                "registration_endpoint": f"{base_url}/oauth/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "client_credentials"],
                "code_challenge_methods_supported": ["S256", "plain"],
                "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic", "none"],
                "scopes_supported": ["mcp"],
            }
            return await send_json(send, metadata)

        # 2. Protected Resource Metadata (RFC 9728)
        if path == "/.well-known/oauth-protected-resource":
            prm = {
                "resource": base_url,
                "authorization_servers": [base_url],
                "scopes_supported": ["mcp"],
                "bearer_methods_supported": ["header"],
            }
            return await send_json(send, prm)

        # 3. Dynamic Client Registration (RFC 7591)
        if path == "/oauth/register" and method == "POST":
            raw_body = await read_asgi_body(receive)
            try:
                reg_data = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except Exception:
                reg_data = {}

            client_id = f"pcbridge-{secrets.token_hex(8)}"
            client_secret = secrets.token_hex(24)
            client_name = reg_data.get("client_name") or "MCP Client"
            redirect_uris = reg_data.get("redirect_uris") or []

            CLIENTS[client_id] = {
                "client_name": client_name,
                "client_secret": client_secret,
                "redirect_uris": redirect_uris,
                "created_at": time.time(),
            }
            log.info("OAuth registered client %s (%s)", client_id, client_name)

            resp = {
                "client_id": client_id,
                "client_secret": client_secret,
                "client_name": client_name,
                "redirect_uris": redirect_uris,
                "grant_types": ["authorization_code", "client_credentials"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
            }
            return await send_json(send, resp, status=201)

        # 4. Authorization Endpoint (/oauth/authorize)
        if path == "/oauth/authorize":
            query_string = scope.get("query_string", b"").decode("latin-1")
            query = urllib.parse.parse_qs(query_string)

            if method == "GET":
                client_id = query.get("client_id", [""])[0]
                redirect_uri = query.get("redirect_uri", [""])[0]
                state = query.get("state", [""])[0]
                code_challenge = query.get("code_challenge", [""])[0]
                code_challenge_method = query.get("code_challenge_method", ["S256"])[0]
                scope_val = query.get("scope", ["mcp"])[0]

                client_info = CLIENTS.get(client_id, {})
                client_name = client_info.get("client_name") or "Tembo / MCP Client"

                html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PC Bridge — Авторизация подключения</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f1115;
      color: #e4e4e7;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      margin: 0;
      padding: 16px;
    }}
    .card {{
      background: #181920;
      border: 1px solid #272832;
      border-radius: 14px;
      padding: 32px;
      max-width: 440px;
      width: 100%;
      box-shadow: 0 10px 40px rgba(0,0,0,0.6);
    }}
    .badge {{
      background: #1e293b;
      color: #38bdf8;
      padding: 4px 10px;
      border-radius: 6px;
      font-weight: 600;
      font-size: 13px;
    }}
    h2 {{
      margin: 0 0 12px 0;
      color: #ffffff;
      font-size: 22px;
    }}
    p {{
      color: #a1a1aa;
      font-size: 14px;
      line-height: 1.5;
      margin: 0 0 24px 0;
    }}
    .btn {{
      display: block;
      width: 100%;
      padding: 12px;
      background: #22c55e;
      color: #0b2e13;
      border: none;
      border-radius: 8px;
      font-weight: 700;
      font-size: 15px;
      cursor: pointer;
      text-align: center;
      text-decoration: none;
      transition: background 0.15s ease;
    }}
    .btn:hover {{ background: #16a34a; color: #fff; }}
    .btn-secondary {{
      background: transparent;
      color: #71717a;
      border: 1px solid #272832;
      margin-top: 10px;
    }}
    .btn-secondary:hover {{ background: #272832; color: #e4e4e7; }}
  </style>
</head>
<body>
  <div class="card">
    <h2>Подключение MCP</h2>
    <p>Сервис <span class="badge">{client_name}</span> запрашивает доступ к инструментам <strong>PC Bridge</strong> на вашем компьютере.</p>
    <form method="POST" action="/oauth/authorize">
      <input type="hidden" name="client_id" value="{client_id}">
      <input type="hidden" name="redirect_uri" value="{redirect_uri}">
      <input type="hidden" name="state" value="{state}">
      <input type="hidden" name="code_challenge" value="{code_challenge}">
      <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
      <input type="hidden" name="scope" value="{scope_val}">
      <button type="submit" class="btn">Разрешить доступ</button>
      <a href="{redirect_uri}?error=access_denied&state={state}" class="btn btn-secondary">Отклонить</a>
    </form>
  </div>
</body>
</html>"""
                return await send_html(send, html)

            elif method == "POST":
                raw_body = await read_asgi_body(receive)
                form_data = urllib.parse.parse_qs(raw_body.decode("utf-8", errors="replace"))

                client_id = form_data.get("client_id", [""])[0] or query.get("client_id", [""])[0]
                redirect_uri = form_data.get("redirect_uri", [""])[0] or query.get("redirect_uri", [""])[0]
                state = form_data.get("state", [""])[0] or query.get("state", [""])[0]
                code_challenge = form_data.get("code_challenge", [""])[0] or query.get("code_challenge", [""])[0]
                code_challenge_method = form_data.get("code_challenge_method", ["S256"])[0] or query.get("code_challenge_method", ["S256"])[0]

                if not redirect_uri:
                    return await send_json(send, {"error": "invalid_request", "error_description": "missing redirect_uri"}, status=400)

                # Issue authorization code
                code = secrets.token_urlsafe(32)
                AUTH_CODES[code] = {
                    "client_id": client_id,
                    "redirect_uri": redirect_uri,
                    "code_challenge": code_challenge,
                    "code_challenge_method": code_challenge_method,
                    "expires_at": time.time() + 600,
                }
                log.info("OAuth issued authorization code for client %s", client_id)

                sep = "&" if "?" in redirect_uri else "?"
                target = f"{redirect_uri}{sep}code={code}"
                if state:
                    target += f"&state={urllib.parse.quote(state)}"
                return await send_redirect(send, target)

        # 5. Token Endpoint (/oauth/token)
        if path == "/oauth/token" and method == "POST":
            raw_body = await read_asgi_body(receive)
            params = {}

            # Parse JSON or form data
            content_type = headers.get("content-type", "").lower()
            if "application/json" in content_type:
                try:
                    params = json.loads(raw_body.decode("utf-8"))
                except Exception:
                    params = {}
            else:
                parsed = urllib.parse.parse_qs(raw_body.decode("utf-8", errors="replace"))
                params = {k: v[0] for k, v in parsed.items()}

            # Basic Auth header fallback for client_id / client_secret
            auth_hdr = headers.get("authorization", "")
            if auth_hdr.lower().startswith("basic "):
                try:
                    decoded = base64.b64decode(auth_hdr[6:].strip()).decode("utf-8")
                    if ":" in decoded:
                        b_id, b_sec = decoded.split(":", 1)
                        params.setdefault("client_id", b_id)
                        params.setdefault("client_secret", b_sec)
                except Exception:
                    pass

            grant_type = params.get("grant_type", "")

            # 5a. Authorization Code Grant
            if grant_type == "authorization_code":
                code = params.get("code", "")
                code_verifier = params.get("code_verifier", "")
                auth_entry = AUTH_CODES.pop(code, None)

                if not auth_entry or time.time() > auth_entry.get("expires_at", 0):
                    return await send_json(send, {"error": "invalid_grant", "error_description": "Invalid or expired authorization code"}, status=400)

                expected_challenge = auth_entry.get("code_challenge")
                method = auth_entry.get("code_challenge_method", "S256")
                if expected_challenge:
                    if not verify_pkce(code_verifier, expected_challenge, method):
                        log.warning("PKCE verification failed for code %s", code[:8])
                        return await send_json(send, {"error": "invalid_grant", "error_description": "PKCE verification failed"}, status=400)

                log.info("OAuth token issued via authorization_code for client %s", auth_entry.get("client_id"))
                return await send_json(send, {
                    "access_token": self.secret,
                    "token_type": "Bearer",
                    "expires_in": 315360000,
                    "scope": "mcp",
                })

            # 5b. Client Credentials Grant
            elif grant_type == "client_credentials":
                client_id = params.get("client_id", "")
                client_secret = params.get("client_secret", "")

                valid = False
                # Master secret always valid as client_secret
                if client_secret and hmac.compare_digest(client_secret, self.secret):
                    valid = True
                # Dynamically registered client secret
                elif client_id in CLIENTS and CLIENTS[client_id].get("client_secret"):
                    if hmac.compare_digest(client_secret, CLIENTS[client_id]["client_secret"]):
                        valid = True
                # Default client name pcbridge accepts master secret
                elif client_id in ("pcbridge", "") and client_secret and hmac.compare_digest(client_secret, self.secret):
                    valid = True

                if not valid:
                    log.warning("OAuth client_credentials rejected for client_id=%s", client_id)
                    return await send_json(send, {"error": "invalid_client", "error_description": "Invalid client_id or client_secret"}, status=401)

                log.info("OAuth token issued via client_credentials for client %s", client_id)
                return await send_json(send, {
                    "access_token": self.secret,
                    "token_type": "Bearer",
                    "expires_in": 315360000,
                    "scope": "mcp",
                })

            else:
                return await send_json(send, {"error": "unsupported_grant_type", "error_description": f"Unsupported grant_type: {grant_type}"}, status=400)

        # Pass through to next ASGI handler (TokenAuth / FastMCP)
        return await self.app(scope, receive, send)
