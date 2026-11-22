"""OAuth authorization-code flow for the PCBridge Streamable HTTP endpoint."""
import base64
import binascii
import hashlib
import hmac
import html
import ipaddress
import json
import re
import secrets
import time
from urllib.parse import parse_qs, unquote_plus, urlencode, urlsplit

from http_policy import request_headers, send_json
from oauth_store import digest


PKCE = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)
VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}", re.ASCII)
CLIENT_METHODS = ("none", "client_secret_post", "client_secret_basic")
TOKEN_TTL = 3600


def verify_pkce(verifier, challenge, method="S256"):
    if method != "S256" or not VERIFIER.fullmatch(verifier) or not PKCE.fullmatch(challenge):
        return False
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode()[:-1]
    return hmac.compare_digest(expected, challenge)


def valid_redirect(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        return False
    if any(not 33 <= ord(c) <= 126 or c in '\\<>"#' for c in value):
        return False
    try:
        url = urlsplit(value)
        if not url.hostname or url.username is not None or url.port == 0:
            return False
        if ":" in url.hostname:
            ipaddress.IPv6Address(url.hostname)
        elif not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", url.hostname):
            return False
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("127.0.0.1", "::1", "localhost")):
            return False
        return not {"code", "state", "error", "iss"}.intersection(parse_qs(url.query, keep_blank_values=True))
    except ValueError:
        return False


def callback(uri, **params):
    return uri + ("&" if "?" in uri else "?") + urlencode(params)


async def read_body(receive):
    parts = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise ValueError("disconnected")
        parts.append(message.get("body", b""))
        if not message.get("more_body"):
            return b"".join(parts).decode("utf-8")


def form(text):
    result = parse_qs(text, keep_blank_values=True, max_num_fields=32, errors="strict")
    if any(len(values) != 1 for values in result.values()):
        raise ValueError("duplicate form field")
    return {field: values[0] for field, values in result.items()}


async def send_html(send, page, redirect_uri):
    redirect = urlsplit(redirect_uri)
    # Chromium applies form-action to a redirect after submission as well.
    form_targets = ("form-action 'self' " + redirect.scheme + "://" + redirect.netloc).encode("ascii")
    body = page.encode("utf-8")
    await send({"type": "http.response.start", "status": 200, "headers": [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"content-security-policy",
         b"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; " + form_targets),
    ]})
    await send({"type": "http.response.body", "body": body})


async def send_redirect(send, url):
    await send({"type": "http.response.start", "status": 303, "headers": [
        (b"location", url.encode("ascii")), (b"content-length", b"0"),
        (b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer"),
    ]})
    await send({"type": "http.response.body", "body": b""})


def consent_html(client_name, origin, nonce, denied):
    client_name, origin, nonce, denied = (html.escape(value, quote=True)
                                         for value in (client_name, origin, nonce, denied))
    return f"""<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PC Bridge — подключение клиента</title>
<style>
  :root {{ color-scheme: light dark; font: 16px 'Segoe UI', sans-serif; }}
  body {{ margin: 8vh auto; max-width: 32rem; padding: 0 1rem; }}
  label {{ display: block; margin-bottom: .5rem; }}
  input {{ box-sizing: border-box; width: 100%; padding: .65rem; font: inherit; }}
  button, a {{ display: inline-block; margin: 1rem 1rem 0 0; font: inherit; }}
</style>
<h1>Подключение к PC Bridge</h1>
<p>Клиент <strong>{client_name}</strong> запрашивает доступ ко всем включённым MCP-инструментам.</p>
<p>Подтвердите подключение токеном владельца для <strong>{origin}</strong>.</p>
<form method="post" action="/oauth/authorize">
  <input type="hidden" name="request_id" value="{nonce}">
  <label for="owner-token">Токен из TOKEN.txt</label>
  <input id="owner-token" name="owner_token" type="password" autocomplete="off" required>
  <button type="submit">Разрешить доступ</button>
  <a href="{denied}">Отклонить</a>
</form>
</html>"""


class OAuthProvider:
    def __init__(self, app, secret, store, clock=time.time):
        self.app = app
        self.secret = secret.encode()
        self.store = store
        self.clock = clock
        self.pending = {}
        self.codes = {}

    @staticmethod
    def remember(records, ttl, now, data):
        for key, (expires, _) in list(records.items()):
            if expires <= now:
                records.pop(key)
        if len(records) >= 512:
            return None
        key = secrets.token_urlsafe(32)
        records[digest(key)] = (now + ttl, data)
        return key

    def lookup(self, records, key):
        if not key or len(key) != 43:
            return None, None
        hashed = digest(key)
        expires, data = records.get(hashed, (0, None))
        return hashed, data if expires > self.clock() else None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        method = scope.get("method")
        base = scope["pcbridge_origin"]
        resource = base + "/mcp"
        if method == "GET" and path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
            return await send_json(send, {
                "issuer": base, "authorization_endpoint": base + "/oauth/authorize",
                "token_endpoint": base + "/oauth/token", "registration_endpoint": base + "/oauth/register",
                "revocation_endpoint": base + "/oauth/revoke",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "client_credentials"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": list(CLIENT_METHODS),
                "revocation_endpoint_auth_methods_supported": list(CLIENT_METHODS),
                "authorization_response_iss_parameter_supported": True, "scopes_supported": ["mcp"],
            })
        if method == "GET" and path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
            return await send_json(send, {"resource": resource, "authorization_servers": [base],
                                          "scopes_supported": ["mcp"], "bearer_methods_supported": ["header"]})
        if method == "GET" and path == "/.well-known/pcbridge-ping":
            return await send_json(send, {"ok": True})

        headers = request_headers(scope)
        try:
            if path == "/oauth/register" and method == "POST":
                return await self.register(send, json.loads(await read_body(receive)))
            if path == "/oauth/authorize" and method == "GET":
                if len(scope["query_string"]) > 8192:
                    raise ValueError("query is too large")
                return await self.authorize(send, base, form(scope["query_string"].decode("ascii")))
            if method == "POST" and path in ("/oauth/authorize", "/oauth/token", "/oauth/revoke"):
                body = await read_body(receive)
                if path == "/oauth/authorize":
                    return await self.approve(send, base, form(body))
                params = json.loads(body) if headers.get("content-type", "").split(";")[0] == "application/json" else form(body)
                if not isinstance(params, dict) or any(not isinstance(value, str) for value in params.values()):
                    raise ValueError("invalid parameters")
                authorization = headers.get("authorization", "")
                if path == "/oauth/revoke":
                    return await self.revoke(send, params, authorization)
                return await self.token(send, resource, params, authorization)
        except (ValueError, UnicodeError, binascii.Error):
            return await send_json(send, {"error": "invalid_request"}, 400)
        return await self.app(scope, receive, send)

    async def register(self, send, params):
        if not isinstance(params, dict):
            raise ValueError("invalid registration")
        name = params.get("client_name", "MCP Client")
        uris = params.get("redirect_uris", [])
        method = params.get("token_endpoint_auth_method", "client_secret_basic")
        if (not isinstance(name, str) or not 1 <= len(name) <= 200
                or not isinstance(uris, list) or not 1 <= len(uris) <= 10
                or not all(valid_redirect(uri) for uri in uris) or method not in CLIENT_METHODS):
            return await send_json(send, {"error": "invalid_client_metadata"}, 400)
        if params.get("grant_types", ["authorization_code"]) != ["authorization_code"]:
            return await send_json(send, {"error": "invalid_client_metadata"}, 400)
        registered = self.store.register(name, uris, method)
        if registered is None:
            return await send_json(send, {"error": "temporarily_unavailable"}, 503)
        client_id, secret = registered
        result = {"client_id": client_id, "client_name": name, "redirect_uris": uris,
                  "grant_types": ["authorization_code"], "response_types": ["code"], "token_endpoint_auth_method": method}
        if secret:
            result.update(client_secret=secret, client_secret_expires_at=0)
        return await send_json(send, result, 201)

    async def authorize(self, send, base, params):
        client_id = params.get("client_id", "")
        client = self.store.client(client_id)
        uri = params.get("redirect_uri")
        challenge = params.get("code_challenge", "")
        resource = base + "/mcp"
        if (not client or uri not in client["uris"] or params.get("response_type") != "code"
                or params.get("code_challenge_method") != "S256" or not PKCE.fullmatch(challenge)
                or params.get("scope", "mcp") != "mcp" or params.get("resource", resource) != resource):
            return await send_json(send, {"error": "invalid_request"}, 400)
        state = {"state": params["state"]} if "state" in params else {}
        nonce = self.remember(self.pending, 300, self.clock(), (client_id, uri, state, challenge, resource))
        if nonce is None:
            return await send_json(send, {"error": "temporarily_unavailable"}, 503)
        return await send_html(send, consent_html(client["name"], base, nonce,
                                                   callback(uri, error="access_denied", iss=base, **state)), uri)

    async def approve(self, send, base, params):
        hashed, entry = self.lookup(self.pending, params.get("request_id"))
        if not entry or entry[4] != base + "/mcp":
            return await send_json(send, {"error": "invalid_request"}, 400)
        if not hmac.compare_digest(params.get("owner_token", "").encode(), self.secret):
            return await send_json(send, {"error": "owner_approval_required"}, 403)
        client_id, uri, state, challenge, resource = entry
        self.pending.pop(hashed)
        client = self.store.client(client_id)
        if not client or uri not in client["uris"]:
            return await send_json(send, {"error": "invalid_client"}, 400)
        code = self.remember(self.codes, 120, self.clock(), (client_id, uri, challenge, resource))
        if not code:
            return await send_json(send, {"error": "temporarily_unavailable"}, 503)
        return await send_redirect(send, callback(uri, code=code, iss=base, **state))

    def authenticate_client(self, params, authorization):
        client_id = params.get("client_id", "")
        secret = params.get("client_secret", "")
        method = "client_secret_post" if "client_secret" in params else "none"
        if authorization:
            if not authorization.lower().startswith("basic ") or "client_secret" in params:
                return None
            credentials = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
            basic_id, secret = map(unquote_plus, credentials.split(":", 1))
            if "client_id" in params and client_id != basic_id:
                return None
            client_id, method = basic_id, "client_secret_basic"
        if client_id == "pcbridge":
            return client_id if method != "none" and hmac.compare_digest(secret.encode(), self.secret) else None
        client = self.store.client(client_id)
        if client and client["method"] == method and (method == "none"
                or hmac.compare_digest(digest(secret), client["secret_hash"])):
            return client_id
        return None

    async def token(self, send, resource, params, authorization):
        client_id = self.authenticate_client(params, authorization)
        if not client_id:
            return await send_json(send, {"error": "invalid_client"}, 401)
        if params.get("scope", "mcp") != "mcp" or params.get("resource", resource) != resource:
            return await send_json(send, {"error": "invalid_scope_or_resource"}, 400)
        grant = params.get("grant_type")
        if grant == "authorization_code":
            hashed, entry = self.lookup(self.codes, params.get("code"))
            if (not entry or (entry[0], entry[1], entry[3]) != (client_id, params.get("redirect_uri"), resource)
                    or not verify_pkce(params.get("code_verifier", ""), entry[2])):
                return await send_json(send, {"error": "invalid_grant"}, 400)
        elif grant != "client_credentials" or client_id != "pcbridge":
            return await send_json(send, {"error": "unauthorized_client"}, 400)

        token = self.store.issue(client_id, resource, "mcp", TOKEN_TTL)
        if not token:
            return await send_json(send, {"error": "temporarily_unavailable"}, 503)
        if grant == "authorization_code":
            self.codes.pop(hashed)
        return await send_json(send, {"access_token": token, "token_type": "Bearer",
                                      "expires_in": TOKEN_TTL, "scope": "mcp"})

    async def revoke(self, send, params, authorization):
        client_id = self.authenticate_client(params, authorization)
        if not client_id:
            return await send_json(send, {"error": "invalid_client"}, 401)
        self.store.revoke(params.get("token", ""), client_id)
        return await send_json(send, {})


class TokenAuth:
    def __init__(self, app, token, store):
        self.app = app
        self.token = token.encode()
        self.store = store

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = request_headers(scope)
        authorization = headers.get("authorization", "").strip()
        bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        keys = [bearer or authorization, *(headers.get(name, "").strip()
                 for name in ("x-api-key", "api-key", "x-pcbridge-token"))]
        principal = "owner" if any(hmac.compare_digest(key.encode(), self.token) for key in keys) else None
        base = scope["pcbridge_origin"]
        if not principal and bearer:
            principal = self.store.authenticate(bearer, base + "/mcp")
        if principal:
            return await self.app(dict(scope, pcbridge_principal=principal), receive, send)
        challenge = f'Bearer realm="PC Bridge", resource_metadata="{base}/.well-known/oauth-protected-resource/mcp"'
        return await send_json(send, {"error": "unauthorized"}, 401, [(b"www-authenticate", challenge.encode("ascii"))])
