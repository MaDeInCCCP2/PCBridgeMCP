"""PCBridge policy and ownership for experimental Win32 Desktop sessions."""
import atexit
import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from computer_use import ComputerUseClient, ComputerUseError
from desktop_worker import APPLICATION_EXITED
from leases import MutationLease
from uia_worker import UIA_ACTIONS


HISTORY_LIMIT = 16
PANEL_OPERATIONS_LIMIT = 10
OPERATION_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")


@dataclass
class OperationRecord:
    signature: bytes
    action: str | None
    started_at: float
    receipt: dict | None = None

    @property
    def status(self):
        return "running" if self.receipt is None else "finished"

    def summary(self, operation_id):
        receipt = self.receipt or {}
        code = receipt.get("error_code")
        outcome = receipt.get("outcome")
        verification = (receipt.get("verification") or {}).get("status")
        return {"operation_id": operation_id, "status": self.status, "action": self.action,
                "started_at": self.started_at,
                "outcome": outcome if outcome in ("completed", "executed", "unknown", "rejected") else None,
                "verification_status": verification if verification in (
                    "verified", "not_observed", "unavailable", "not_requested") else None,
                "error_code": code if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,64}", code) else None}


class Session:
    def __init__(self, id_, owner, app, client):
        self.id, self.owner, self.app, self.client = id_, owner, app, client
        self.lock = threading.Lock()
        self.pid = None
        self.final_state = None
        self.input_paused = False
        self.operations = {}
        self.lease = MutationLease()
        self.replacement_id = None

    def state(self):
        if self.final_state is not None:
            return self.final_state
        process = self.client.proc
        code = process.poll() if process is not None else None
        if self.pid is None:
            return "starting"
        if code == APPLICATION_EXITED:
            return "exited"
        return "ready" if process is not None and code is None else "lost"

    def info(self):
        return {"session_id": self.id, "owner": self.owner, "app": self.app, "pid": self.pid,
                "mode": "win32_desktop", "experimental": True, "input": "uia_semantic",
                "state": self.state(), "input_paused": self.input_paused,
                "lease_required": bool(self.lease.generation)}


class DesktopSessions:
    def __init__(self, app_config, worker, logs, *, receipt_store=None):
        self.receipt_store = receipt_store
        self.config = app_config
        self.worker = worker
        self.log = Path(logs) / "desktop-worker.log"
        self.sessions = {}
        self.history = {}
        self.lock = threading.Lock()
        atexit.register(self.close_all)

    def _allowed_app(self, app, who):
        if not isinstance(self.config, dict):
            return None
        entry = self.config.get(app)
        if not isinstance(entry, dict):
            return None
        owners = entry.get("clients", [])
        if who == "owner" or (isinstance(owners, list) and who in owners):
            return entry
        return None

    def apps(self, who):
        names = self.config.keys() if isinstance(self.config, dict) else ()
        return {"apps": [{"id": name} for name in names if self._allowed_app(name, who) is not None],
                "experimental": True}

    @staticmethod
    def launch_config(entry):
        executable = entry.get("executable")
        args = entry.get("args", [])
        cwd = entry.get("cwd")
        if (not isinstance(executable, str) or not isinstance(args, list)
                or not all(isinstance(arg, str) and "\0" not in arg for arg in args)
                or (cwd is not None and not isinstance(cwd, str))):
            raise ComputerUseError("desktop_config_invalid", "Check the locally configured executable, args and cwd.")
        try:
            target = Path(executable)
            work = Path(cwd) if cwd is not None else target.parent
            # CreateProcess receives an explicit executable and argv, never a shell command string.
            if not (target.is_absolute() and target.suffix.lower() == ".exe" and target.is_file()
                    and work.is_absolute() and work.is_dir()):
                raise ValueError
        except (OSError, ValueError) as error:
            raise ComputerUseError("desktop_config_invalid", "Use an absolute .exe and an existing absolute cwd.") from error
        return {"executable": str(target), "cwd": str(work), "args": args}

    def _collect_finished(self):
        """Move ended workers out of the active quota. Called with the registry lock held."""
        for id_, session in list(self.sessions.items()):
            # Other requests take the session lock first and may need the registry lock.
            if not session.lock.acquire(blocking=False):
                continue
            try:
                state = session.state()
                if state in ("exited", "lost"):
                    self._archive(session, state)
            finally:
                session.lock.release()
    def _archive(self, session, state):
        session.final_state = state
        session.client.close()
        self.history[session.id] = self.sessions.pop(session.id)
        if len(self.history) > HISTORY_LIMIT:
            self.history.pop(next(iter(self.history)))

    def open(self, app, who, session_limit=2):
        entry = self._allowed_app(app, who)
        if entry is None:
            raise ComputerUseError("desktop_app_denied", "The application is unavailable to this client.")
        launch = self.launch_config(entry)
        id_ = uuid.uuid4().hex
        client = ComputerUseClient(self.worker, self.log.with_name(f"desktop-worker-{id_}.log"), timeout=15,
                                   worker_args=["--parent", str(os.getpid())], close_at_exit=False)
        session = Session(id_, who, app, client)
        with session.lock:
            with self.lock:
                self._collect_finished()
                if len(self.sessions) >= 4 or sum(s.owner == who for s in self.sessions.values()) >= session_limit:
                    raise ComputerUseError("desktop_session_limit", "Close a desktop session before opening another.")
                self.sessions[id_] = session
            try:
                session.pid = client.call("start", launch, timeout=20)["pid"]
            except BaseException:
                try:
                    client.close()
                finally:
                    with self.lock:
                        self.sessions.pop(id_, None)
                raise
        return session.info()

    def application(self, id_, who):
        with self.lock:
            session = self._get_session(id_)
            if session is None or session.owner != who:
                raise ComputerUseError("desktop_session_missing", "Desktop session is unavailable to this client.")
            return session.app

    def recover(self, id_, who, session_limit=2):
        with self._locked(id_, who) as session:
            state = session.state()
            if state not in ("lost", "exited"):
                raise ComputerUseError("desktop_recovery_active", "The previous session is still running.")
            with self.lock:
                if session.replacement_id:
                    replacement = self._get_session(session.replacement_id)
                    if replacement is None:
                        raise ComputerUseError("desktop_recovery_finished", "The replacement is closed. Open a new session.")
                    return {**replacement.info(), "replaces_session_id": id_, "already_recovered": True}
                if id_ in self.sessions:
                    self._archive(session, state)
            replacement = self.open(session.app, who, session_limit=session_limit)
            session.replacement_id = replacement["session_id"]
            return {**replacement, "replaces_session_id": id_, "already_recovered": False}

    def _get_session(self, id_):
        return self.sessions.get(id_) or self.history.get(id_)

    @contextmanager
    def _locked(self, id_, who, *, local_owner=False):
        with self.lock:
            session = self._get_session(id_)
        if session is None or (session.owner != who and not local_owner):
            raise ComputerUseError("desktop_session_missing", "Desktop session is unavailable to this client.")
        if not session.lock.acquire(timeout=2):
            raise ComputerUseError("desktop_session_busy", "Another request is using this desktop session.")
        try:
            with self.lock:
                if self._get_session(id_) is not session:
                    raise ComputerUseError("desktop_session_missing", "Desktop session has already closed.")
            yield session
        finally:
            session.lock.release()

    def list(self, who=None, *, include_operations=False):
        with self.lock:
            self._collect_finished()
            sessions = (*self.sessions.values(), *self.history.values())
            result = []
            for session in sessions:
                if who is not None and who != session.owner:
                    continue
                info = session.info()
                if include_operations:
                    recent = islice(reversed(session.operations.items()), PANEL_OPERATIONS_LIMIT)
                    info["operations"] = [record.summary(id_) for id_, record in recent]
                    info["operation_count"] = len(session.operations)
                result.append(info)
            return {"sessions": result}

    def set_input_paused(self, id_, paused):
        """Set input policy from the authenticated local control panel."""
        with self.lock:
            session = self.sessions.get(id_)
            if session is None or session.state() != "ready":
                raise ComputerUseError("desktop_session_missing", "This desktop session is no longer active.")
            session.input_paused = paused
            return {"ok": True, "session_id": id_, "input_paused": paused}

    def close(self, id_, who, *, local_owner=False):
        with self._locked(id_, who, local_owner=local_owner) as session:
            if session.final_state is None:
                session.client.close()
            with self.lock:
                self.sessions.pop(id_, None)
                self.history.pop(id_, None)
        return {"closed_session_id": id_}

    def close_all(self):
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.client.close()

    @staticmethod
    def _call(session, operation, arguments, *, timeout=None):
        state = session.state()
        if state == "ready":
            result = session.client.call(operation, arguments, timeout=timeout, start=False)
            if result != {"sessions": []}:
                return {"session_id": session.id, **result}
            state = session.state()
        if state == "exited":
            raise ComputerUseError("desktop_application_exited", "All processes in this desktop session have exited.")
        raise ComputerUseError("desktop_session_lost", "The desktop worker ended. Open a new desktop session.")

    def observe(self, id_, who, operation, arguments=None, *, wait_seconds=0):
        with self._locked(id_, who) as session:
            timeout = session.client.timeout + wait_seconds if wait_seconds else None
            return self._call(session, operation, arguments or {}, timeout=timeout)

    @staticmethod
    def _validate_operation_id(operation_id):
        if not OPERATION_ID.fullmatch(operation_id):
            raise ComputerUseError("invalid_operation_id", "Use an operation ID of 16 to 64 characters.")

    def operation(self, id_, who, operation_id, *, summary=False):
        self._validate_operation_id(operation_id)
        with self.lock:
            session = self._get_session(id_)
            owned = session is not None and session.owner == who
            record = session.operations.get(operation_id) if owned else None
            if record is not None:
                receipt = record.receipt
                if summary and receipt is not None:
                    receipt = self._persistent_receipt(receipt)
                return {"ok": True, "session_id": id_, "operation_id": operation_id,
                        "status": record.status, "receipt": deepcopy(receipt)}
            if self.receipt_store is not None:
                stored = self.receipt_store.get(json.dumps((who, id_)), operation_id)
                if stored is not None:
                    return {"ok": True, "session_id": id_, "operation_id": operation_id,
                            "status": "finished", "receipt": stored[1] or self._unknown_receipt(id_, operation_id)}
            if not owned:
                raise ComputerUseError("desktop_session_missing", "Desktop session is unavailable to this client.")
            raise ComputerUseError("desktop_operation_missing", "No retained receipt for this operation ID.")

    def acquire_lease(self, id_, who, seconds, renew_fence=None):
        with self._locked(id_, who) as session:
            if session.state() != "ready":
                raise ComputerUseError("desktop_session_missing", "Desktop session is no longer active.")
            return {"session_id": id_, **session.lease.acquire(seconds, renew_fence)}

    def release_lease(self, id_, who, fence):
        with self._locked(id_, who) as session:
            session.lease.release(fence)
        return {"session_id": id_, "released": True}

    @staticmethod
    def _unknown_receipt(id_, operation_id):
        return {"session_id": id_, "operation_id": operation_id, "outcome": "unknown", "ok": False,
                "action_executed": None, "retry_action": False}

    @staticmethod
    def _persistent_receipt(receipt):
        saved = {key: receipt[key] for key in ("session_id", "operation_id", "outcome", "ok",
                                               "action_executed", "retry_action", "observation_failed") if key in receipt}
        if saved["outcome"] not in ("completed", "executed", "unknown", "rejected"):
            saved["outcome"] = "unknown"
        code = receipt.get("error_code")
        if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,64}", code):
            saved["error_code"] = code
        verification = receipt.get("verification")
        if isinstance(verification, dict) and verification.get("status") in (
                "verified", "not_observed", "unavailable", "not_requested"):
            saved["verification"] = {"status": verification["status"], "verified": verification.get("verified") is True}
        return saved

    def act(self, id_, who, operation_id, arguments, *, fence=None):
        self._validate_operation_id(operation_id)
        store = self.receipt_store
        scope = json.dumps((who, id_))
        signature = (store.fingerprint(arguments) if store is not None
                     else hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).digest())
        with self._locked(id_, who) as session:
            session.lease.authorize(fence)
            previous = session.operations.get(operation_id)
            if previous:
                if previous.signature != signature:
                    raise ComputerUseError("operation_conflict", "Operation ID already belongs to another action.")
                return {**previous.receipt, "already_dispatched": True, "hint": "Use desktop_snapshot for current refs."}
            receipt = self._unknown_receipt(id_, operation_id)
            action = arguments.get("action", "invoke")
            record = OperationRecord(signature, action if isinstance(action, str) and action in UIA_ACTIONS else None,
                                     time.time())
            with self.lock:
                if session.input_paused:
                    raise ComputerUseError("desktop_input_paused", "The PC owner paused input for this desktop session.")
                if len(session.operations) >= 512:
                    raise ComputerUseError("operation_limit", "The session has reached its recorded action limit.")
                session.lease.authorize(fence)
                if store is not None:
                    stored = store.reserve(scope, operation_id, signature)
                    if stored is not None:
                        if stored[0] != signature:
                            raise ComputerUseError("operation_conflict", "Operation ID already belongs to another action.")
                        return {**(stored[1] or receipt), "already_dispatched": True}
                session.operations[operation_id] = record
            result = {}
            try:
                result = self._call(session, "action", arguments)
                verification = result["verification"]
                status = verification["status"]
                receipt.update(ok=True, outcome="completed", action_executed=True, verification=verification,
                               observation_failed="observation_error" in result)
                if status == "not_observed":
                    receipt.update(ok=False, outcome="executed", error_code="desktop_goal_not_observed",
                                   error="Action executed, but the expected result was not observed.")
                elif status == "unavailable":
                    receipt.update(ok=False, outcome="executed", error_code="desktop_verification_unavailable",
                                   error="Action executed, but the expected result could not be checked.")
            except ComputerUseError as error:
                outcome = error.details.get("outcome", "rejected")
                receipt.update(outcome=outcome, error_code=error.code,
                               action_executed=None if outcome == "unknown" else outcome == "executed")
            finally:
                if store is not None:
                    try:
                        store.complete(scope, operation_id, self._persistent_receipt(receipt))
                    except ComputerUseError as error:
                        result = {}
                        receipt = {**self._unknown_receipt(id_, operation_id), "error_code": error.code}
                with self.lock:
                    record.receipt = deepcopy(receipt)
            return {**result, **receipt}
