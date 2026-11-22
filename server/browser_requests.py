"""Serialize browser work fairly and keep bounded, owner-scoped action receipts."""
from collections import OrderedDict, deque
from contextlib import contextmanager
from hashlib import sha256
import hmac
import json
import re
from threading import Condition, Lock

from computer_use import ComputerUseError


class BrowserRequests:
    def __init__(self, client, queue_timeout=20, per_owner=4, queue_size=64,
                 operation_limit=512, history_limit=16, policy=None, receipt_store=None):
        self.client = client
        self.queue_timeout = queue_timeout
        self.per_owner = per_owner
        self.queue_size = queue_size
        self.operation_limit = operation_limit
        self.history_limit = history_limit
        self.policy = policy
        self.receipt_store = receipt_store
        self._queue = OrderedDict()
        self._waiting = 0
        self._ready = Condition()
        self._busy = False
        self._last_owner = None
        self._records = Lock()
        self._active = {}
        self._closed = OrderedDict()

    def _next_owner(self):
        owners = iter(self._queue)
        first = next(owners, None)
        return next(owners, first) if first == self._last_owner else first

    @contextmanager
    def _turn(self, owner):
        token = object()
        with self._ready:
            pending = self._queue.get(owner)
            if self._waiting >= self.queue_size or (pending is not None and len(pending) >= self.per_owner):
                raise ComputerUseError("browser_queue_full", "Browser queue is full. No request was sent.",
                                       outcome="not_dispatched")
            if pending is None:
                pending = self._queue[owner] = deque()
            pending.append(token)
            self._waiting += 1
            ready = False
            try:
                ready = self._ready.wait_for(
                    lambda: not self._busy and self._next_owner() == owner and pending[0] is token,
                    timeout=self.queue_timeout,
                )
                if not ready:
                    raise ComputerUseError("browser_queue_timeout", "Browser queue wait expired. No request was sent.",
                                           outcome="not_dispatched")
                self._busy = True
                self._last_owner = owner
            finally:
                pending.remove(token)
                self._waiting -= 1
                if not pending:
                    del self._queue[owner]
                elif ready:
                    self._queue.move_to_end(owner)
                self._ready.notify_all()
        try:
            yield
        finally:
            with self._ready:
                self._busy = False
                self._ready.notify_all()

    def _archive(self, key):
        if key in self._active:
            records = self._active.pop(key)
            if records:
                self._closed[key] = records
                while len(self._closed) > self.history_limit:
                    self._closed.popitem(last=False)

    def _reconcile(self, operation, owner, result, old_process):
        with self._records:
            if old_process is not None and old_process is not self.client.proc:
                for key in list(self._active):
                    self._archive(key)
            live = result.pop("_live_sessions", None)
            if operation == "session_inventory" and "sessions" in result:
                live = [(item["owner"], item["session_id"]) for item in result["sessions"]]
            if live is not None:
                live = {tuple(key) for key in live}
                for key in self._active.keys() - live:
                    self._archive(key)
            if operation == "sessions" and "sessions" in result:
                owned = {item["session_id"] for item in result["sessions"]}
                for key in list(self._active):
                    if key[0] == owner and key[1] not in owned:
                        self._archive(key)
            if operation == "session_open" and "session_id" in result:
                self._active.setdefault((owner, result["session_id"]), {})
            if operation == "session_close" and "closed_session_id" in result:
                self._archive((owner, result["closed_session_id"]))

    def call(self, owner, operation, arguments=None, timeout=None, *, start=True, write=False):
        arguments = arguments or {}
        with self._turn(owner):
            if self.policy and start:
                self.policy(write)
            old_process = self.client.proc
            result = {}
            try:
                result = self.client.call(operation, {**arguments, "_owner": owner}, timeout=timeout, start=start)
                return result
            except ValueError as error:
                result = getattr(error, "details", {})
                if getattr(error, "code", None) == "unknown_browser_session" and operation == "owned_action":
                    with self._records:
                        self._archive((owner, arguments["session_id"]))
                raise
            finally:
                self._reconcile(operation, owner, result, old_process)

    @staticmethod
    def _validate_id(operation_id):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", operation_id):
            raise ComputerUseError("invalid_operation_id", "Use 1 to 64 ASCII letters, digits, dots, hyphens or underscores.")

    @staticmethod
    def _response(session_id, operation_id, record, replayed):
        receipt = {"session_id": session_id, "operation_id": operation_id, **record["receipt"]}
        return {"ok": receipt["outcome"] in {"verified", "dispatched"}, "replayed": replayed, "operation": receipt}

    @staticmethod
    def _stored_record(saved):
        if saved is None:
            return None
        fingerprint, receipt = saved
        return {"fingerprint": fingerprint,
                "receipt": receipt or {"state": "finished", "outcome": "unknown", "action_executed": None}}

    def _record(self, key, operation_id):
        records = self._active.get(key, self._closed.get(key, {}))
        record = records.get(operation_id)
        if record is None and self.receipt_store is not None:
            record = self._stored_record(self.receipt_store.get(json.dumps(key), operation_id))
        return record

    @classmethod
    def _replay(cls, session_id, operation_id, record, fingerprint):
        if not hmac.compare_digest(fingerprint, record["fingerprint"]):
            raise ComputerUseError("browser_operation_conflict", "This operation ID already has different input.")
        return cls._response(session_id, operation_id, record, True)

    def status(self, owner, session_id, operation_id):
        self._validate_id(operation_id)
        key = (owner, session_id)
        with self._records:
            record = self._record(key, operation_id)
            if record is None:
                raise ComputerUseError("unknown_browser_operation", "Browser operation is unavailable.")
            return {"ok": True, "operation": self._response(session_id, operation_id, record, False)["operation"]}

    def act_once(self, owner, session_id, operation_id, arguments):
        self._validate_id(operation_id)
        key = (owner, session_id)
        scope = json.dumps(key)
        store = self.receipt_store
        if store is None:
            fingerprint = sha256(json.dumps(arguments, sort_keys=True).encode()).digest()
        else:
            fingerprint = store.fingerprint(arguments)
        with self._records:
            active = self._active.get(key)
            record = self._record(key, operation_id)
            if record is not None:
                return self._replay(session_id, operation_id, record, fingerprint)
            if active is None:
                raise ComputerUseError("unknown_browser_session", "Open an explicit browser session before submitting operations.")
            if len(active) >= self.operation_limit:
                raise ComputerUseError("browser_operation_limit", "Browser receipt limit reached. Open a new session.")
            if store is not None:
                prior = self._stored_record(store.reserve(scope, operation_id, fingerprint))
                if prior is not None:
                    return self._replay(session_id, operation_id, prior, fingerprint)
            record = {"fingerprint": fingerprint, "receipt": {"state": "running", "outcome": "pending"}}
            active[operation_id] = record

        response = {}
        receipt = {"state": "finished", "outcome": "unknown", "action_executed": None}
        try:
            response = self.call(owner, "owned_action", {**arguments, "session_id": session_id}, write=True)
            verification = response["verification"]
            receipt.update(outcome=verification["status"], action_executed=True)
        except ValueError as error:
            code = getattr(error, "code", "browser_failed")
            receipt["error_code"] = code
            if code in {"browser_queue_full", "browser_queue_timeout", "unknown_browser_session", "unknown_page",
                        "stale_snapshot", "stale_page", "unknown_ref", "unsupported_action", "invalid_value",
                        "element_not_ready", "stale_element",
                        "browser_not_started", "request_too_large", "paused", "read_only"}:
                receipt.update(outcome="not_dispatched", action_executed=False)
        finally:
            with self._records:
                if store is not None:
                    try:
                        store.complete(scope, operation_id, receipt)
                    except ComputerUseError as error:
                        response = {}
                        receipt = {"state": "finished", "outcome": "unknown", "action_executed": None,
                                   "error_code": error.code}
                record["receipt"] = receipt
        return {**response, **self._response(session_id, operation_id, record, False)}
