"""Exercise Chromium context isolation and cleanup under concurrent client load."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
from time import monotonic, sleep

from psutil import NoSuchProcess, Process


APP = Path(__file__).resolve().parents[1]
FIXTURE = b"""<!doctype html><title>Browser isolation fixture</title>
<p id="cookie"></p><p id="storage"></p>
<script>
const value = new URLSearchParams(location.search).get("write");
if (value) localStorage.setItem("benchmark", value);
document.querySelector("#cookie").textContent = document.cookie || "cookie-empty";
document.querySelector("#storage").textContent = "storage-" + (localStorage.getItem("benchmark") || "empty");
</script>"""


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        marker = re.fullmatch(r"/fixture\?write=(client-[0-9]+)", self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if marker:
            self.send_header("Set-Cookie", f"fixture-visible={marker[1]}; Path=/")
        self.end_headers()
        self.wfile.write(FIXTURE)

    def log_message(self, *_):
        pass


def contains_only_marker(snapshot, marker):
    aria = snapshot["aria"]
    cookies = re.findall(r"fixture-visible=(client-[0-9]+)", aria)
    storage = re.findall(r"storage-(client-[0-9]+)", aria)
    if cookies != [marker] or storage != [marker]:
        raise RuntimeError(f"Cookie or local storage isolation failed for {marker}.")


def verify_empty(snapshot):
    aria = snapshot["aria"]
    if "cookie-empty" not in aria or "storage-empty" not in aria:
        raise RuntimeError("A fresh browser context contains persisted fixture data.")


def run_workload(requests, base, runs, parallel):
    from computer_use import ComputerUseError

    client = requests.client

    def call(owner, operation, **arguments):
        return requests.call(owner, operation, arguments, timeout=45)

    def open_session(owner, url):
        return call(owner, "session_open", url=url, headless=True)

    def close_session(owner, snapshot):
        result = call(owner, "session_close", session_id=snapshot["session_id"])
        if result.get("closed_session_id") != snapshot["session_id"]:
            raise RuntimeError("The canceled browser session was not closed.")

    def require_error(code, owner, operation, **arguments):
        try:
            call(owner, operation, **arguments)
        except ComputerUseError as error:
            if error.code == code:
                return
            raise
        raise RuntimeError(f"{operation} unexpectedly succeeded for {owner}.")

    def browser_processes():
        result = set()
        for process in Process(client.proc.pid).children(recursive=True):
            try:
                if process.name().lower().removesuffix(".exe") in {"chrome", "chrome-headless-shell", "chromium"}:
                    result.add(process.pid)
            except NoSuchProcess:
                continue
        return result

    def verify_closed():
        if call("benchmark", "session_inventory")["sessions"]:
            raise RuntimeError("Browser sessions remained in the worker after cleanup.")
        deadline = monotonic() + 5
        while browser_processes():
            if monotonic() >= deadline:
                raise RuntimeError("Chromium processes remained after the final context closed.")
            sleep(.05)

    def scenario(index, opened, checked, peaks):
        owner = f"client-{index}"
        started = monotonic()
        snapshot = open_session(owner, base + "?write=" + owner)
        try:
            contains_only_marker(snapshot, owner)
            if opened.wait(90) == 0:
                live = call("benchmark", "session_inventory")["sessions"]
                peaks.append(len(live))
                if len(live) != opened.parties or not browser_processes():
                    raise RuntimeError("The expected number of simultaneous Chromium contexts was not observed.")
            checked.wait(90)
            refreshed = call(owner, "navigate", page_id=snapshot["page_id"], url=base)
            contains_only_marker(refreshed, owner)
            require_error("unknown_page", owner + "-other", "snapshot", page_id=snapshot["page_id"])
        finally:
            close_session(owner, snapshot)
        return round((monotonic() - started) * 1000, 1)

    timings, peaks = [], []
    with ThreadPoolExecutor(max_workers=parallel) as workers:
        for offset in range(0, runs, parallel):
            participants = min(runs - offset, parallel)
            opened, checked = Barrier(participants), Barrier(participants)
            futures = [workers.submit(scenario, index, opened, checked, peaks)
                       for index in range(offset, offset + participants)]
            timings.extend(future.result() for future in futures)
            verify_closed()

    # Cancel one task after it has created state. Its peer must keep running,
    # and a replacement under the canceled owner's identity must start empty.
    survivor = open_session("survivor", base + "?write=client-10000")
    try:
        canceled = open_session("canceled", base + "?write=client-10001")
        try:
            contains_only_marker(canceled, "client-10001")
        finally:
            close_session("canceled", canceled)
        require_error("unknown_page", "canceled", "snapshot", page_id=canceled["page_id"])
        contains_only_marker(call("survivor", "snapshot", page_id=survivor["page_id"]), "client-10000")
        replacement = open_session("canceled", base)
        try:
            verify_empty(replacement)
        finally:
            close_session("canceled", replacement)
    finally:
        close_session("survivor", survivor)
    verify_closed()
    ordered = sorted(timings)
    return {"scenario": "browser-concurrency", "passed": len(timings), "peak_live_contexts": max(peaks),
            "p50_ms": median(ordered), "p95_ms": ordered[(95 * len(ordered) + 99) // 100 - 1],
            "cancel_released_context": True, "last_close_released_chromium": True}


def benchmark_browser_sessions(runs, parallel):
    from browser_requests import BrowserRequests
    from computer_use import ComputerUseClient

    with ThreadingHTTPServer(("127.0.0.1", 0), Fixture) as server, TemporaryDirectory() as temp:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        worker = ComputerUseClient(APP / "server" / "browser_worker.py", Path(temp) / "worker.log",
                                   timeout=30, worker_args=["--allow-lan"], kill_tree=True, close_at_exit=False)
        try:
            return run_workload(BrowserRequests(worker), f"http://127.0.0.1:{server.server_port}/fixture", runs, parallel)
        finally:
            worker.close()
            server.shutdown()
            thread.join()
