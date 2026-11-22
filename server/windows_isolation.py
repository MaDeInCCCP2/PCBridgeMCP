"""Read-only probes for Windows isolation backends."""
import os
from pathlib import Path
import shutil


def isolation_status():
    available = {"platform": "windows" if os.name == "nt" else "unsupported",
                 "child_sessions": "unsupported", "child_session_present": None,
                 "child_session_query_error": 0, "sandbox_cli_detected": False,
                 "sandbox_legacy_launcher_detected": False}
    if os.name != "nt":
        return available

    import ctypes
    from ctypes import wintypes

    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    enabled = wintypes.BOOL()
    query = wts.WTSIsChildSessionsEnabled
    query.argtypes = [ctypes.POINTER(wintypes.BOOL)]
    query.restype = wintypes.BOOL
    if not query(ctypes.byref(enabled)):
        available["child_sessions"] = "unknown"
        available["child_session_query_error"] = ctypes.get_last_error()
    elif not enabled.value:
        available.update(child_sessions="disabled", child_session_present=False)
    else:
        available["child_sessions"] = "enabled"
        id_ = wintypes.ULONG()
        query = wts.WTSGetChildSessionId
        query.argtypes = [ctypes.POINTER(wintypes.ULONG)]
        query.restype = wintypes.BOOL
        if query(ctypes.byref(id_)):
            available["child_session_present"] = id_.value != 0xFFFFFFFF
        else:
            available["child_session_query_error"] = ctypes.get_last_error()

    native = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsSandbox.exe"
    available["sandbox_cli_detected"] = bool(shutil.which("wsb.exe"))
    available["sandbox_legacy_launcher_detected"] = native.is_file() or bool(shutil.which("WindowsSandbox.exe"))
    return available
