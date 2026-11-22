import base64
import hashlib
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from http_policy import HTTPPolicy, request_headers, send_json
from oauth import OAuthProvider, TokenAuth, TOKEN_TTL
from oauth_store import OAuthStore


OWNER = "test-owner-credential"
VERIFIER = "v" * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode()[:-1]
REDIRECT = "https://client.example/callback?existing=1"
PUBLIC = "https://bridge.example"


async def endpoint(scope, receive, send):
    await send_json(send, {"principal": scope["pcbridge_principal"],
                           "proxy_header_present": "x-forwarded-host" in request_headers(scope)})


class OAuthSecurity(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "oauth.sqlite3"
        self.tunnel = Path(temp.name) / "tunnel-origin.txt"
        self.now = 1000
        self.store = OAuthStore(self.path, OWNER, clock=lambda: self.now)
        app = OAuthProvider(TokenAuth(endpoint, OWNER, self.store), OWNER, self.store, clock=lambda: self.now)
        self.client = TestClient(HTTPPolicy(app, 8765, PUBLIC, self.tunnel), base_url=PUBLIC, follow_redirects=False)

    def register(self, **kwargs):
        response = self.client.post("/oauth/register", json={
            "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none", **kwargs,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def authorize(self, client_id, **kwargs):
        return self.client.get("/oauth/authorize", params={
            "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", **kwargs,
        })

    def issue_code(self, client_id):
        consent = self.authorize(client_id)
        self.assertEqual(consent.status_code, 200, consent.text)
        nonce = re.search(r'name="request_id" value="([^"]+)"', consent.text)[1]
        result = self.client.post("/oauth/authorize", data={"request_id": nonce, "owner_token": OWNER})
        self.assertEqual(result.status_code, 303, result.text)
        query = parse_qs(urlsplit(result.headers["location"]).query)
        self.assertEqual(query["existing"], ["1"])
        self.assertEqual(query["iss"], [PUBLIC])
        return query["code"][0]

    def exchange(self, client_id, code, **kwargs):
        return self.client.post("/oauth/token", data={
            "grant_type": "authorization_code", "client_id": client_id, "code": code,
            "redirect_uri": REDIRECT, "code_verifier": VERIFIER, **kwargs,
        })

    def test_owner_required_and_consent_escapes_html_and_state(self):
        client = self.register(client_name='<b class="submitted">Owner</b>')["client_id"]
        consent = self.authorize(client, state='&x="<b>"')
        self.assertIn("&lt;b class=&quot;submitted&quot;&gt;", consent.text)
        self.assertIn("%26x%3D%22%3Cb%3E%22", consent.text)
        self.assertIn("frame-ancestors 'none'", consent.headers["content-security-policy"])
        self.assertNotIn("access-control-allow-origin", consent.headers)
        nonce = re.search(r'name="request_id" value="([^"]+)"', consent.text)[1]
        self.assertEqual(self.client.post("/oauth/authorize", data={
            "request_id": nonce, "owner_token": "wrong",
        }).status_code, 403)
        self.assertEqual(self.client.post("/oauth/authorize", data={
            "client_id": client, "redirect_uri": REDIRECT, "code_challenge": CHALLENGE,
        }).status_code, 400)
        self.assertEqual(self.client.post("/oauth/authorize", data={
            "request_id": nonce, "owner_token": OWNER,
        }).status_code, 303)
        self.assertEqual(self.client.post("/oauth/authorize", data={
            "request_id": nonce, "owner_token": OWNER,
        }).status_code, 400)

    def test_invalid_redirect_pkce_or_scope_is_rejected_without_redirect(self):
        client = self.register()["client_id"]
        for overrides in ({"redirect_uri": REDIRECT + "2"}, {"code_challenge": ""},
                          {"code_challenge_method": "plain"}, {"scope": "other"}, {"client_id": "unknown"}):
            with self.subTest(overrides=overrides):
                request = {"client_id": client, "redirect_uri": REDIRECT, "response_type": "code",
                           "code_challenge": CHALLENGE, "code_challenge_method": "S256"}
                response = self.client.get("/oauth/authorize", params={**request, **overrides})
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("location", response.headers)
        for uri in ("javascript:alert(1)", "http://external.example/callback", REDIRECT + "&code=x",
                    "https://user@external.example/callback", "https://bad;host/path", REDIRECT + "#fragment"):
            self.assertEqual(self.client.post("/oauth/register", json={"redirect_uris": [uri]}).status_code, 400)

    def test_code_is_bound_to_client_redirect_resource_and_verifier_and_single_use(self):
        a, b = self.register()["client_id"], self.register()["client_id"]
        code = self.issue_code(a)
        for response in (self.exchange(b, code), self.exchange(a, code, redirect_uri=REDIRECT + "2"),
                         self.exchange(a, code, code_verifier="я" * 43),
                         self.exchange(a, code, resource="https://wrong.example/mcp")):
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.exchange(a, code).status_code, 200)
        self.assertEqual(self.exchange(a, code).status_code, 400)
        code = self.issue_code(a)
        self.now += 121
        self.assertEqual(self.exchange(a, code).status_code, 400)

    def test_registration_does_not_grant_client_credentials(self):
        client = self.register(token_endpoint_auth_method="client_secret_post")
        response = self.client.post("/oauth/token", data={"grant_type": "client_credentials",
            "client_id": client["client_id"], "client_secret": client["client_secret"]})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/oauth/token", data={"grant_type": "client_credentials",
            "client_id": client["client_id"], "client_secret": OWNER})
        self.assertEqual(response.status_code, 401)
        response = self.client.post("/oauth/token", data={"grant_type": "client_credentials",
            "client_id": "pcbridge", "client_secret": OWNER})
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["access_token"], OWNER)

    def test_scoped_distinct_persistent_tokens_and_revocation(self):
        a, b = self.register()["client_id"], self.register()["client_id"]
        first = self.exchange(a, self.issue_code(a)).json()["access_token"]
        second = self.exchange(b, self.issue_code(b)).json()["access_token"]
        self.assertEqual(len({first, second, OWNER}), 3)
        db = sqlite3.connect(self.path)
        try:
            hashes = {row[0] for row in db.execute("SELECT hash FROM tokens")}
        finally:
            db.close()
        self.assertTrue(hashes.isdisjoint({first, second, OWNER}))
        self.assertEqual(OAuthStore(self.path, OWNER, clock=lambda: self.now).authenticate(first, PUBLIC + "/mcp"), a)
        self.assertIsNone(OAuthStore(self.path, "rotated", clock=lambda: self.now).authenticate(first, PUBLIC + "/mcp"))
        hdr = {"Authorization": "Bearer " + first}
        self.assertEqual(self.client.get("/mcp", headers=hdr).json()["principal"], a)
        self.assertEqual(self.client.get("/mcp", headers={**hdr, "Host": "127.0.0.1:8765"}).status_code, 401)
        self.client.post("/oauth/revoke", data={"client_id": b, "token": first}).raise_for_status()
        self.assertEqual(self.client.get("/mcp", headers=hdr).status_code, 200)
        self.client.post("/oauth/revoke", data={"client_id": a, "token": first}).raise_for_status()
        self.assertEqual(self.client.get("/mcp", headers=hdr).status_code, 401)
        self.now += TOKEN_TTL
        self.assertEqual(self.client.get("/mcp", headers={"Authorization": "Bearer " + second}).status_code, 401)

    def test_untrusted_authority_is_rejected_before_oauth_or_mcp(self):
        for path in ("/mcp", "/.well-known/oauth-protected-resource/mcp"):
            self.assertEqual(self.client.get(path, headers={"Host": "evil.example"}).status_code, 421)
            self.assertEqual(self.client.get(path, headers={"Origin": "https://evil.example"}).status_code, 403)
        proxy = {"X-Forwarded-Host": "evil.example", "X-Forwarded-Proto": "http", "X-Original-Host": "evil.example"}
        metadata = self.client.get("/.well-known/oauth-protected-resource/mcp", headers=proxy).json()
        self.assertEqual(metadata["resource"], PUBLIC + "/mcp")
        self.assertFalse(self.client.get("/mcp", headers={**proxy, "Authorization": OWNER}).json()["proxy_header_present"])
        response = self.client.get("/mcp", headers={"Authorization": "fake-secret", **proxy})
        self.assertEqual(response.status_code, 401)
        self.assertIn(PUBLIC, response.headers["www-authenticate"])
        self.assertNotIn("fake-secret", response.text)
        self.assertEqual(self.client.post("/oauth/token", content=b"x" * 65537).status_code, 413)

    def test_only_the_supervisor_approved_tunnel_origin_is_allowed(self):
        self.tunnel.write_text("https://first.example", encoding="utf-8")
        path = "/.well-known/oauth-authorization-server"
        self.assertEqual(self.client.get(path, headers={"Host": "first.example"}).json()["issuer"], "https://first.example")
        self.tunnel.write_text("https://second.example", encoding="utf-8")
        self.assertEqual(self.client.get(path, headers={"Host": "first.example"}).status_code, 421)
        self.assertEqual(self.client.get(path, headers={"Host": "second.example"}).json()["issuer"], "https://second.example")
        self.tunnel.unlink()
        self.assertEqual(self.client.get(path, headers={"Host": "second.example"}).status_code, 421)
