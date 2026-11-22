from concurrent.futures import ThreadPoolExecutor
import hmac
import json
import sqlite3
from pathlib import Path
from queue import Empty
import subprocess
import sys
import tempfile
from threading import Event
import unittest

from test_computer_use import APP, C

sys.path.insert(0, str(APP / "server"))
from uia_receipts import ComputerUseError, UIActionReceipts


OPERATION = "uia-receipt-test-001"
OTHER = "uia-receipt-test-002"


class UIAReceipts(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "receipts.sqlite3"
        self.args = {"snapshot_id": "opaque-ref", "value": "private-typed-input", "expect_text": "private-postcondition"}
        self.receipts = UIActionReceipts(self.path, b"fixture-key")

    def test_receipt_from_previous_database_schema_is_readable(self):
        from contextlib import closing
        from time import time

        with closing(sqlite3.connect(self.path)) as database, database:
            database.execute("""CREATE TABLE uia_actions (
                client TEXT, operation_id TEXT, created REAL, fingerprint BLOB, receipt TEXT,
                PRIMARY KEY (client, operation_id)
            )""")
            fingerprint = hmac.digest(b"fixture-key", json.dumps([False, self.args], sort_keys=True).encode(), "sha256")
            database.execute("INSERT INTO uia_actions VALUES (?, ?, ?, ?, ?)",
                             ("a", OPERATION, time(), fingerprint, '{"state":"finished","outcome":"verified"}'))
        recovered = self.receipts.act("a", OPERATION, self.args, lambda: self.fail("previous receipt was not reused"))
        self.assertTrue(recovered["replayed"])
        self.assertEqual(recovered["operation"]["outcome"], "verified")

    def test_verified_replay_across_restart_preserves_status_and_ignores_snapshot(self):
        result = self.receipts.act("a", OPERATION, self.args, lambda: {
            "action_executed": True, "verification": {"status": "verified"},
            "snapshot": {"contains": "private-window-data"},
        })
        self.assertTrue(result["ok"])
        self.assertIn("snapshot", result)
        restarted = UIActionReceipts(self.path, b"fixture-key")
        repeated = restarted.act("a", OPERATION, self.args, lambda: self.fail("UI action re-ran"))
        self.assertTrue(repeated["replayed"])
        self.assertNotIn("snapshot", repeated)
        self.assertEqual(repeated["operation"], result["operation"])
        with self.assertRaises(ComputerUseError) as foreign:
            restarted.status("b", OPERATION)
        self.assertEqual(foreign.exception.code, "ui_operation_missing")
        with self.assertRaises(ComputerUseError) as conflict:
            restarted.act("a", OPERATION, {**self.args, "expect_text": "different"}, self.fail)
        self.assertEqual(conflict.exception.code, "ui_operation_conflict")
        self.assertNotIn(b"private-", self.path.read_bytes())
        self.assertNotIn(b"opaque-ref", self.path.read_bytes())

    def test_read_while_pending_includes_no_second_dispatch(self):
        entered, release = Event(), Event()

        def dispatch():
            entered.set()
            if not release.wait(4):
                self.fail("Test did not release pending action")
            return {"action_executed": True, "verification": {"status": "not_observed"}}

        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(self.receipts.act, "a", OPERATION, self.args, dispatch)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(self.receipts.status("a", OPERATION)["operation"]["state"], "running")
                again = self.receipts.act("a", OPERATION, self.args, lambda: self.fail("Pending action re-ran"))
                self.assertTrue(again["replayed"])
                self.assertEqual(again["operation"]["outcome"], "pending")
            finally:
                release.set()
            finished = pending.result(2)
        self.assertFalse(finished["ok"])
        self.assertTrue(finished["operation"]["action_executed"])
        self.assertEqual(finished["operation"]["outcome"], "unverified")

    def test_lost_worker_reply_is_unknown_and_new_observation_recovers(self):
        worker = C.ComputerUseClient(APP / "tests" / "fixtures" / "uia_fake_worker.py",
                                     self.path.with_suffix(".log"), timeout=.5, close_at_exit=False)
        self.addCleanup(worker.close)
        worker.call("snapshot")
        from unittest.mock import patch

        with patch.object(worker.messages, "get", side_effect=Empty):
            action = self.receipts.act("a", OPERATION, self.args, lambda: worker.call("action", self.args))
        self.assertEqual(action["operation"]["outcome"], "unknown")
        self.assertIsNone(action["operation"]["action_executed"])
        self.assertTrue(worker.call("snapshot")["ok"])
        restarted = UIActionReceipts(self.path, b"fixture-key")
        self.assertTrue(restarted.act("a", OPERATION, self.args, lambda: self.fail("Unknown action re-ran"))["replayed"])

    def test_expired_receipts_do_not_block_another_client_or_future_requests(self):
        now = [100]
        store = UIActionReceipts(self.path, b"fixture-key", clock=lambda: now[0], owner_limit=1, retention=10)

        def stale():
            raise ComputerUseError("stale_snapshot", "private error detail")

        blocked = store.act("a", OPERATION, self.args, stale)
        self.assertEqual(blocked["operation"]["outcome"], "not_dispatched")
        self.assertNotIn("private", str(blocked))
        with self.assertRaises(ComputerUseError) as capped:
            store.act("a", OTHER, self.args, stale)
        self.assertEqual(capped.exception.code, "ui_operation_limit")
        self.assertFalse(store.act("b", OTHER, self.args, stale)["ok"])
        now[0] = 111
        with self.assertRaises(ComputerUseError) as expired:
            store.status("a", OPERATION)
        self.assertEqual(expired.exception.code, "ui_operation_missing")
        self.assertFalse(store.act("a", OTHER, self.args, stale)["ok"])

    def test_partial_workflow_records_executed_prefix_without_repeating_it(self):
        args = {"steps": [{"value": "private"}, {"text": "private confirmation"}]}
        result = self.receipts.act("a", OPERATION, args, lambda: {
            "ok": False, "error_code": "ui_condition_timeout", "outcome": "not_executed",
            "failed_step": 1, "completed_steps": 1, "steps": [{"executed": True}, {"executed": False}],
        }, workflow=True)
        self.assertEqual(result["operation"]["outcome"], "partial")
        self.assertEqual(result["operation"]["executed_steps"], 1)
        self.assertEqual(result["operation"]["completed_steps"], 1)
        replay = self.receipts.act("a", OPERATION, args, lambda: self.fail("workflow replayed"), workflow=True)
        self.assertEqual(replay["operation"], result["operation"])
        self.assertNotIn("steps", replay)
        with self.assertRaises(ComputerUseError):
            self.receipts.act("a", OPERATION, args, lambda: self.fail("workflow ID accepted by action"))

    def test_terminated_parent_reservation_survives_as_unknown(self):
        command = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from uia_receipts import UIActionReceipts
store = UIActionReceipts(Path(sys.argv[2]), b'fixture-key')
store.act('a', sys.argv[3], {'ref': 'e1'}, lambda: os._exit(12))
"""
        result = subprocess.run([sys.executable, "-c", command, str(APP / "server"),
                                 str(self.path), OPERATION], timeout=10)
        self.assertEqual(result.returncode, 12)
        unknown = self.receipts.status("a", OPERATION)
        self.assertEqual(unknown["operation"]["outcome"], "unknown")
        duplicate = self.receipts.act("a", OPERATION, {"ref": "e1"}, lambda: self.fail("Abandoned action re-ran"))
        self.assertTrue(duplicate["replayed"])
