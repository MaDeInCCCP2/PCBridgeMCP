"""Opt-in Win32 Desktop validation during keyboard activity or Windows lock transitions."""
import atexit
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import time
import uuid

from desktop_sessions import DesktopSessions


APP = Path(__file__).resolve().parents[1]
TITLE = "PCBridge Fixture"
BUTTON = "PCBridge apply"
SESSION_LOCK, SESSION_UNLOCK = 0x7, 0x8


def monitor_scales():
    """Report only scaling factors, without monitor names, identifiers, or positions."""
    import ctypes
    from ctypes import wintypes

    import win32api

    scale_for_monitor = ctypes.WinDLL("shcore").GetScaleFactorForMonitor
    scale_for_monitor.argtypes = [wintypes.HMONITOR, ctypes.POINTER(ctypes.c_int)]
    scale_for_monitor.restype = ctypes.c_long
    scales = []
    monitors = win32api.EnumDisplayMonitors()
    for monitor, _, _ in monitors:
        scale = ctypes.c_int()
        if scale_for_monitor(int(monitor), ctypes.byref(scale)) == 0 and scale.value > 0:
            scales.append(scale.value)
    return {"monitor_count": len(monitors), "monitor_scale_pct": sorted(scales),
            "mixed_dpi": len(set(scales)) > 1 if monitors and len(scales) == len(monitors) else None}


@contextmanager
def fixture():
    exe = Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    script = APP / "tests" / "fixtures" / "uia_form.ps1"
    config = {"fixture": {"executable": str(exe), "args": [
        "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Title", TITLE,
    ]}}
    with tempfile.TemporaryDirectory(prefix="pcbridge-win32-session-") as log:
        sessions = DesktopSessions(config, APP / "server" / "desktop_worker.py", log)
        try:
            yield sessions, sessions.open("fixture", "owner")
        finally:
            sessions.close_all()
            atexit.unregister(sessions.close_all)


def ready(sessions, sid):
    result = sessions.observe(sid, "owner", "wait", {
        "window": TITLE, "text": BUTTON, "role": "Button", "timeout_seconds": 10,
    }, wait_seconds=10)
    if not result["condition_met"]:
        raise ValueError("Fixture did not expose its accessibility button.")
    return result["snapshot"]["window"]


def prepare(sessions, sid, window, value):
    snapshot = sessions.observe(sid, "owner", "snapshot", {"window": window})
    field = next((node for node in snapshot["elements"] if node["name"] == "PCBridge input"), None)
    if field is None:
        raise ValueError("Fixture did not expose its accessibility input field.")
    changed = sessions.act(sid, "owner", uuid.uuid4().hex, {
        "snapshot_id": snapshot["snapshot_id"], "ref": field["ref"], "action": "set_value", "value": value,
    })
    if not changed["ok"] or not changed["verification"]["verified"]:
        raise ValueError("Fixture input did not verify its test value.")
    snapshot = changed["snapshot"]
    button = next((node for node in snapshot["elements"] if node["name"] == BUTTON), None)
    if button is None:
        raise ValueError("Fixture did not expose its accessibility button.")
    return {"snapshot_id": snapshot["snapshot_id"], "ref": button["ref"], "expect_text": "Saved: " + value}


def invoke(sessions, sid, args):
    result = sessions.act(sid, "owner", uuid.uuid4().hex, args)
    return bool(result["ok"] and result["verification"]["verified"])


def active_input(seconds, probe_cls):
    import win32api

    with fixture() as (sessions, session):
        sid = session["session_id"]
        window = ready(sessions, sid)
        print("Fixture ready. Keep typing in your own window and leave the mouse still.", flush=True)
        attempts = overlapping = 0
        succeeded = False
        with probe_cls() as probe:
            started = time.monotonic()
            while time.monotonic() - started < seconds:
                attempts += 1
                args = prepare(sessions, sid, window, f"test-{attempts}")
                before = win32api.GetLastInputInfo()
                succeeded = invoke(sessions, sid, args)
                after = win32api.GetLastInputInfo()
                overlapping += before != after
                if not succeeded:
                    break
                time.sleep(.15)
    result = probe.result({session["pid"]})
    result.update({"scenario": "win32-active-input", "attempts": attempts,
                   "actions_with_input_activity": overlapping, "all_actions_verified": succeeded})
    if not succeeded:
        result["outcome"] = "failed"
    elif result["outcome"] == "passed" and not overlapping:
        result["outcome"] = "inconclusive"
    return result


@contextmanager
def session_notifications():
    import win32api
    import win32con
    import win32gui
    import win32ts

    events = []
    instance = win32api.GetModuleHandle(None)
    name = "PCBridge-WTS-" + uuid.uuid4().hex
    wc = win32gui.WNDCLASS()
    wc.hInstance, wc.lpszClassName = instance, name
    def notify(_hwnd, _message, change, _session):
        if change in (SESSION_LOCK, SESSION_UNLOCK):
            events.append(change)
        return 0

    wc.lpfnWndProc = {0x02B1: notify}  # WM_WTSSESSION_CHANGE
    win32gui.RegisterClass(wc)
    hwnd = None
    try:
        hwnd = win32gui.CreateWindow(name, "", win32con.WS_POPUP, 0, 0, 0, 0, 0, 0, instance, None)
        win32ts.WTSRegisterSessionNotification(hwnd, 0)  # NOTIFY_FOR_THIS_SESSION
        try:
            yield events, win32gui.PumpWaitingMessages
        finally:
            win32ts.WTSUnRegisterSessionNotification(hwnd)
    finally:
        if hwnd is not None:
            win32gui.DestroyWindow(hwnd)
        win32gui.UnregisterClass(name, instance)


def wait_for_event(events, pump, start, event, seconds):
    deadline = time.monotonic() + seconds
    while True:
        pump()
        try:
            return events.index(event, start)
        except ValueError:
            if time.monotonic() >= deadline:
                return None
            time.sleep(.05)


def lock_transition(seconds):
    with session_notifications() as (events, pump), fixture() as (sessions, session):
        sid = session["session_id"]
        window = ready(sessions, sid)
        before = invoke(sessions, sid, prepare(sessions, sid, window, "before-lock"))
        result = {"scenario": "win32-lock-transition", "before_lock": before, "lock_observed": False,
                  "unlock_observed": False, "action_while_locked": None, "after_unlock": None,
                  "outcome": "failed" if not before else "inconclusive"}
        if not before:
            return result
        pump()
        first = len(events)
        print("Fixture ready. Lock Windows, wait five seconds, then unlock it yourself.", flush=True)
        locked = wait_for_event(events, pump, first, SESSION_LOCK, seconds)
        if locked is None:
            return result
        result["lock_observed"] = True
        try:
            verified = invoke(sessions, sid, prepare(sessions, sid, window, "while-locked"))
        except (OSError, ValueError) as exc:
            verified = False
            result["locked_error_code"] = getattr(exc, "code", type(exc).__name__)
        pump()
        if SESSION_UNLOCK not in events[locked + 1:]:
            result["action_while_locked"] = verified
        unlocked = wait_for_event(events, pump, locked + 1, SESSION_UNLOCK, seconds)
        if unlocked is None:
            return result
        result["unlock_observed"] = True
        result["after_unlock"] = invoke(sessions, sid, prepare(sessions, sid, window, "after-unlock"))
        if result["action_while_locked"] is False or not result["after_unlock"]:
            result["outcome"] = "failed"
        elif result["action_while_locked"] is True:
            result["outcome"] = "passed"
        return result
