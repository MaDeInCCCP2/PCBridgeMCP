import json
import os
import tempfile
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import anyio

from test_extensions import Extensions, SCHEMAS
from external_mcp import ExternalMCP, ExternalMCPError, PluginBackoff


def fixture(port=None):
    from mcp.server.mcpserver import MCPServer
    from mcp.types import CallToolResult, TextContent

    app = MCPServer("local fixture")
    calls = 0

    @app.tool()
    def allowed() -> dict:
        nonlocal calls
        calls += 1
        return {"calls": calls, "fixture": os.environ.get("PCB_PLUGIN_TEST_SECRET")}

    @app.tool()
    def desktop_session_open(app: str) -> CallToolResult:
        nonlocal calls
        calls += 1
        data = {"mode": "win32_desktop", "session_id": f"{calls:032x}", "private": "do-not-return", "app": app}
        return CallToolResult(structured_content=data, content=[TextContent(type="text", text=json.dumps(data))])

    @app.tool()
    def restricted() -> str:
        return "restricted"

    if port is None:
        app.run(transport="stdio")
        return
    import uvicorn

    target = app.streamable_http_app(json_response=True, stateless_http=True)

    async def checked(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if (headers.get(b"x-pcb-fixture") != b"remote-secret"
                    and headers.get(b"authorization") != b"remote-secret"):
                from http_policy import send_json

                return await send_json(send, {"error": "unauthorized"}, 401)
        return await target(scope, receive, send)

    uvicorn.run(checked, host="127.0.0.1", port=port, access_log=False)


class ExternalRuntime(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.registry = Extensions(root)
        package = root / "source"
        package.mkdir()
        (package / "plugin.json").write_text(json.dumps({
            "$schema": SCHEMAS + "plugin.schema.json", "name": "fixture"
        }), encoding="utf-8")
        (package / "mcp.json").write_text(json.dumps({
            "$schema": SCHEMAS + "mcp.schema.json", "mcpServers": {"test": {
                "type": "stdio", "command": "python", "args": [str(Path(__file__).resolve())],
            }},
        }), encoding="utf-8")
        record = self.registry.import_package(package)
        self.registry.set_enabled("plugin", "fixture", True)
        self.cfg = {"enable_shell": True, "plugin_mcp_grants": {"fixture": {"test": {
            "sha256": record["sha256"], "clients": {"client-a": ["allowed"]},
            "credentials": {"PCB_PLUGIN_TEST_SECRET": "PCB_TEST_MCP_CREDENTIAL"},
        }}}}
        self.runtime = ExternalMCP(self.registry, self.cfg)

    def test_grant_is_client_and_tool_bound_before_process_start(self):
        async def check():
            with self.assertRaises(ExternalMCPError) as error:
                await self.runtime.run("fixture", "test", "client-b")
            self.assertEqual(error.exception.code, "plugin_permission_denied")
            with self.assertRaises(ExternalMCPError) as error:
                await self.runtime.run("fixture", "test", "client-a", tool="restricted", operation_id="example-operation")
            self.assertEqual(error.exception.code, "plugin_permission_denied")
        anyio.run(check)

    def test_stdio_transport_redaction_and_durable_action_deduplication(self):
        paths = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")

        async def check():
            with patch.dict(os.environ, {"PATH": paths, "PCB_TEST_MCP_CREDENTIAL": "private-fixture-value"}):
                listing = await self.runtime.run("fixture", "test", "client-a")
                self.assertEqual([tool["name"] for tool in listing["tools"]], ["allowed"])
                options = {"tool": "allowed", "operation_id": "example-operation", "arguments": {}}
                called = await self.runtime.run("fixture", "test", "client-a", **options)
                self.assertTrue(called["ok"])
                self.assertNotIn("private-fixture-value", json.dumps(called))
                self.assertIn("[redacted]", json.dumps(called))
                restarted = ExternalMCP(self.registry, self.cfg)
                with patch.object(restarted, "connect", side_effect=AssertionError("duplicate opened a subprocess")):
                    repeated = await restarted.run("fixture", "test", "client-a", **options)
                self.assertTrue(repeated["already_dispatched"])
                self.assertEqual(repeated["outcome"], "completed")
        anyio.run(check)

    def test_transport_failure_backs_off_only_the_offending_principal(self):
        now = [100]
        budget = PluginBackoff(clock=lambda: now[0])
        self.cfg["plugin_mcp_grants"]["fixture"]["test"]["clients"]["client-b"] = ["allowed"]

        async def check():
            runtime = ExternalMCP(self.registry, self.cfg, backoff=budget)
            with patch.object(runtime, "connect", side_effect=OSError("fixture unavailable")) as connect:
                for _ in range(2):
                    with self.assertRaises(ExternalMCPError) as problem:
                        await runtime.run("fixture", "test", "client-a")
                    self.assertEqual(problem.exception.code, "plugin_unavailable")
                with self.assertRaises(ExternalMCPError) as cooldown:
                    await runtime.run("fixture", "test", "client-a")
                self.assertEqual(cooldown.exception.code, "plugin_backoff")
                self.assertEqual(cooldown.exception.details["retry_after_seconds"], 2)
                self.assertEqual(connect.call_count, 2)
                with self.assertRaises(ExternalMCPError) as other:
                    await runtime.run("fixture", "test", "client-b")
                self.assertEqual(other.exception.code, "plugin_unavailable")
                now[0] += 2
                with self.assertRaises(ExternalMCPError) as retry:
                    await runtime.run("fixture", "test", "client-a")
                self.assertEqual(retry.exception.code, "plugin_unavailable")
                self.assertEqual(connect.call_count, 4)
        anyio.run(check)

    def test_peer_handoff_refuses_local_stdio(self):
        with self.assertRaises(ExternalMCPError) as failure:
            anyio.run(self.runtime.desktop_handoff, "fixture", "test", "client-a", "peer",
                      "source", "editor", "test-op-peer-000001")
        self.assertEqual(failure.exception.code, "desktop_peer_transport")

    def test_streamable_http_auth_failure_is_isolated(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen([sys.executable, __file__, "--http", str(port)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.1):
                        break
                except OSError:
                    if process.poll() is not None:
                        self.fail("HTTP fixture exited before startup")
                    time.sleep(.05)
            else:
                self.fail("HTTP fixture did not start")
            package = self.registry.root.parent.parent / "source"
            (package / "mcp.json").write_text(json.dumps({
                "$schema": SCHEMAS + "mcp.schema.json", "mcpServers": {"test": {
                    "type": "streamable-http", "url": f"http://127.0.0.1:{port}/mcp",
                }},
            }), encoding="utf-8")
            record = self.registry.import_package(package)
            self.registry.set_enabled("plugin", "fixture", True)
            self.cfg["enable_internet"] = True
            grant = self.cfg["plugin_mcp_grants"]["fixture"]["test"]
            grant.update(sha256=record["sha256"], credentials={"X-PCB-Fixture": "PCB_TEST_MCP_CREDENTIAL"})

            async def check():
                with patch.dict(os.environ, {"PCB_TEST_MCP_CREDENTIAL": "incorrect"}):
                    with self.assertRaises(ExternalMCPError) as error:
                        await self.runtime.run("fixture", "test", "client-a")
                    self.assertEqual(error.exception.code, "plugin_unavailable")
                with patch.dict(os.environ, {"PCB_TEST_MCP_CREDENTIAL": "remote-secret"}):
                    result = await self.runtime.run("fixture", "test", "client-a")
                self.assertEqual([tool["name"] for tool in result["tools"]], ["allowed"])
                grant["clients"]["client-a"].append("desktop_session_open")
                args = ("fixture", "test", "client-a", "peer-one", "source", "editor", "test-op-peer-000001")
                with self.assertRaises(ExternalMCPError) as absent:
                    await self.runtime.desktop_handoff(*args)
                self.assertEqual(absent.exception.code, "desktop_peer_credential")
                grant["credentials"] = {"Authorization": "PCB_TEST_MCP_CREDENTIAL"}
                with patch.dict(os.environ, {"PCB_TEST_MCP_CREDENTIAL": "remote-secret"}):
                    started = await self.runtime.desktop_handoff(*args)
                    self.assertTrue(started["ok"], started)
                    self.assertEqual(started["destination_session_id"], f"{1:032x}")
                    self.assertNotIn("do-not-return", str(started))
                    restarted = ExternalMCP(self.registry, self.cfg)
                    with patch.object(restarted, "connect", side_effect=AssertionError("handoff replay connected")):
                        repeated = await restarted.desktop_handoff(*args)
                        self.assertTrue(repeated["already_dispatched"])
                        self.assertEqual(repeated["destination_session_id"], started["destination_session_id"])
                        status = restarted.desktop_handoff_status(*args[:4], args[-1])
                        self.assertEqual(status["destination_session_id"], started["destination_session_id"])
                        with self.assertRaises(ExternalMCPError) as missing:
                            restarted.desktop_handoff_status(*args[:3], "different-peer", args[-1])
                        self.assertEqual(missing.exception.code, "desktop_handoff_missing")
                        with self.assertRaises(ExternalMCPError) as conflict:
                            await restarted.desktop_handoff(*args[:4], "different-source", *args[5:])
                        self.assertEqual(conflict.exception.code, "plugin_operation_conflict")
            anyio.run(check)
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    fixture(int(sys.argv[2]) if len(sys.argv) == 3 and sys.argv[1] == "--http" else None)
