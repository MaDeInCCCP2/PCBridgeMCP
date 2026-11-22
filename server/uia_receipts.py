"""Durable, client-scoped receipts for foreground UI Automation actions."""
import hmac
import re
from threading import Lock
from time import time

from computer_use import ComputerUseError
from operation_receipts import OperationReceiptStore


NOT_DISPATCHED = {
    "foreground_busy", "paused", "read_only", "ui_busy", "request_too_large",
    "stale_snapshot", "stale_window", "stale_element", "unknown_ref", "element_not_ready",
    "invalid_value", "unsupported_action", "unsupported_pattern", "invalid_steps", "invalid_selector",
    "invalid_condition", "invalid_window", "window_not_found", "ambiguous_window",
}
VERIFICATION = {"verified", "not_observed", "unavailable", "not_requested"}


class UIActionReceipts:
    def __init__(self, path, secret, *, clock=time, owner_limit=1024, retention=86400):
        self.store = OperationReceiptStore(path, secret, "uia", scope_limit=owner_limit, clock=clock, retention=retention)
        self._lock = Lock()
        self._running = set()

    @staticmethod
    def _validate_id(operation_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", operation_id):
            raise ComputerUseError("invalid_operation_id", "Use an operation ID of 16 to 64 ASCII characters.")

    @staticmethod
    def _receipt(outcome, executed=None, **details):
        return {"state": "finished", "outcome": outcome, "action_executed": executed, **details}

    def _view(self, key, receipt):
        if receipt is not None:
            return receipt
        if key in self._running:
            return {"state": "running", "outcome": "pending", "action_executed": None}
        return self._receipt("unknown")

    def status(self, owner, operation_id):
        self._validate_id(operation_id)
        key = (owner, operation_id)
        with self._lock:
            existing = self.store.get(*key)
            if existing is None:
                raise ComputerUseError("ui_operation_missing", "No retained receipt for this operation.")
            operation = self._view(key, existing[1])
        return {"ok": True, "operation": {"operation_id": operation_id, **operation}}

    @staticmethod
    def _response(operation_id, receipt, replayed):
        response = {"ok": receipt["outcome"] in ("verified", "dispatched", "completed"), "replayed": replayed,
                    "operation": {"operation_id": operation_id, **receipt}}
        if "error_code" in receipt:
            response["error_code"] = receipt["error_code"]
        return response

    @classmethod
    def _workflow_receipt(cls, result):
        if not isinstance(result.get("steps"), list):
            return cls._receipt("unknown")
        count = sum(step["executed"] is True for step in result["steps"] if isinstance(step, dict) and "executed" in step)
        finished = result.get("completed_steps")
        details = {"executed_steps": count}
        if type(finished) is int and 0 <= finished <= 20:
            details["completed_steps"] = finished
        if result.get("ok") is True:
            return cls._receipt("completed", bool(count), **details)
        failed = result.get("failed_step")
        if type(failed) is int and 0 <= failed < 20:
            details["failed_step"] = failed
        code = result.get("error_code")
        if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code):
            details["error_code"] = code
        if result.get("outcome") == "unknown":
            return cls._receipt("unknown", True if count else None, **details)
        return cls._receipt("partial" if count else "not_dispatched", bool(count), **details)

    def act(self, owner, operation_id, arguments, dispatch, *, workflow=False):
        self._validate_id(operation_id)
        key = (owner, operation_id)
        fingerprint = self.store.fingerprint([workflow, arguments])
        with self._lock:
            existing = self.store.reserve(*key, fingerprint)
            if existing is not None:
                previous, stored = existing
                if not hmac.compare_digest(previous, fingerprint):
                    raise ComputerUseError("ui_operation_conflict", "Operation ID is already bound to different input.")
                return self._response(operation_id, self._view(key, stored), True)
            self._running.add(key)

        initial = {}
        receipt = self._receipt("unknown")
        try:
            initial = dispatch()
            if workflow:
                receipt = self._workflow_receipt(initial)
            else:
                verification = initial.get("verification", {}).get("status")
                if initial.get("action_executed") is True and verification in VERIFICATION:
                    outcome = {"verified": "verified", "not_requested": "dispatched"}.get(verification, "unverified")
                    receipt = self._receipt(outcome, True, verification_status=verification)
        except (OSError, ValueError) as error:
            code = getattr(error, "code", "ui_failed")
            if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,64}", code):
                receipt["error_code"] = code
                if code in NOT_DISPATCHED:
                    receipt.update(outcome="not_dispatched", action_executed=False)
        finally:
            with self._lock:
                try:
                    self.store.complete(*key, receipt)
                except ComputerUseError as error:
                    initial = {}
                    receipt = self._receipt("unknown", error_code=error.code)
                finally:
                    self._running.discard(key)
        return {**initial, **self._response(operation_id, receipt, False)}
