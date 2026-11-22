from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import unittest

from test_computer_use import APP, module


peers = module("desktop_peers")


class DesktopPeerPolicy(unittest.TestCase):
    def test_only_approved_principal_peer_and_app_can_be_selected(self):
        settings = {"desktop_peers": {"lab": {"clients": ["client-a"],
                    "plugin": "lab-pc", "server": "bridge", "apps": {"local-editor": "lab-editor"}}}}
        self.assertEqual(peers.resolve_peer(settings, "lab", "client-a", "local-editor"),
                         ("lab-pc", "bridge", "lab-editor"))
        for peer, caller, app, code in [
            ("lab", "client-b", "local-editor", "desktop_peer_unavailable"),
            ("https://example.invalid/mcp", "owner", "local-editor", "desktop_peer_unavailable"),
            ("lab", "client-a", "other-app", "desktop_peer_app_denied"),
        ]:
            with self.subTest(code=code), self.assertRaises(peers.PeerError) as failure:
                peers.resolve_peer(settings, peer, caller, app)
            self.assertEqual(failure.exception.code, code)

# This test uses two real, independently configured bridges on loopback. The
# fixture applications run on separate Win32 desktops and receive no physical input.
@unittest.skipUnless(os.name == "nt" and os.environ.get("PCBRIDGE_PEER_TESTS") == "1",
                     "opt-in pair of Windows desktop bridge processes")
class DesktopPeerWindows(unittest.TestCase):
    @staticmethod
    @contextmanager
    def ready(root, port, environment=None):
        import httpx
        import psutil

        with (root / "output.txt").open("wb") as log:
            process = subprocess.Popen([sys.executable, str(APP / "server" / "server.py")],
                                       env={**os.environ, **(environment or {}), "PCBRIDGE_ROOT": str(root)},
                                       stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=25) as http:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("Loopback bridge exited: " + (root / "output.txt").read_text()[-3000:])
                    try:
                        http.get("/.well-known/pcbridge-ping").raise_for_status()
                        break
                    except httpx.ConnectError:
                        time.sleep(.1)
                else:
                    raise TimeoutError("Loopback bridge did not start.")
                yield http
        finally:
            children = psutil.Process(process.pid).children(recursive=True) if process.poll() is None else []
            process.terminate()
            process.wait(timeout=10)
            _, remaining = psutil.wait_procs(children, timeout=3)
            for child in remaining:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs(remaining, timeout=5)

    def test_approved_peer_starts_exactly_one_native_app_and_preserves_source(self):
        from tools.smoke_mcp import ACCEPT, data, free_port, request_modern
        from oauth_store import OAuthStore
        from extensions import Extensions
        from plugin_manager import SCHEMAS

        ports = set()
        while len(ports) < 4:
            ports.add(free_port())
        origin_port, origin_control, destination_port, destination_control = sorted(ports)
        with tempfile.TemporaryDirectory(prefix="pcbridge-peer-wire-") as directory:
            origin, destination = Path(directory) / "origin", Path(directory) / "destination"
            origin.mkdir()
            destination.mkdir()
            passwords = {root: secrets.token_urlsafe(32) for root in (origin, destination)}
            identities = {}
            tokens = {}
            for root, port in ((origin, origin_port), (destination, destination_port)):
                store = OAuthStore(root / "oauth.sqlite3", passwords[root])
                identities[root], _ = store.register("pcbridge-test", [], "none")
                tokens[root] = store.issue(identities[root], f"http://127.0.0.1:{port}/mcp", "mcp", 180)
            peer_source = Path(directory) / "peer-plugin"
            peer_source.mkdir()
            (peer_source / "plugin.json").write_text(json.dumps({
                "$schema": SCHEMAS + "plugin.schema.json", "name": "peer-fixture",
            }), encoding="utf-8")
            (peer_source / "mcp.json").write_text(json.dumps({
                "$schema": SCHEMAS + "mcp.schema.json", "mcpServers": {"bridge": {
                    "type": "streamable-http", "url": f"http://127.0.0.1:{destination_port}/mcp",
                }},
            }), encoding="utf-8")
            registry = Extensions(origin)
            plugin = registry.import_package(peer_source)
            registry.set_enabled("plugin", "peer-fixture", True)
            powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            for root, port, control, app in ((origin, origin_port, origin_control, "form-origin"),
                                              (destination, destination_port, destination_control, "form-peer")):
                caller = identities[root]
                config = {"port": port, "ctrl_port": control, "secret": passwords[root], "workspace": str(root),
                          "enable_background_desktop": True, "enable_internet": True,
                          "background_desktop_apps": {app: {"clients": [caller], "executable": str(powershell),
                              "args": ["-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File",
                                       str(APP / "tests" / "fixtures" / "uia_form.ps1"), "-Title", "PCBridge Peer Fixture"]}}}
                if root == origin:
                    config["desktop_peers"] = {"lab": {"plugin": "peer-fixture", "server": "bridge",
                                                       "clients": [caller], "apps": {"form-origin": "form-peer"}}}
                    config["plugin_mcp_grants"] = {"peer-fixture": {"bridge": {
                        "sha256": plugin["sha256"], "clients": {caller: ["desktop_session_open"]},
                        "credentials": {"Authorization": "PCBRIDGE_TEST_PEER_AUTHORIZATION"},
                    }}}
                else:
                    config["client_profiles"] = {caller: {"tools": ["desktop_session_open", "desktop_sessions",
                                                                   "desktop_wait", "desktop_session_close"]}}
                (root / "config.json").write_text(json.dumps(config), encoding="utf-8")

            def call(http, root, name, args=None):
                return data(request_modern(http, {"Accept": ACCEPT, "Authorization": "Bearer " + tokens[root]},
                                           "tools/call", {"name": name, "arguments": args or {}}))

            with self.ready(destination, destination_port) as target:
                with self.ready(origin, origin_port, {
                    "PCBRIDGE_TEST_PEER_AUTHORIZATION": "Bearer " + tokens[destination],
                }) as source:
                    first = call(source, origin, "desktop_session_open", {"app": "form-origin"})
                    original = first["session_id"]
                    try:
                        options = {"session_id": original, "operation_id": "peer-native-test-0001", "peer": "lab"}
                        receipt = call(source, origin, "desktop_peer_handoff", options)
                        self.assertTrue(receipt["ok"], receipt)
                        sid = receipt["destination_session_id"]
                        try:
                            self.assertNotEqual(sid, original)
                            replay = call(source, origin, "desktop_peer_handoff", options)
                            self.assertTrue(replay["already_dispatched"])
                            self.assertEqual(replay["destination_session_id"], sid)
                            observed = call(target, destination, "desktop_wait", {"session_id": sid, "role": "Button",
                                            "text": "PCBridge apply", "window": "PCBridge Peer Fixture"})
                            self.assertTrue(observed["condition_met"], observed)
                            self.assertEqual([x["session_id"] for x in call(target, destination, "desktop_sessions")["sessions"]],
                                             [sid])
                            self.assertEqual([x["session_id"] for x in call(source, origin, "desktop_sessions")["sessions"]],
                                             [original])
                        finally:
                            call(target, destination, "desktop_session_close", {"session_id": sid})
                    finally:
                        call(source, origin, "desktop_session_close", {"session_id": original})
                    retained = call(source, origin, "desktop_peer_operation",
                                    {"peer": "lab", "operation_id": options["operation_id"]})
                    self.assertEqual(retained["destination_session_id"], sid)
