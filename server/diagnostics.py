"""Bounded structured tool diagnostics; tool arguments are deliberately excluded."""
import json
import re
import threading
import time


DIAGNOSTIC_LIMIT = 250
REPLAY_BODY_LIMIT = 16 * 1024
TAIL_BYTES = 256 * 1024


def _tail(path):
    try:
        with path.open("rb") as stream:
            start = max(0, stream.seek(0, 2) - TAIL_BYTES)
            stream.seek(start)
            raw = stream.read(TAIL_BYTES)
    except OSError:
        return []
    if start:
        raw = raw.partition(b"\n")[2]
    return raw.rsplit(b"\n", 1)[0].splitlines() if b"\n" in raw else []


def _safe_event(data):
    if not isinstance(data, dict) or data.get("event") != "tool_finished":
        return None
    at, duration = data.get("ts"), data.get("duration_ms")
    tool, correlation = data.get("tool"), data.get("correlation_id")
    outcome, code = data.get("outcome"), data.get("error_code")
    if (type(at) not in (int, float) or not 0 < at <= 253402300799
            or type(duration) not in (int, float) or not 0 <= duration <= 10**12
            or not isinstance(tool, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", tool)
            or not isinstance(correlation, str) or not re.fullmatch(r"[a-f0-9]{32}", correlation)
            or outcome not in ("ok", "error")):
        return None
    return {
        "ts": at, "duration_ms": duration, "tool": tool, "correlation_id": correlation,
        "outcome": outcome,
        "error_code": code if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,64}", code) else None,
    }


def recent_diagnostics(path, *, principal=None):
    """Project bounded log tails onto safe fields, optionally limited to one caller."""
    events = {}
    for file in (path.with_suffix(".old.jsonl"), path):
        for line in _tail(file):
            try:
                data = json.loads(line)
                event = _safe_event(data)
                if event is not None and principal is not None and data.get("client_id") != principal:
                    continue
            except (ValueError, RecursionError):
                continue
            if event is not None:
                events[event["correlation_id"]] = event
    return sorted(events.values(), key=lambda row: row["ts"], reverse=True)[:DIAGNOSTIC_LIMIT]


class Diagnostics:
    def __init__(self, path, max_bytes=2_000_000):
        self.path = path
        self.max_bytes = max_bytes
        self.lock = threading.Lock()

    def tool_finished(self, correlation_id, principal, name, started, *, result=None, error=None):
        data = result if isinstance(result, dict) else {}
        code = getattr(error, "code", None) or data.get("error_code")
        code = code if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_]{1,64}", code) else None
        outcome = "error" if error or data.get("ok") is False or data.get("error") else "ok"
        entry = {
            "ts": time.time(), "correlation_id": correlation_id, "client_id": principal,
            "component": "mcp", "event": "tool_finished", "tool": name, "outcome": outcome,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
            "error_code": code, "secret_fields_redacted": True,
        }
        with self.lock:
            try:
                if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                    self.path.replace(self.path.with_suffix(".old.jsonl"))
                with self.path.open("a", encoding="utf-8") as log:
                    log.write(json.dumps(entry) + "\n")
            except OSError:
                pass
