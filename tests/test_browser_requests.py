from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
from time import monotonic, sleep
import unittest

from test_browser import B

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from browser_requests import BrowserRequests, ComputerUseError
from operation_receipts import OperationReceiptStore


class Client:
    def __init__(self):
        self.proc = None
        self.sessions = {}
        self.calls = []
        self.handler = None

    def call(self, operation, arguments, **options):
        if self.proc is None:
            self.proc = object()
        owner = arguments["_owner"]
        self.calls.append((owner, operation))
        if operation == "session_open":
            sid = "session-" + str(len(self.calls))
            self.sessions[sid] = owner
            return {"session_id": sid, "_live_sessions": [(owner, sid) for sid, owner in self.sessions.items()]}
        if operation == "session_close":
            sid = arguments["session_id"]
            self.sessions.pop(sid)
            return {"closed_session_id": sid}
        if self.handler:
            return self.handler(operation)
        return {"ok": True, "verification": {"status": "verified"}, "snapshot": {"text": "Private value"}}


class BrowserReceipts(unittest.TestCase):
    def setUp(self):
        self.client = Client()
        self.requests = BrowserRequests(self.client)
        opened = self.requests.call("a", "session_open")
        self.sid = opened["session_id"]
        self.assertNotIn("_live_sessions", opened)
        self.arguments = {"page_id": "p", "snapshot_id": "s", "ref": "e1", "value": "Private value"}

    def test_replay_conflict_status_and_owner(self):
        result = self.requests.act_once("a", self.sid, "fill", self.arguments)
        self.assertEqual(result["operation"]["outcome"], "verified")
        self.assertIn("snapshot", result)
        replay = self.requests.act_once("a", self.sid, "fill", self.arguments)
        self.assertTrue(replay["replayed"])
        self.assertNotIn("Private value", str(replay))
        self.assertNotIn("snapshot", replay)
        self.assertEqual(self.requests.status("a", self.sid, "fill")["operation"], result["operation"])
        with self.assertRaisesRegex(ComputerUseError, "different input"):
            self.requests.act_once("a", self.sid, "fill", {**self.arguments, "value": "different"})
        with self.assertRaisesRegex(ComputerUseError, "unavailable"):
            self.requests.status("b", self.sid, "fill")
        with self.assertRaisesRegex(ComputerUseError, "explicit browser session"):
            self.requests.act_once("b", self.sid, "fill", self.arguments)
        self.assertEqual(self.client.calls.count(("a", "owned_action")), 1)

    def test_unknown_worker_result_survives_loss_and_cannot_be_replayed(self):
        def lose_worker(operation):
            self.client.proc = None
            raise ComputerUseError("ui_timeout", "Possible private diagnostics", outcome="unknown")

        self.client.handler = lose_worker
        result = self.requests.act_once("a", self.sid, "click", self.arguments)
        self.assertEqual(result["operation"]["outcome"], "unknown")
        self.assertIsNone(result["operation"]["action_executed"])
        self.assertNotIn("private", str(result))
        self.assertTrue(self.requests.act_once("a", self.sid, "click", self.arguments)["replayed"])
        self.assertEqual(self.client.calls.count(("a", "owned_action")), 1)
        with self.assertRaises(ComputerUseError):
            self.requests.act_once("a", self.sid, "another", self.arguments)

    def test_caches_rejection_and_keeps_recent_receipts_on_close(self):
        def stale(operation):
            raise ComputerUseError("stale_snapshot", "Snapshot expired")

        self.client.handler = stale
        result = self.requests.act_once("a", self.sid, "click", self.arguments)
        self.assertEqual(result["operation"]["outcome"], "not_dispatched")
        self.assertFalse(result["operation"]["action_executed"])
        self.requests.call("a", "session_close", {"session_id": self.sid})
        self.assertEqual(self.requests.status("a", self.sid, "click")["operation"]["error_code"], "stale_snapshot")

    def test_pending_operation_can_be_queried_and_retried_without_dispatch(self):
        started, release = Event(), Event()

        def block(operation):
            started.set()
            release.wait(3)
            return {"verification": {"status": "unverified"}}

        self.client.handler = block
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.requests.act_once, "a", self.sid, "click", self.arguments)
            try:
                self.assertTrue(started.wait(2))
                self.assertEqual(self.requests.status("a", self.sid, "click")["operation"]["state"], "running")
                self.assertTrue(self.requests.act_once("a", self.sid, "click", self.arguments)["replayed"])
            finally:
                release.set()
            result = future.result(3)
        self.assertFalse(result["ok"])
        self.assertTrue(result["operation"]["action_executed"])
        self.assertEqual(result["operation"]["outcome"], "unverified")
        self.assertEqual(self.client.calls.count(("a", "owned_action")), 1)

    def test_durable_receipt_after_close_and_restart_never_dispatches_again(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipts.sqlite3"
            store = OperationReceiptStore(path, b"fixture-secret", "browser", scope_limit=512)
            client = Client()
            requests = BrowserRequests(client, receipt_store=store)
            sid = requests.call("a", "session_open")["session_id"]
            first = requests.act_once("a", sid, "persisted-fill", self.arguments)
            requests.call("a", "session_close", {"session_id": sid})
            replacement_client = Client()
            replacement = BrowserRequests(replacement_client, receipt_store=OperationReceiptStore(
                path, b"fixture-secret", "browser", scope_limit=512))
            replay = replacement.act_once("a", sid, "persisted-fill", self.arguments)
            self.assertEqual(replay["operation"], first["operation"])
            self.assertTrue(replay["replayed"])
            self.assertNotIn("snapshot", replay)
            self.assertNotIn(b"Private value", path.read_bytes())
            with self.assertRaises(ComputerUseError) as foreign:
                replacement.status("b", sid, "persisted-fill")
            self.assertEqual(foreign.exception.code, "unknown_browser_operation")
            with self.assertRaises(ComputerUseError) as conflict:
                replacement.act_once("a", sid, "persisted-fill", {**self.arguments, "value": "other"})
            self.assertEqual(conflict.exception.code, "browser_operation_conflict")
            self.assertEqual(replacement_client.calls, [])

    def test_uncommitted_result_survives_restart_as_unknown(self):
        with TemporaryDirectory() as tmp:
            store = OperationReceiptStore(Path(tmp) / "receipts.sqlite3", b"fixture", "browser", scope_limit=512)
            store.reserve(json.dumps(("a", self.sid)), "worker-lost", store.fingerprint(self.arguments))
            client = Client()
            recovered = BrowserRequests(client, receipt_store=store)
            operation = recovered.status("a", self.sid, "worker-lost")["operation"]
            self.assertEqual(operation["outcome"], "unknown")
            self.assertIsNone(operation["action_executed"])
            retry = recovered.act_once("a", self.sid, "worker-lost", self.arguments)
            self.assertEqual(retry["operation"], operation)
            self.assertTrue(retry["replayed"])
            self.assertEqual(client.calls, [])

    def test_storage_failure_before_admission_sends_no_input(self):
        with TemporaryDirectory() as tmp:
            invalid = Path(tmp) / "absent" / "receipts.sqlite3"
            store = OperationReceiptStore(invalid, b"fixture", "browser", scope_limit=512)
            client = Client()
            requests = BrowserRequests(client, receipt_store=store)
            sid = requests.call("a", "session_open")["session_id"]
            with self.assertRaises(ComputerUseError) as unavailable:
                requests.act_once("a", sid, "failed-storage", self.arguments)
            self.assertEqual(unavailable.exception.code, "browser_receipt_store_unavailable")
            self.assertEqual(client.calls, [("a", "session_open")])

    def test_browser_session_quota_isolated_by_owner(self):
        from unittest.mock import patch

        contexts = B.BrowserContexts()
        with patch.object(B.BrowserService, "open", return_value={}):
            contexts.dispatch("open", {"_owner": "first", "_session_limit": 1})
            with self.assertRaises(B.BrowserError) as error:
                contexts.dispatch("session_open", {"_owner": "first", "_session_limit": 1})
            self.assertEqual(error.exception.code, "browser_session_limit")
            second = contexts.dispatch("session_open", {"_owner": "second", "_session_limit": 1})
        self.assertIn(("second", second["session_id"]), contexts.sessions)

    def test_worker_rejects_page_from_another_session_of_the_same_owner(self):
        contexts = B.BrowserContexts()
        context_a = B.BrowserService()
        context_b = B.BrowserService()
        context_a.pages["page"] = object()
        contexts.sessions[("a", "one")] = context_a
        contexts.sessions[("a", "two")] = context_b
        with self.assertRaises(B.BrowserError) as result:
            contexts.dispatch("owned_action", {"_owner": "a", "session_id": "two", "page_id": "page"})
        self.assertEqual(result.exception.code, "unknown_page")


class BrowserQueue(unittest.TestCase):
    def wait_for_queue(self, requests, count):
        end = monotonic() + 2
        while monotonic() < end:
            with requests._ready:
                if requests._waiting == count:
                    return
            sleep(.01)
        self.fail(f"Expected {count} browser requests to be waiting")

    def test_rotates_owners_and_limits_one_owners_backlog(self):
        client = Client()
        requests = BrowserRequests(client, per_owner=2)
        started, release = Event(), Event()

        def handler(operation):
            if operation == "a1":
                started.set()
                release.wait(3)
            return {"ok": True}

        client.handler = handler
        with ThreadPoolExecutor(4) as pool:
            futures = [pool.submit(requests.call, "a", "a1")]
            try:
                self.assertTrue(started.wait(2))
                for pending, owner, label in [(1, "a", "a2"), (2, "a", "a3"), (3, "b", "b1")]:
                    futures.append(pool.submit(requests.call, owner, label))
                    self.wait_for_queue(requests, pending)
                with self.assertRaises(ComputerUseError) as error:
                    requests.call("a", "a4")
                self.assertEqual(error.exception.code, "browser_queue_full")
            finally:
                release.set()
            for future in futures:
                self.assertTrue(future.result(3)["ok"])
        self.assertEqual(client.calls, [("a", "a1"), ("b", "b1"), ("a", "a2"), ("a", "a3")])

    def test_policy_is_checked_after_wait_with_no_worker_dispatch(self):
        client = Client()
        started, release = Event(), Event()
        blocked = False

        def check(write):
            if blocked and write:
                raise ComputerUseError("read_only", "read-only")

        def handler(operation):
            started.set()
            release.wait(3)
            return {}

        requests = BrowserRequests(client, policy=check)
        client.handler = handler
        with ThreadPoolExecutor(2) as pool:
            current = pool.submit(requests.call, "a", "running")
            try:
                self.assertTrue(started.wait(2))
                queued = pool.submit(requests.call, "b", "action", write=True)
                self.wait_for_queue(requests, 1)
                blocked = True
            finally:
                release.set()
            current.result(3)
            with self.assertRaises(ComputerUseError) as error:
                queued.result(3)
        self.assertEqual(error.exception.code, "read_only")
        self.assertEqual(client.calls, [("a", "running")])

    def test_queue_timeout_is_definitively_not_dispatched(self):
        client = Client()
        requests = BrowserRequests(client, queue_timeout=.05)
        started, release = Event(), Event()

        def handler(operation):
            started.set()
            release.wait(3)
            return {}

        client.handler = handler
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(requests.call, "a", "running")
            try:
                self.assertTrue(started.wait(2))
                with self.assertRaises(ComputerUseError) as error:
                    requests.call("b", "timed-out")
                self.assertEqual(error.exception.code, "browser_queue_timeout")
                self.assertEqual(error.exception.details["outcome"], "not_dispatched")
            finally:
                release.set()
            future.result(3)
        self.assertEqual(client.calls, [("a", "running")])
