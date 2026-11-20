"""Regression tests for the local URL-token Streamable HTTP adapter."""
import asyncio
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import httpx

SOURCE = Path(__file__).resolve().parents[1] / "server" / "token_http.py"
spec = importlib.util.spec_from_file_location("pcbridge_token_http", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class TokenHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "TOKEN.txt").write_text("test-secret-123", encoding="utf-8")
        (self.root / "config.json").write_text(json.dumps({"port": 8765}), encoding="utf-8")
        self.upstream_calls = []

        def upstream(req):
            self.upstream_calls.append(req)
            return httpx.Response(
                200, headers={"content-type": "application/json",
                              "mcp-session-id": "session-1"},
                json={"jsonrpc": "2.0", "result": {"tools": []}, "id": 1},
            )

        self.app = module.make_app(self.root, transport=httpx.MockTransport(upstream))
        self.context = self.app.router.lifespan_context(self.app)
        await self.context.__aenter__()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://127.0.0.1:8770",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.context.__aexit__(None, None, None)
        self.temp.cleanup()

    async def test_valid_token_forwards_bearer_and_session(self):
        r = await self.client.post(
            "/test-secret-123/mcp",
            headers={"accept": "application/json, text/event-stream",
                     "mcp-protocol-version": "2025-06-18"},
            json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["result"], {"tools": []})
        self.assertEqual(r.headers["mcp-session-id"], "session-1")
        self.assertEqual(len(self.upstream_calls), 1)
        self.assertEqual(self.upstream_calls[0].url.path, "/mcp")
        self.assertEqual(self.upstream_calls[0].headers["authorization"], "Bearer test-secret-123")
        self.assertEqual(self.upstream_calls[0].headers["mcp-protocol-version"], "2025-06-18")

    async def test_wrong_token_rejected_without_upstream(self):
        r = await self.client.post("/wrong/mcp", json={})
        self.assertEqual(r.status_code, 401)
        self.assertFalse(self.upstream_calls)

    async def test_cross_origin_rejected(self):
        r = await self.client.get("/test-secret-123/mcp",
                                  headers={"origin": "https://attacker.example"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(self.upstream_calls)

    async def test_token_rotation(self):
        (self.root / "TOKEN.txt").write_text("rotated-secret", encoding="utf-8")
        self.assertEqual((await self.client.get("/test-secret-123/mcp")).status_code, 401)
        self.assertEqual((await self.client.get("/rotated-secret/mcp")).status_code, 200)

    async def test_get_and_delete_forwarded(self):
        self.assertEqual((await self.client.get("/test-secret-123/mcp",
                     headers={"mcp-session-id": "x"})).status_code, 200)
        self.assertEqual((await self.client.delete("/test-secret-123/mcp")).status_code, 200)
        self.assertEqual([r.method for r in self.upstream_calls], ["GET", "DELETE"])


if __name__ == "__main__":
    unittest.main()
