"""Owner isolation, operation receipts and real WinForms on a non-input desktop."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch


APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "server"))
from computer_use import ComputerUseError
from desktop_sessions import DesktopSessions, Session
from desktop_worker import APPLICATION_EXITED
from operation_receipts import OperationReceiptStore


class DesktopPolicy(unittest.TestCase):
    def setUp(self):
        self.sessions = DesktopSessions({"first": {"clients": ["client-a"]}, "owner-tool": {}}, None, ".")
        client = Mock()
        client.proc.poll.return_value = None
        self.session = Session("example", "client-a", "first", client)
        self.session.pid = 123
        self.sessions.sessions[self.session.id] = self.session

    def test_durable_receipt_keeps_only_status_after_worker_process_is_gone(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "desk.sqlite3"
            store = OperationReceiptStore(path, b"fixture", "desktop", scope_limit=512)
            self.sessions.receipt_store = store
            self.session.client.call.return_value = {
                "verification": {"status": "verified", "verified": True, "reason": "private expectation"},
                "snapshot": {"private": "private window body"},
            }
            sid, who, op = self.session.id, self.session.owner, "desktop-restart-0001"
            sent = self.sessions.act(sid, who, op, {"value": "private typed value"})
            self.assertTrue(sent["ok"])
            self.sessions.close(sid, who)
            next_bridge = DesktopSessions({}, None, directory, receipt_store=store)
            read = next_bridge.operation(sid, who, op)
            self.assertEqual(read["status"], "finished")
            self.assertEqual(read["receipt"]["verification"], {"status": "verified", "verified": True})
            self.assertNotIn(b"private", path.read_bytes())
            with self.assertRaises(ComputerUseError) as denied:
                next_bridge.operation(sid, "client-b", op)
            self.assertEqual(denied.exception.code, "desktop_session_missing")
            with self.assertRaises(ComputerUseError) as stale:
                next_bridge.act(sid, who, op, {})
            self.assertEqual(stale.exception.code, "desktop_session_missing")

    def test_durable_reservation_without_worker_result_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OperationReceiptStore(Path(directory) / "desk.sqlite3", b"fixture", "desktop", scope_limit=512)
            key = json.dumps(("client-a", "exited-session"))
            store.reserve(key, "desktop-worker-abort", store.fingerprint({"value": "typed"}))
            recovered = DesktopSessions({}, None, directory, receipt_store=store)
            receipt = recovered.operation("exited-session", "client-a", "desktop-worker-abort")["receipt"]
            self.assertEqual(receipt["outcome"], "unknown")
            self.assertIsNone(receipt["action_executed"])
            self.assertFalse(receipt["retry_action"])

    def test_apps_and_session_handles_are_scoped(self):
        self.assertEqual(self.sessions.apps("client-a")["apps"], [{"id": "first"}])
        self.assertEqual(self.sessions.apps("client-b")["apps"], [])
        self.assertEqual(self.sessions.list("client-b")["sessions"], [])
        with self.assertRaises(ComputerUseError):
            self.sessions.close(self.session.id, "client-b")
        self.session.client.close.assert_not_called()
        self.sessions.close(self.session.id, "owner", local_owner=True)
        self.assertEqual(self.sessions.list()["sessions"], [])

    def test_expired_fence_cannot_mutate_or_reclaim_the_lease(self):
        current = [100]
        self.session.lease.clock = lambda: current[0]
        sid = self.session.id
        first = self.sessions.acquire_lease(sid, "client-a", 5)["fence"]
        self.assertTrue(self.sessions.list("client-a")["sessions"][0]["lease_required"])
        with self.assertRaises(ComputerUseError) as other:
            self.sessions.acquire_lease(sid, "client-b", 5)
        self.assertEqual(other.exception.code, "desktop_session_missing")
        with self.assertRaises(ComputerUseError) as held:
            self.sessions.acquire_lease(sid, "client-a", 5)
        self.assertEqual(held.exception.code, "lease_held")
        self.assertEqual(self.sessions.acquire_lease(sid, "client-a", 10, first)["fence"], first)
        current[0] = 110
        second = self.sessions.acquire_lease(sid, "client-a", 5)["fence"]
        self.assertGreater(second, first)
        for stale in (None, first):
            with self.assertRaises(ComputerUseError) as denied:
                self.sessions.act(sid, "client-a", "desktop-lease-test", {}, fence=stale)
            self.assertEqual(denied.exception.code, "lease_stale")
        self.session.client.call.assert_not_called()
        self.session.client.call.return_value = {"verification": {"status": "verified"}}
        self.assertTrue(self.sessions.act(sid, "client-a", "desktop-lease-test", {}, fence=second)["ok"])
        self.sessions.release_lease(sid, "client-a", second)
        self.assertEqual(self.sessions.operation(sid, "client-a", "desktop-lease-test")["status"], "finished")
        with self.assertRaises(ComputerUseError):
            self.sessions.act(sid, "client-a", "desktop-lease-next", {}, fence=second)
        self.session.client.call.assert_called_once()

    def test_desktop_profile_limit_does_not_block_another_client(self):
        self.sessions.config["first"]["clients"].append("client-b")
        with (patch.object(self.sessions, "launch_config", return_value={}),
              patch("desktop_sessions.ComputerUseClient") as worker):
            worker.return_value.call.return_value = {"pid": 9}
            worker.return_value.proc.poll.return_value = None
            with self.assertRaises(ComputerUseError) as error:
                self.sessions.open("first", "client-a", session_limit=1)
            self.assertEqual(error.exception.code, "desktop_session_limit")
            other = self.sessions.open("first", "client-b", session_limit=1)
        self.assertEqual(other["owner"], "client-b")

    def test_recovery_reclaims_the_slot_and_never_replays_previous_input(self):
        old = self.session
        with self.assertRaises(ComputerUseError) as active:
            self.sessions.recover(old.id, "client-a")
        self.assertEqual(active.exception.code, "desktop_recovery_active")
        old.client.proc.poll.return_value = APPLICATION_EXITED
        with (patch.object(self.sessions, "launch_config", return_value={}),
              patch("desktop_sessions.ComputerUseClient") as worker):
            worker.return_value.call.return_value = {"pid": 77}
            worker.return_value.proc.poll.return_value = None
            new = self.sessions.recover(old.id, "client-a", session_limit=1)
            repeat = self.sessions.recover(old.id, "client-a", session_limit=1)
        self.assertNotEqual(new["session_id"], old.id)
        self.assertEqual(repeat["session_id"], new["session_id"])
        self.assertTrue(repeat["already_recovered"])
        self.assertIn(old.id, self.sessions.history)
        old.client.call.assert_not_called()
        worker.return_value.call.assert_called_once()

    def test_unknown_action_receipt_prevents_replay(self):
        self.session.client.call.side_effect = ComputerUseError("ui_timeout", "Worker timeout", outcome="unknown")
        signature = {"snapshot_id": "some", "ref": "e1", "action": "invoke"}
        first = self.sessions.act(self.session.id, "client-a", "test-action-00001", signature)
        self.assertEqual(first["outcome"], "unknown")
        self.assertIsNone(first["action_executed"])
        self.assertFalse(first["retry_action"])
        again = self.sessions.act(self.session.id, "client-a", "test-action-00001", signature)
        self.assertTrue(again["already_dispatched"])
        self.session.client.call.assert_called_once()
        with self.assertRaises(ComputerUseError) as error:
            self.sessions.act(self.session.id, "client-a", "test-action-00001", {**signature, "ref": "e2"})
        self.assertEqual(error.exception.code, "operation_conflict")

    def test_operation_reads_inflight_and_finished_receipt_without_input(self):
        entered = threading.Event()
        release = threading.Event()
        operation_id = "desktop-poll-pending"
        sid = self.session.id
        inputs = {"snapshot_id": "private-snapshot", "ref": "private-ref", "value": "private-value"}

        def dispatch(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("Test never released the operation")
            return {"verification": {"verified": True, "status": "verified"}, "snapshot": {"secret": "private-snapshot"}}

        self.session.client.call.side_effect = dispatch
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.sessions.act, sid, "client-a", operation_id, inputs)
            try:
                self.assertTrue(entered.wait(2))
                active = self.sessions.operation(sid, "client-a", operation_id)
                self.assertEqual(active["status"], "running")
                self.assertIsNone(active["receipt"])
                panel = self.sessions.list(include_operations=True)["sessions"][0]["operations"][0]
                self.assertEqual((panel["operation_id"], panel["status"]), (operation_id, "running"))
                self.assertIsNone(panel["verification_status"])
                # Pause can take effect while the admitted action is still blocked in the worker.
                self.assertTrue(self.sessions.set_input_paused(sid, True)["input_paused"])
                with self.assertRaises(ComputerUseError) as foreign:
                    self.sessions.operation(sid, "client-b", operation_id)
                self.assertEqual(foreign.exception.code, "desktop_session_missing")
            finally:
                release.set()
            self.assertTrue(future.result(timeout=2)["ok"])

        finished = self.sessions.operation(sid, "client-a", operation_id)
        self.assertTrue(finished["ok"])
        self.assertEqual(finished["status"], "finished")
        self.assertTrue(finished["receipt"]["verification"]["verified"])
        self.assertNotIn("private-", str(finished))
        self.session.client.call.assert_called_once()
        with self.assertRaises(ComputerUseError) as paused:
            self.sessions.act(sid, "client-a", "desktop-poll-new-input", inputs)
        self.assertEqual(paused.exception.code, "desktop_input_paused")
        self.session.client.call.assert_called_once()

    def test_panel_history_is_bounded_redacted_and_preserved_for_archived_sessions(self):
        client = self.session.client
        client.call.return_value = {"verification": {"status": "verified", "verified": True, "reason": "private-text"},
                                    "snapshot": {"secret": "private-snapshot"}}
        for number in range(11):
            self.sessions.act(self.session.id, "client-a", f"desktop-history-{number:02}", {
                "action": "set_value", "value": "private-value", "ref": "private-ref"
            })
        self.assertNotIn("operations", self.sessions.list("client-a")["sessions"][0])
        client.proc.poll.return_value = APPLICATION_EXITED
        history = self.sessions.list(include_operations=True)["sessions"][0]
        self.assertEqual(history["state"], "exited")
        self.assertEqual(history["operation_count"], 11)
        self.assertEqual([op["operation_id"] for op in history["operations"]],
                         [f"desktop-history-{number:02}" for number in range(10, 0, -1)])
        self.assertEqual(history["operations"][0]["action"], "set_value")
        self.assertEqual(history["operations"][0]["verification_status"], "verified")
        self.assertNotIn("private-", str(history))
        self.assertEqual(len(client.call.call_args_list), 11)

    def test_paused_session_can_be_observed_and_resumed_independently(self):
        sid = self.session.id
        other_client = Mock()
        other_client.proc.poll.return_value = None
        other_client.call.return_value = {"verification": {"status": "verified", "verified": True}}
        other = Session("other", "client-a", "first", other_client)
        other.pid = 456
        self.sessions.sessions[other.id] = other
        args = {"snapshot_id": "before", "ref": "e1"}
        self.session.client.call.return_value = {"verification": {"status": "verified", "verified": True}}
        self.sessions.act(sid, "client-a", "desktop-input-before", args)

        self.sessions.set_input_paused(sid, True)
        status = {row["session_id"]: row["input_paused"] for row in self.sessions.list("client-a")["sessions"]}
        self.assertEqual(status, {sid: True, other.id: False})
        self.assertTrue(self.sessions.operation(sid, "client-a", "desktop-input-before")["ok"])
        self.assertTrue(self.sessions.act(sid, "client-a", "desktop-input-before", args)["already_dispatched"])
        with self.assertRaises(ComputerUseError) as denied:
            self.sessions.act(sid, "client-a", "desktop-input-after", args)
        self.assertEqual(denied.exception.code, "desktop_input_paused")
        with self.assertRaises(ComputerUseError) as absent:
            self.sessions.operation(sid, "client-a", "desktop-input-after")
        self.assertEqual(absent.exception.code, "desktop_operation_missing")
        self.session.client.call.assert_called_once()

        self.session.client.call.return_value = {"snapshot_id": "fresh", "elements": []}
        self.assertEqual(self.sessions.observe(sid, "client-a", "snapshot")["snapshot_id"], "fresh")
        self.assertTrue(self.sessions.act(other.id, "client-a", "desktop-input-other", args)["ok"])

        self.assertFalse(self.sessions.set_input_paused(sid, False)["input_paused"])
        self.session.client.call.return_value = {"verification": {"status": "verified", "verified": True}}
        self.assertTrue(self.sessions.act(sid, "client-a", "desktop-input-after", args)["ok"])
        self.assertEqual([call.args[0] for call in self.session.client.call.call_args_list], ["action", "snapshot", "action"])
        self.session.client.proc.poll.return_value = APPLICATION_EXITED
        with self.assertRaises(ComputerUseError) as missing:
            self.sessions.set_input_paused(sid, True)
        self.assertEqual(missing.exception.code, "desktop_session_missing")

    def test_desktop_wait_uses_the_owned_worker_and_preserves_timeout_budget(self):
        worker = self.session.client
        worker.timeout = 15
        worker.call.return_value = {"ok": True, "condition_met": True}
        arguments = {"window": "Test", "timeout_seconds": 30}
        with self.assertRaises(ComputerUseError) as denied:
            self.sessions.observe(self.session.id, "client-b", "wait", arguments, wait_seconds=30)
        self.assertEqual(denied.exception.code, "desktop_session_missing")
        worker.call.assert_not_called()
        self.sessions.set_input_paused(self.session.id, True)
        result = self.sessions.observe(self.session.id, "client-a", "wait", arguments, wait_seconds=30)
        self.assertEqual(result["session_id"], self.session.id)
        self.assertTrue(result["condition_met"])
        worker.call.assert_called_once_with("wait", arguments, timeout=45, start=False)

    def test_operation_reports_missing_without_contacting_worker(self):
        for operation_id, error_code in (("short", "invalid_operation_id"),
                                         ("desktop-missing-receipt", "desktop_operation_missing")):
            with self.subTest(operation_id=operation_id):
                with self.assertRaises(ComputerUseError) as missing:
                    self.sessions.operation(self.session.id, "client-a", operation_id)
                self.assertEqual(missing.exception.code, error_code)
        self.session.client.call.assert_not_called()

    def test_receipt_distinguishes_execution_from_verification(self):
        for number, (status, error_code) in enumerate((
                ("not_observed", "desktop_goal_not_observed"),
                ("unavailable", "desktop_verification_unavailable"),
                ("not_requested", None))):
            with self.subTest(status=status):
                client = self.session.client
                client.call.reset_mock()
                client.call.return_value = {"verification": {"verified": False, "status": status},
                                            **({"observation_error": "provider error"} if status == "unavailable" else {})}
                signature = {"snapshot_id": "some", "ref": "e1",
                             "expect_text": "" if status == "not_requested" else "goal"}
                operation_id = f"test-verification-{number}"
                first = self.sessions.act(self.session.id, "client-a", operation_id, signature)
                replay = self.sessions.act(self.session.id, "client-a", operation_id, signature)
                self.assertTrue(first["action_executed"])
                self.assertEqual(first["outcome"], "executed" if error_code else "completed")
                self.assertEqual(first["ok"], error_code is None)
                self.assertEqual(replay.get("error_code"), error_code)
                self.assertEqual(replay["verification"]["status"], status)
                self.assertEqual(replay["observation_failed"], status == "unavailable")
                self.assertTrue(replay["already_dispatched"])
                self.assertFalse(replay["retry_action"])
                client.call.assert_called_once()

    def test_clean_exit_between_check_and_request_keeps_exit_reason(self):
        self.session.client.proc.poll.side_effect = [None, APPLICATION_EXITED]
        self.session.client.call.return_value = {"sessions": []}
        with self.assertRaises(ComputerUseError) as error:
            self.sessions.observe(self.session.id, "client-a", "windows")
        self.assertEqual(error.exception.code, "desktop_application_exited")

    def test_finished_worker_returns_slot_without_losing_receipts(self):
        signature = {"snapshot_id": "before-exit", "ref": "e1", "action": "invoke"}
        self.session.client.call.return_value = {"verification": {"verified": True, "status": "verified"}}
        receipt = self.sessions.act(self.session.id, "client-a", "test-action-00001", signature)
        self.assertTrue(receipt["ok"])

        def worker(*args, **kwargs):
            client = Mock()
            client.proc.poll.return_value = None
            client.call.return_value = {"pid": 456}
            return client

        with patch.object(self.sessions, "launch_config", return_value={}), patch(
                "desktop_sessions.ComputerUseClient", side_effect=worker):
            second = self.sessions.open("first", "client-a")
            with self.assertRaises(ComputerUseError) as limited:
                self.sessions.open("first", "client-a")
            self.assertEqual(limited.exception.code, "desktop_session_limit")
            self.session.client.proc.poll.return_value = APPLICATION_EXITED
            third = self.sessions.open("first", "client-a")

        entries = {item["session_id"]: item["state"] for item in self.sessions.list("client-a")["sessions"]}
        self.assertEqual(entries, {self.session.id: "exited", second["session_id"]: "ready", third["session_id"]: "ready"})
        self.assertEqual(self.sessions.list("client-b")["sessions"], [])
        archived = self.sessions.operation(self.session.id, "client-a", "test-action-00001")
        self.assertEqual(archived["receipt"]["verification"], receipt["verification"])
        replay = self.sessions.act(self.session.id, "client-a", "test-action-00001", signature)
        self.assertTrue(replay["already_dispatched"])
        self.assertEqual(replay["verification"], receipt["verification"])
        self.session.client.call.assert_called_once()
        self.sessions.close(self.session.id, "owner", local_owner=True)
        self.session.client.close.assert_called_once()

    def test_recent_history_is_bounded_and_keeps_lost_state(self):
        self.session.client.proc.poll.return_value = 1
        for number in range(3):
            client = Mock()
            client.proc.poll.return_value = 1 if number == 1 else APPLICATION_EXITED
            session = Session(str(number), "client-a", "first", client)
            session.pid = 1000 + number
            self.sessions.sessions[session.id] = session
        with patch("desktop_sessions.HISTORY_LIMIT", 2):
            entries = self.sessions.list("client-a")["sessions"]
        self.assertEqual([(entry["session_id"], entry["state"]) for entry in entries], [("1", "lost"), ("2", "exited")])
        self.assertEqual(self.sessions.sessions, {})
        self.session.client.close.assert_called_once()
        with self.assertRaises(ComputerUseError) as missing:
            self.sessions.observe(self.session.id, "client-a", "windows")
        self.assertEqual(missing.exception.code, "desktop_session_missing")

    def test_lost_worker_never_restarts_session(self):
        self.session.client.proc.poll.return_value = 1
        with self.assertRaises(ComputerUseError) as error:
            self.sessions.observe(self.session.id, "client-a", "windows")
        self.assertEqual(error.exception.code, "desktop_session_lost")
        self.session.client.call.assert_not_called()


@unittest.skipUnless(os.name == "nt" and os.environ.get("PCBRIDGE_DESKTOP_TESTS") == "1",
                     "opt-in native Win32 Desktop test")
class Win32Desktop(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        exe = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        config = {}
        for name, script in (("fixture", "uia_form.ps1"), ("wpf-fixture", "uia_wpf.ps1")):
            fixture = APP / "tests" / "fixtures" / script
            config[name] = {"executable": str(exe), "args": ["-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                "-File", str(fixture), "-Title", "PCBridge Fixture"], "clients": ["fixture-client"]}
        self.store = OperationReceiptStore(Path(temp.name) / "desk.sqlite3", b"fixture", "desktop", scope_limit=512)
        self.sessions = DesktopSessions(config, APP / "server" / "desktop_worker.py", temp.name, receipt_store=self.store)
        self.addCleanup(self.sessions.close_all)

    def windows(self, session):
        for _ in range(40):
            result = self.sessions.observe(session["session_id"], session["owner"], "windows")
            visible = [window for window in result["windows"] if window["title"] == "PCBridge Fixture"]
            if visible:
                return visible
            time.sleep(.25)
        self.fail("WinForms fixture did not open on its desktop")

    def wait_for_exit(self, pid):
        import psutil
        try:
            psutil.Process(pid).wait(timeout=5)
        except psutil.NoSuchProcess:
            pass

    def test_owned_windows_semantic_action_and_job_cleanup(self):
        import win32gui
        a = self.sessions.open("fixture", "fixture-client")
        b = self.sessions.open("fixture", "owner")
        first, second = self.windows(a)[0], self.windows(b)[0]
        self.assertNotEqual(first["hwnd"], second["hwnd"])
        visible = []
        win32gui.EnumWindows(lambda hwnd, results: results.append(hwnd) or True, visible)
        self.assertNotIn(first["hwnd"], visible)
        self.assertNotIn(second["hwnd"], visible)
        session_id = a["session_id"]
        for inaccessible in (second["hwnd"], win32gui.GetForegroundWindow()):
            if inaccessible:
                with self.assertRaises(ComputerUseError):
                    self.sessions.observe(session_id, a["owner"], "snapshot", {"window": f"hwnd:{inaccessible}"})
        self.exercise_form(a)
        self.sessions.close(session_id, a["owner"])
        self.wait_for_exit(a["pid"])
        self.assertEqual(self.windows(b)[0]["hwnd"], second["hwnd"])

    def exercise_form(self, session):
        session_id, owner = session["session_id"], session["owner"]
        hwnd = self.windows(session)[0]["hwnd"]
        snap = self.sessions.observe(session_id, owner, "snapshot", {"window": f"hwnd:{hwnd}"})
        self.assertNotIn("DO_NOT_EXPOSE_UIA_TEST_SECRET", str(snap))
        box = next(node for node in snap["elements"] if node["name"] == "PCBridge input")
        filled = self.sessions.act(session_id, owner, "desktop-action-00001",
                                   {"snapshot_id": snap["snapshot_id"], "ref": box["ref"],
                                    "action": "set_value", "value": "background value"})
        self.assertTrue(filled["verification"]["verified"], filled)
        fresh = filled["snapshot"]
        button = next(node for node in fresh["elements"] if node["name"] == "PCBridge apply")
        args = {"snapshot_id": fresh["snapshot_id"], "ref": button["ref"], "expect_text": "Saved: background value"}
        invoked = self.sessions.act(session_id, owner, "desktop-action-00002", args)
        self.assertTrue(invoked["verification"]["verified"], invoked)
        replay = self.sessions.act(session_id, owner, "desktop-action-00002", args)
        self.assertTrue(replay["already_dispatched"])
    def test_lease_fences_real_winforms_input(self):
        session = self.sessions.open("fixture", "owner")
        sid = session["session_id"]
        hwnd = self.windows(session)[0]["hwnd"]
        snap = self.sessions.observe(sid, "owner", "snapshot", {"window": f"hwnd:{hwnd}"})
        button = next(node for node in snap["elements"] if node["name"] == "PCBridge apply")
        args = {"snapshot_id": snap["snapshot_id"], "ref": button["ref"], "expect_text": "Saved:"}
        first = self.sessions.acquire_lease(sid, "owner", 10)["fence"]
        self.sessions.release_lease(sid, "owner", first)
        current = self.sessions.acquire_lease(sid, "owner", 10)["fence"]
        with self.assertRaises(ComputerUseError) as stale:
            self.sessions.act(sid, "owner", "desktop-leased-native", args, fence=first)
        self.assertEqual(stale.exception.code, "lease_stale")
        sent = self.sessions.act(sid, "owner", "desktop-leased-native", args, fence=current)
        self.assertTrue(sent["verification"]["verified"], sent)
        self.assertTrue(self.sessions.act(sid, "owner", "desktop-leased-native", args, fence=current)["already_dispatched"])
        self.sessions.close(sid, "owner")
        next_bridge = DesktopSessions({}, None, self.store.path.parent, receipt_store=self.store)
        receipt = next_bridge.operation(sid, "owner", "desktop-leased-native")["receipt"]
        self.assertTrue(receipt["verification"]["verified"])

    def test_wpf_value_and_invoke(self):
        session = self.sessions.open("wpf-fixture", "owner")
        self.windows(session)
        self.exercise_form(session)
        self.sessions.close(session["session_id"], "owner")
        self.wait_for_exit(session["pid"])

    def test_clean_app_exit_releases_worker_and_reports_exited(self):
        session = self.sessions.open("fixture", "owner")
        second = self.sessions.open("fixture", "owner")
        hwnd = self.windows(session)[0]["hwnd"]
        sid = session["session_id"]
        worker = self.sessions.sessions[sid].client.proc
        snap = self.sessions.observe(sid, "owner", "snapshot", {"window": f"hwnd:{hwnd}"})
        close = next(node for node in snap["elements"] if node["name"] == "PCBridge exit")
        self.sessions.act(sid, "owner", "desktop-action-exit", {"snapshot_id": snap["snapshot_id"], "ref": close["ref"]})
        worker.wait(timeout=8)
        self.wait_for_exit(session["pid"])
        third = self.sessions.recover(sid, "owner")
        self.assertEqual(self.sessions.recover(sid, "owner")["session_id"], third["session_id"])
        entries = {item["session_id"]: item["state"] for item in self.sessions.list("owner")["sessions"]}
        self.assertEqual(entries[sid], "exited")
        self.assertEqual(entries[second["session_id"]], "ready")
        self.assertEqual(entries[third["session_id"]], "ready")
        restarted_hwnd = self.windows(third)[0]["hwnd"]
        self.assertNotEqual(third["session_id"], sid)
        stale = self.sessions.act(third["session_id"], "owner", "desktop-after-restart",
                                  {"snapshot_id": snap["snapshot_id"], "ref": close["ref"]})
        self.assertEqual(stale["error_code"], "stale_snapshot")
        self.assertFalse(stale["action_executed"])
        restarted = self.sessions.observe(third["session_id"], "owner", "snapshot",
                                          {"window": f"hwnd:{restarted_hwnd}"})
        self.assertNotEqual(restarted["snapshot_id"], snap["snapshot_id"])
        with self.assertRaises(ComputerUseError) as error:
            self.sessions.observe(sid, "owner", "windows")
        self.assertEqual(error.exception.code, "desktop_application_exited")
        self.sessions.close(sid, "owner")
        self.assertEqual({item["session_id"] for item in self.sessions.list("owner")["sessions"]},
                         {second["session_id"], third["session_id"]})

    def test_unobserved_goal_reports_execution_and_preserves_receipt(self):
        session = self.sessions.open("fixture", "owner")
        hwnd = self.windows(session)[0]["hwnd"]
        sid = session["session_id"]
        snap = self.sessions.observe(sid, "owner", "snapshot", {"window": f"hwnd:{hwnd}"})
        button = next(node for node in snap["elements"] if node["name"] == "PCBridge apply")
        args = {"snapshot_id": snap["snapshot_id"], "ref": button["ref"], "expect_text": "Missing fixture confirmation"}
        result = self.sessions.act(sid, "owner", "desktop-missing-goal", args)
        self.assertFalse(result["ok"])
        self.assertTrue(result["action_executed"])
        self.assertEqual(result["verification"]["status"], "not_observed")
        self.assertTrue(any("Saved:" in node["name"] for node in result["snapshot"]["elements"]))
        self.assertFalse(result["retry_action"])
        replay = self.sessions.act(sid, "owner", "desktop-missing-goal", args)
        self.assertTrue(replay["already_dispatched"])
        self.assertEqual(replay["error_code"], "desktop_goal_not_observed")

    def test_worker_crash_terminates_application_job(self):
        session = self.sessions.open("fixture", "owner")
        self.windows(session)
        id_ = session["session_id"]
        worker = self.sessions.sessions[id_].client.proc
        worker.kill()
        worker.wait(timeout=5)
        self.wait_for_exit(session["pid"])
        self.assertEqual(self.sessions.list("owner")["sessions"][0]["state"], "lost")
        with self.assertRaises(ComputerUseError):
            self.sessions.observe(id_, session["owner"], "windows")
