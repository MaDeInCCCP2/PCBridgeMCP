"""Opt-in integration on an interactive Windows desktop using owned fixtures and local MCP."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

import httpx
import psutil


APP = Path(__file__).resolve().parent.parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def open_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(os.name == "nt" and os.environ.get("PCBRIDGE_HYBRID_TESTS") == "1",
                     "opt-in interactive Windows MCP fixture")
class HybridWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="pcbridge-hybrid-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.secret = uuid.uuid4().hex
        cls.port = open_port()
        cls.url = f"http://127.0.0.1:{cls.port}/mcp"
        cls.headers = {"Authorization": f"Bearer {cls.secret}",
                       "Accept": "application/json, text/event-stream"}
        cfg = {"workspace": str(cls.root), "secret": cls.secret, "port": cls.port, "ctrl_port": open_port(),
               "full_disk": True, "enable_screen": True, "enable_input": True, "enable_shell": True,
               "enable_internet": True, "enable_browser": True, "dangerous_commands": "block",
               "enable_dxgi_capture": os.environ.get("PCBRIDGE_TEST_DXGI") == "1"}
        (cls.root / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        cls.server_log = (cls.root / "server.log").open("wb")
        cls.addClassCleanup(cls.server_log.close)
        cls.proc = subprocess.Popen([sys.executable, str(APP / "server" / "server.py")],
            cwd=APP, env={**os.environ, "PCBRIDGE_ROOT": str(cls.root)}, stdout=cls.server_log,
            stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        cls.addClassCleanup(cls.stop, cls.proc)
        for attempt in range(80):
            try:
                if cls.rpc("tools/list", {})["tools"]:
                    break
            except httpx.TransportError:
                if cls.proc.poll() is not None:
                    break
                time.sleep(.1)
        else:
            raise RuntimeError("Candidate MCP server did not come online.")
        if cls.proc.poll() is not None:
            raise RuntimeError((cls.root / "server.log").read_text(encoding="utf-8", errors="replace"))

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            try:
                parent = psutil.Process(proc.pid)
                children = parent.children(recursive=True)
            except psutil.NoSuchProcess:
                children = []
            for child in reversed(children):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            proc.terminate()
            proc.wait(8)
            psutil.wait_procs(children, timeout=5)

    @classmethod
    def rpc(cls, method, params, headers=None):
        response = httpx.post(cls.url, headers=cls.headers if headers is None else headers, timeout=18, trust_env=False,
            json={"jsonrpc":"2.0", "id":uuid.uuid4().hex, "method":method, "params":params})
        response.raise_for_status()
        reply = response.json()
        if "error" in reply:
            raise AssertionError(reply["error"])
        return reply["result"]

    @classmethod
    def call(cls, name, **arguments):
        result = cls.rpc("tools/call", {"name":name, "arguments":arguments})
        data = result.get("structuredContent")
        if data is None and result.get("content"):
            try:
                data = json.loads(result["content"][0]["text"])
            except (KeyError, ValueError):
                data = {"error": result["content"][0].get("text")}
        images = [x for x in result.get("content", []) if x["type"] == "image"]
        return data, images, result.get("isError", False)

    def test_auth_process_arguments_environment_timeout_and_artifact_lock(self):
        with self.assertRaises(httpx.HTTPStatusError) as denied:
            self.rpc("tools/list", {}, headers={"Accept": "application/json, text/event-stream"})
        self.assertEqual(denied.exception.response.status_code, 401)
        version, _, error = self.call("server_info")
        self.assertFalse(error)
        self.assertEqual(version["version"], "1.7.0")
        argv = ['a"b', "Кириллица", "& $env:EXAMPLE", ""]
        source = self.root / "own-output.bin"
        code = ("import pathlib,sys,os,time,json; print('ready',flush=True); time.sleep(2); "
                "pathlib.Path('own-output.bin').write_bytes(sys.stdin.buffer.read()); "
                "print(json.dumps([sys.argv[1:], os.environ['PCB_SAMPLE']]))")
        args = dict(executable=sys.executable, argv=["-c", code, *argv], cwd=str(self.root),
                    env_delta={"PCB_SAMPLE":"тест"}, stdin_text="байты", resources=[str(self.root / "cache")],
                    artifacts=[str(source)])
        started, _, error = self.call("run_process", background=True, **args)
        self.assertFalse(error, started)
        other, _, error = self.call("run_process", background=True, **args)
        self.assertTrue(error, other)
        self.assertEqual(other["owner_job_id"], started["job_id"])
        cursor, output, deadline = "", "", time.monotonic()+10
        while time.monotonic()<deadline:
            chunk, _, error = self.call("read_job_output", job_id=started["job_id"], cursor=cursor, wait_ms=1000)
            self.assertFalse(error, chunk)
            cursor, output = chunk["next_cursor"], output+chunk["text"]
            if chunk["status"] == "completed":
                break
        state, _, _ = self.call("job_status", job_id=started["job_id"])
        self.assertEqual(state["status"], "completed", state)
        self.assertEqual(json.loads(output.splitlines()[-1]), [argv, "тест"])
        artifact = state["artifacts"][0]
        self.assertEqual(Path(artifact["path"]).read_bytes(), "байты".encode())
        self.assertEqual(artifact["sha256"], hashlib.sha256("байты".encode()).hexdigest())
        failed, _, error = self.call("run_process", executable=sys.executable,
            argv=["-c", "import sys; print('test error',file=sys.stderr); sys.exit(9)"])
        self.assertTrue(error)
        self.assertEqual((failed["executed"], failed["exit_code"], failed["error_code"]), (True, 9, "process_exit_nonzero"))
        self.assertIn("test error", failed["stderr"])
        expired, _, error = self.call("run_process", executable=sys.executable,
            argv=["-c","import time; time.sleep(6)"], timeout=1)
        self.assertTrue(error)
        self.assertEqual(expired["error_code"], "process_timeout")
        paths, _, _ = self.call("read_files_batch", paths=[str(source), str(self.root / "missing")])
        self.assertEqual(paths["files"][0]["text"], "байты")
        self.assertEqual(paths["files"][1]["error_code"], "file_failed")

    @unittest.skipUnless(os.environ.get("PCBRIDGE_TEST_DXGI") == "1", "optional Windows DXGI smoke")
    def test_dxgi_on_owned_webgl(self):
        fixture = Path(__file__).parent / "fixtures" / "hybrid_webgl.py"
        marker = self.root / "renderer.txt"
        proc = subprocess.Popen([sys.executable, str(fixture), str(marker)], cwd=self.root, creationflags=NO_WINDOW)
        self.addCleanup(self.stop, proc)
        title = "PCBridge DXGI Validation"
        for _ in range(60):
            found, _, _ = self.call("list_windows", filter=title)
            if marker.exists() and found.get("windows"):
                break
            time.sleep(.1)
        renderer = marker.read_text(encoding="utf-8")
        self.assertTrue(renderer, "WebGL did not initialize")
        hwnd = found["windows"][0]["hwnd"]
        data, images, error = self.call("computer_observe", window=f"hwnd:{hwnd}", method="dxgi")
        self.assertFalse(error, data)
        self.assertEqual(data["capture_method"], "dxgi_desktop")
        self.assertTrue(images)
        print(f"WebGL renderer {renderer}; DXGI capture {data['capture_ms']} ms")

    def test_canvas_mcp_capture_input_focus_and_pause(self):
        fixture = Path(__file__).parent / "fixtures" / "hybrid_canvas.py"
        title = "PCBridge Canvas Fixture " + uuid.uuid4().hex[:8]
        owned = subprocess.Popen([sys.executable, str(fixture), title], cwd=self.root, creationflags=NO_WINDOW)
        self.addCleanup(self.stop, owned)
        window = None
        for _ in range(50):
            found, _, _ = self.call("list_windows", filter=title)
            windows = found.get("windows") or []
            if windows:
                window = f'hwnd:{windows[0]["hwnd"]}'
                break
            time.sleep(.1)
        self.assertIsNotNone(window)
        save = self.root / "canvas.jpg"
        first, images, error = self.call("computer_observe", window=window, method="screen", strict=True,
                                        bring_to_front=True, save_as=str(save))
        self.assertFalse(error, first)
        self.assertEqual(save.read_bytes(), base64.b64decode(images[0]["data"]))
        self.assertEqual(hashlib.sha256(save.read_bytes()).hexdigest(), first["sha256"])
        params = {k:first[k] for k in ("window_ref","capture_id")}
        ground, _, error = self.call("computer_find_target", mode="visual", **params)
        self.assertFalse(error, ground)
        self.assertTrue(any(c["source"] == "contour" for c in ground["candidates"]), ground)
        steps = [{"op":"click", "x":100, "y":85}, {"op":"verify", "region":[52,48,154,101],
                  "threshold":35, "ms":140}]
        action, _, error = self.call("computer_run_steps", **params, steps=steps, operation_id="canvas_click")
        self.assertFalse(error, action)
        self.assertTrue(action["postcondition_verified"], action)
        timings = {"legacy": [], "bound": []}
        for _ in range(5):
            for k,name,args in [
                ("legacy","screenshot",{"window":window,"method":"screen"}),
                ("bound","computer_observe",{"window_ref":first["window_ref"],"method":"screen"})]:
                start = time.monotonic()
                _, _, error = self.call(name, **args)
                self.assertFalse(error)
                timings[k].append((time.monotonic()-start)*1000)
        print("Capture median ms " + json.dumps({k:round(statistics.median(v),1) for k,v in timings.items()}))
        if os.environ.get("PCBRIDGE_TEST_DXGI") == "1":
            dxgi, _, error = self.call("computer_observe", window_ref=first["window_ref"], method="dxgi", strict=True)
            self.assertFalse(error, dxgi)
            self.assertEqual(dxgi["capture_method"], "dxgi_desktop")
            print("DXGI capture ms", dxgi["capture_ms"])
        flag = self.root / "logs" / "readonly.flag"
        flag.touch()
        try:
            _, _, error = self.call("computer_run_steps", **params, steps=steps, operation_id="canvas_readonly")
            self.assertTrue(error)
            _, _, error = self.call("computer_observe", window_ref=first["window_ref"])
            self.assertFalse(error)
        finally:
            flag.unlink()
        next_frame, _, error = self.call("computer_observe", window_ref=first["window_ref"], strict=True)
        self.assertFalse(error, next_frame)
        latest = {k:next_frame[k] for k in ("window_ref","capture_id")}
        with ThreadPoolExecutor() as pool:
            focus = pool.submit(self.call, "observe_focus", window_ref=first["window_ref"], duration_ms=1500)
            time.sleep(.15)
            action, _, error = self.call("computer_run_steps", **latest, operation_id="focus_loss",
                                         steps=[{"op":"key", "key":"w", "hold_ms":900}])
            events, _, observe_error = focus.result()
        self.assertFalse(observe_error, events)
        self.assertTrue(events["events"], events)
        self.assertTrue(error, action)
        self.assertEqual(action["error_code"], "focus_lost")
        self.assertEqual(action["released_owned_inputs"], ["key"])


if __name__ == "__main__":
    unittest.main()
