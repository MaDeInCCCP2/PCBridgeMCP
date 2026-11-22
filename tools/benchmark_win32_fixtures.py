"""Exercise approved local fixtures through Win32 Desktop and Windows UI Automation."""
import atexit
import re
from pathlib import Path
import tempfile
import time

from desktop_sessions import DesktopSessions


TARGETS = {
    "chromium": ("PCBridge WebGL Fixture", "PCBridge change pixel"),
    "electron": ("PCBridge WebGL Fixture", "PCBridge change pixel"),
    "winui": ("PCBridge WinUI Fixture", "PCBridge apply"),
    "winui-dpi": ("PCBridge WinUI Fixture", "PCBridge move to another monitor"),
}
RENDERER = "PCBridge WebGL renderer: "
MODES = ("hardware-reported", "software", "unknown", "unavailable")
FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def chromium_executable():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return Path(playwright.chromium.executable_path)


def fixture_config(runtime, exe, profile):
    if runtime in ("winui", "winui-dpi"):
        args = ["--monitor-test"] if runtime == "winui-dpi" else []
    elif runtime == "electron":
        args = [str(FIXTURES / "desktop_electron.cjs"), str(profile)]
    else:
        args = [
            f"--app={(FIXTURES / 'desktop_webgl.html').as_uri()}",
            f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
            "--enable-features=UiaProvider", "--force-renderer-accessibility=complete",
        ]
    return {runtime: {"executable": str(exe), "args": args}}


def snapshot_names(snapshot):
    return {node["name"] for node in snapshot["elements"]}


def render_mode(snapshot):
    names = snapshot_names(snapshot)
    return next((mode for mode in MODES if RENDERER + mode in names), None)


def exercise(sessions, sid, fixture_pids, runtime):
    import win32gui

    if runtime == "winui-dpi":
        from benchmark_win32_session import monitor_scales

        if monitor_scales()["monitor_count"] < 2:
            return {"monitor_transition": False, "dpi_changed": False}

    window_title, button_name = TARGETS[runtime]
    target = sessions.observe(sid, "owner", "wait", {
        "window": window_title, "text": button_name, "role": "Button", "max_nodes": 350,
        "max_depth": 12, "timeout_seconds": 10,
    }, wait_seconds=10)
    if not target["condition_met"]:
        raise ValueError("The fixture button was unavailable through Windows UI Automation.")
    windows = sessions.observe(sid, "owner", "windows")["windows"]
    hwnd = int(target["snapshot"]["window"].removeprefix("hwnd:"))
    fixture_pids.update(window["pid"] for window in windows)
    visible = set()
    win32gui.EnumWindows(lambda address, _: visible.add(address) or True, None)
    if hwnd in visible:
        raise ValueError("The fixture window appeared on the input desktop.")
    buttons = [button for button in target["matches"] if button["name"] == button_name]
    if len(buttons) != 1 or "invoke" not in buttons[0]["actions"]:
        raise ValueError("The fixture needs one unambiguous UI Automation Invoke target.")
    before = snapshot_names(target["snapshot"])
    web = runtime in ("chromium", "electron")
    mode = render_mode(target["snapshot"]) if web else None
    if web and mode is None:
        raise ValueError("The fixture did not report its WebGL renderer.")
    goal = "PCBridge monitor moved:" if runtime == "winui-dpi" else "PCBridge clicks: 1"
    action_args = {"snapshot_id": target["snapshot"]["snapshot_id"], "ref": buttons[0]["ref"],
                   "expect_text": goal, "wait_seconds": 4}
    operation_id = "win32-web-click-once"
    action = sessions.act(sid, "owner", operation_id, action_args)
    if not action["ok"] or not action["verification"]["verified"]:
        raise ValueError("The fixture did not verify Invoke: " + action.get("error_code", "unverified"))
    replay = sessions.act(sid, "owner", operation_id, action_args)
    after = sessions.observe(sid, "owner", "snapshot",
                             {"window": f"hwnd:{hwnd}", "max_nodes": 350, "max_depth": 12})
    names = snapshot_names(after)
    if runtime == "winui-dpi":
        match = re.search(r"PCBridge monitor moved: (\d+) to (\d+)", " ".join(names))
        if not replay["already_dispatched"] or match is None:
            raise ValueError("The fixture did not verify the monitor transition.")
        before, after = map(int, match.groups())
        return {"monitor_transition": True, "dpi_before": before, "dpi_after": after, "dpi_changed": before != after}
    if not replay["already_dispatched"] or "PCBridge clicks: 1" not in names or "PCBridge clicks: 2" in names:
        raise ValueError("The fixture did not verify the action ran exactly once.")
    if runtime == "winui":
        return {"semantic_invoke_verified": True}
    red, green = "PCBridge pixel: red verified", "PCBridge pixel: green verified"
    if mode != "unavailable" and (red not in before or green not in names):
        raise ValueError("The WebGL framebuffer did not contain the expected color after Invoke.")
    return {"renderer": mode, "pixel_verified": mode != "unavailable", "semantic_invoke_verified": True}


def trial(runtime, exe, probe_cls):
    import psutil

    worker = FIXTURES.parents[1] / "server" / "desktop_worker.py"
    started = time.monotonic()
    info = {}
    pids = set()
    with tempfile.TemporaryDirectory(prefix="pcbridge-win32-web-") as directory:
        root = Path(directory)
        profile = root / "profile"
        profile.mkdir()
        sessions = DesktopSessions(fixture_config(runtime, exe, profile), worker, root)
        with probe_cls() as probe:
            try:
                session = sessions.open(runtime, "owner")
                pids.add(session["pid"])
                info = exercise(sessions, session["session_id"], pids, runtime)
            except (OSError, ValueError) as exc:
                info = {"test_error": getattr(exc, "code", str(exc))}
            finally:
                processes = []
                for pid in pids:
                    try:
                        process = psutil.Process(pid)
                        processes.extend([process, *process.children(recursive=True)])
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                sessions.close_all()
                atexit.unregister(sessions.close_all)
                _, remaining = psutil.wait_procs(processes, timeout=5)
                if remaining:
                    info["test_error"] = "Fixture processes did not exit after the session closed."
    result = {**info, **probe.result(pids), "duration_ms": round((time.monotonic() - started) * 1000, 1)}
    if "test_error" in result or result["fixture_foreground"]:
        result["outcome"] = "failed"
    elif result.get("pixel_verified") is False or result.get("dpi_changed") is False:
        result["outcome"] = "inconclusive"
    return result
