"""Verify both Streamable HTTP protocol generations against a real loopback server."""
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
from mcp.types.version import LATEST_HANDSHAKE_VERSION, LATEST_MODERN_VERSION


APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "server"))
from mcp_protocol import LEGACY_PROTOCOL_VERSION
from oauth_store import OAuthStore

ACCEPT = "application/json, text/event-stream"
CLIENT = {"name": "pcbridge-smoke", "version": "1"}


def free_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return server.getsockname()[1]


def request(client, headers, method, params, padding=0):
    message = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    if padding:
        response = client.post("/mcp", headers={**headers, "Content-Type": "application/json"},
                               content=json.dumps(message).encode() + b" " * padding)
    else:
        response = client.post("/mcp", headers=headers, json=message)
    response.raise_for_status()
    payload = response.json()
    assert "result" in payload, payload
    return payload["result"]


def data(result):
    if "structuredContent" in result:
        return result["structuredContent"]
    return json.loads(result["content"][0]["text"])


def check_legacy(client, headers, version):
    init = request(client, headers, "initialize",
                   {"protocolVersion": version, "capabilities": {}, "clientInfo": CLIENT})
    assert init["protocolVersion"] == version
    return {**headers, "MCP-Protocol-Version": version}


def request_modern(client, headers, method, params=None):
    metadata = {"io.modelcontextprotocol/protocolVersion": LATEST_MODERN_VERSION,
                "io.modelcontextprotocol/clientInfo": CLIENT,
                "io.modelcontextprotocol/clientCapabilities": {}}
    modern = {**headers, "MCP-Protocol-Version": LATEST_MODERN_VERSION, "Mcp-Method": method}
    if method == "tools/call":
        modern["Mcp-Name"] = params["name"]
    return request(client, modern, method, {**(params or {}), "_meta": metadata})


def check_tools(call):
    catalog = call("tools/list", {})["tools"]
    names = {tool["name"] for tool in catalog}
    assert {"server_info", "read_text_file", "file_info"}.issubset(names)
    assert all("pcbridge_context" not in tool["inputSchema"].get("properties", {}) for tool in catalog)
    info = call("tools/call", {"name": "server_info", "arguments": {}})
    assert data(info)["version"] == (APP / "VERSION.txt").read_text().strip()
    err = call("tools/call", {"name": "apply_file_edits", "arguments": {"plan_id": "invalid"}})
    assert err["isError"] and data(err)["error_code"] == "invalid_plan"


def run():
    port, control = free_port(), free_port()
    while control == port:
        control = free_port()
    with tempfile.TemporaryDirectory(prefix="pcbridge-smoke-") as temp:
        root = Path(temp)
        owner = secrets.token_urlsafe(32)
        base = f"http://127.0.0.1:{port}"
        store = OAuthStore(root / "oauth.sqlite3", owner)
        client_id, _ = store.register("smoke-local-client", [], "none")
        token = store.issue(client_id, base + "/mcp", "mcp", 120)
        (root / "config.json").write_text(json.dumps({
            "port": port, "ctrl_port": control, "secret": owner, "public_origin": "https://bridge.example",
            "client_profiles": {client_id: {"tools": ["file_info", "skills_list", "skill_read"], "skills": []}},
        }), encoding="utf-8")
        proc = subprocess.Popen([sys.executable, str(APP / "server" / "server.py")],
                                env=dict(os.environ, PCBRIDGE_ROOT=temp), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            with httpx.Client(base_url=base, trust_env=False, timeout=5) as client:
                for _ in range(150):
                    if proc.poll() is not None:
                        raise RuntimeError("MCP server exited")
                    try:
                        client.get("/.well-known/pcbridge-ping").raise_for_status()
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        time.sleep(.1)
                else:
                    raise TimeoutError("MCP server did not listen")
                assert client.post("/mcp", json={}).status_code == 401
                assert client.post("/mcp", headers={"Host": "untrusted.example"}, json={}).status_code == 421
                assert client.post("/mcp", headers={"Origin": "https://untrusted.example"}, json={}).status_code == 403
                for host in (f"127.0.0.1:{port}", "bridge.example"):
                    headers = {"Host": host, "Accept": ACCEPT, "Authorization": f"Bearer {owner}"}
                    for version in (LEGACY_PROTOCOL_VERSION, LATEST_HANDSHAKE_VERSION):
                        legacy = check_legacy(client, headers, version)
                        check_tools(lambda method, params: request(client, legacy, method, params))
                    discovery = request_modern(client, headers, "server/discover")
                    assert LATEST_MODERN_VERSION in discovery["supportedVersions"], discovery
                    check_tools(lambda method, params: request_modern(client, headers, method, params))

                # The inspector uses the same registered tool definitions without invoking a tool.
                endpoint = f"http://127.0.0.1:{control}/tool-catalog"
                assert client.post(endpoint, json={}).status_code == 403
                from hashlib import sha256

                key = sha256(("pcbridge-ctrl:" + owner).encode()).hexdigest()
                report = client.post(endpoint, headers={"X-PCB-Key": key}, json={})
                report.raise_for_status()
                shown = {tool["name"]: tool for tool in report.json()["tools"]}
                live = request_modern(client, headers, "tools/list")["tools"]
                assert set(shown) == {tool["name"] for tool in live}
                assert all(shown[tool["name"]]["inputSchema"] == tool["inputSchema"] for tool in live)
                assert shown["file_info"]["read_only"] and not shown["apply_file_edits"]["read_only"]

                # Replay is confined to the authenticated loopback control endpoint.
                assert {name for name, tool in shown.items() if tool["replayable"]} == {"computer_capabilities", "computer_plan"}
                endpoint = f"http://127.0.0.1:{control}/diagnostic-replay"
                ctrl = {"X-PCB-Key": key}
                call = {"name": "computer_plan", "arguments": {"target": "browser"}}
                assert client.post(endpoint, json=call).status_code == 403
                assert client.post("/diagnostic-replay", headers=headers, json=call).status_code == 404
                for name in ("file_info", "apply_file_edits", "plugin_call"):
                    assert client.post(endpoint, headers=ctrl, json={**call, "name": name}).status_code == 403
                assert client.post(endpoint, headers=ctrl, json={**call, "arguments": []}).status_code == 400
                assert client.post(endpoint, headers=ctrl, json={**call,
                                   "arguments": {"target": "a" * 17000}}).status_code == 413
                for arguments in ({}, {"target": "browser", "unknown": True}):
                    assert "error" in client.post(endpoint, headers=ctrl, json={**call, "arguments": arguments}).json()
                logs = root / "logs"
                actions = (logs / "actions.log").read_bytes()
                activity = json.loads((logs / "activity.json").read_bytes())
                (logs / "readonly.flag").touch()
                try:
                    plan = client.post(endpoint, headers=ctrl, json=call).json()["result"]
                    assert plan["ok"] and "candidates" in plan
                    capabilities = client.post(endpoint, headers=ctrl,
                                               json={"name": "computer_capabilities", "arguments": {}}).json()["result"]
                    assert capabilities["ok"] and capabilities["read_only"]
                    invalid = client.post(endpoint, headers=ctrl, json={**call, "arguments": {
                        "target": "private-replay-fixture"}}).json()["result"]
                    assert not invalid["ok"]
                    (logs / "paused.flag").touch()
                    paused = client.post(endpoint, headers=ctrl, json=call).json()["result"]
                    assert not paused["ok"] and "PAUSED" in paused["error"]
                finally:
                    (logs / "readonly.flag").unlink()
                    (logs / "paused.flag").unlink(missing_ok=True)
                assert (logs / "actions.log").read_bytes() == actions
                current_activity = json.loads((logs / "activity.json").read_bytes())
                assert all(current_activity[key] == activity[key] for key in ("t", "tool", "detail"))
                replay_log = (logs / "diagnostics.jsonl").read_text()
                assert "private-replay-fixture" not in replay_log and "candidates" not in replay_log
                replay_events = [json.loads(line) for line in replay_log.splitlines()]
                assert [event["outcome"] for event in replay_events if event["tool"] == "computer_plan"] == ["ok", "error", "error"]

                # A restricted account sees and calls only its granted tools, in both protocol generations.
                headers = {"Accept": ACCEPT, "Authorization": f"Bearer {token}"}
                legacy = check_legacy(client, headers, LEGACY_PROTOCOL_VERSION)
                for call_mcp in (lambda method, args: request(client, legacy, method, args),
                                 lambda method, args: request_modern(client, headers, method, args)):
                    assert {tool["name"] for tool in call_mcp("tools/list", {})["tools"]} == {
                        "file_info", "skills_list", "skill_read"}
                    assert data(call_mcp("tools/call", {"name": "file_info", "arguments": {"path": "fixture"}}))["ok"]
                    denied = call_mcp("tools/call", {"name": "server_info", "arguments": {}})
                    assert denied["isError"] and data(denied)["error_code"] == "tool_not_granted"
                    skill = call_mcp("tools/call", {"name": "skill_read", "arguments": {"skill_id": "private-fixture"}})
                    assert skill["isError"] and data(skill)["error_code"] == "skill_not_granted"
                records = [json.loads(line) for line in (root / "logs" / "diagnostics.jsonl").read_text().splitlines()]
                assert [record["client_id"] for record in records if record["tool"] == "file_info"] == [client_id, client_id]

                # Large existing tool POSTs must still work above v2's 4 MiB default.
                assert data(request(client, legacy, "tools/call", {"name": "file_info", "arguments": {"path": "fixture"}},
                                    padding=4 * 1024 * 1024))["ok"]
        finally:
            proc.terminate()
            output, _ = proc.communicate(timeout=10)
            if owner in output or token in output:
                raise RuntimeError("Server output contained a credential")
            if sys.exc_info()[0]:
                print(output, file=sys.stderr)
    print(f"Streamable HTTP passed for {LEGACY_PROTOCOL_VERSION}, {LATEST_HANDSHAKE_VERSION} and {LATEST_MODERN_VERSION}")


if __name__ == "__main__":
    run()
