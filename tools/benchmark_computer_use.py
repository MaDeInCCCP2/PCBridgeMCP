"""Measure Windows isolation capabilities and background automation interference."""
import argparse
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from statistics import median


def windows_isolation():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
    from windows_isolation import isolation_status

    print(json.dumps({"scenario": "windows_isolation_capabilities", **isolation_status()}))
    return 0


def benchmark_browser(runs, interference=False):
    os.environ["PCBRIDGE_BROWSER_TESTS"] = "1"
    from test_browser import NativeBrowser

    timings = []
    probes = []

    class TimedScenario(NativeBrowser):
        def run(self, result=None):
            start = time.monotonic()
            try:
                if not interference:
                    return super().run(result)
                with DesktopProbe() as probe:
                    completed = super().run(result)
                    proc = self.client.proc
                    pids = browser_process_ids(proc.pid) if proc else set()
                probes.append(probe.result(pids))
                return completed
            finally:
                timings.append(round((time.monotonic() - start) * 1000, 1))

    test = ("test_explicit_session_action_receipt_prevents_second_chromium_click" if interference
            else "test_session_storage_and_page_handles_are_isolated")
    suite = unittest.TestSuite(TimedScenario(test) for _ in range(runs))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    passed = result.testsRun - len(result.errors) - len(result.failures) - len(result.skipped)
    ordered = sorted(timings)
    report = {
        "scenario": "headless_browser_interference" if interference else "headless_browser_isolation",
        "requested_runs": runs,
        "passed": max(0, passed),
        "success_rate": max(0, passed) / runs,
        "p50_task_ms": median(ordered) if ordered else None,
        "p95_task_ms": ordered[(95 * len(ordered) + 99) // 100 - 1] if ordered else None,
    }
    if interference:
        report["interference_trials"] = probes
    print(json.dumps(report))
    if passed != runs or any(probe["outcome"] == "failed" for probe in probes):
        return 1
    return 2 if any(probe["outcome"] == "inconclusive" for probe in probes) else 0


def browser_process_ids(worker_pid):
    import psutil

    try:
        return {worker_pid, *(child.pid for child in psutil.Process(worker_pid).children(recursive=True))}
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return set()


class DesktopProbe:
    """Sample input-desktop signals without injecting input or opening the clipboard."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        import pywintypes
        import win32api
        import win32gui
        import win32process

        user32 = ctypes.WinDLL("user32")
        clipboard = user32.GetClipboardSequenceNumber
        clipboard.argtypes = []
        clipboard.restype = wintypes.DWORD

        def foreground():
            hwnd = win32gui.GetForegroundWindow()
            return (hwnd, win32process.GetWindowThreadProcessId(hwnd)[1]) if hwnd else None

        self.readers = {"foreground": foreground, "cursor": win32api.GetCursorPos,
                        "clipboard": lambda: clipboard() or None}
        self.errors = (OSError, pywintypes.error)
        self.last = {}
        self.observed_pids = set()
        self.counts = dict.fromkeys(self.readers, 0)
        self.changes = dict.fromkeys(self.readers, 0)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.sample_until_stopped, daemon=True)

    def sample(self):
        for name, read in self.readers.items():
            try:
                value = read()
            except self.errors:
                value = None
            if value is None:
                continue
            if name in self.last and value != self.last[name]:
                self.changes[name] += 1
            self.last[name] = value
            self.counts[name] += 1
            if name == "foreground":
                self.observed_pids.add(value[1])

    def sample_until_stopped(self):
        while not self.stop.wait(.02):
            self.sample()

    def __enter__(self):
        self.sample()
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()
        self.sample()

    def result(self, fixture_pids):
        in_foreground = bool(fixture_pids & self.observed_pids)
        if in_foreground:
            outcome = "failed"
        elif not fixture_pids or any(count < 2 for count in self.counts.values()) or any(self.changes.values()):
            outcome = "inconclusive"
        else:
            outcome = "passed"
        return {"outcome": outcome, "fixture_foreground": in_foreground,
                "samples": self.counts, "observed_changes": self.changes}


def benchmark_desktop(runs):
    os.environ["PCBRIDGE_DESKTOP_TESTS"] = "1"
    from test_desktop_sessions import Win32Desktop

    results = []

    class Interference(Win32Desktop):
        def trial(self, app):
            started = time.monotonic()
            with DesktopProbe() as probe:
                session = self.sessions.open(app, "owner")
                try:
                    self.exercise_form(session)
                finally:
                    self.sessions.close(session["session_id"], "owner")
                    self.wait_for_exit(session["pid"])
            result = probe.result({session["pid"]})
            results.append({"app": app, "duration_ms": round((time.monotonic() - started) * 1000, 1), **result})
            self.assertFalse(result["fixture_foreground"], "The background application entered the foreground.")

        def test_winforms(self):
            self.trial("fixture")

        def test_wpf(self):
            self.trial("wpf-fixture")

    suite = unittest.TestSuite(Interference(test) for _ in range(runs) for test in ("test_winforms", "test_wpf"))
    tests = unittest.TextTestRunner(verbosity=1).run(suite)
    outcomes = [result["outcome"] for result in results]
    counts = {outcome: outcomes.count(outcome) for outcome in ("passed", "inconclusive", "failed")}
    counts["failed"] += (2 * runs - len(results) - len(tests.skipped))
    print(json.dumps({"scenario": "win32_desktop_interference", "runs_per_app": runs,
                      **counts, "skipped": len(tests.skipped), "trials": results}))
    if not tests.wasSuccessful():
        return 1
    return 0 if counts["passed"] == 2 * runs else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("browser", "browser-interference", "browser-concurrency", "win32-desktop",
                                              "win32-chromium", "win32-electron", "win32-winui", "win32-winui-dpi", "windows-isolation",
                                              "win32-active-input", "win32-lock-transition", "win32-monitor-scales"),
                        default="browser")
    parser.add_argument("--runs", type=int, help="Number of tasks or repetitions per app")
    parser.add_argument("--parallel", type=int, default=16, help="Simultaneous contexts for browser-concurrency (1–16)")
    parser.add_argument("--executable", type=Path, help="Local executable for a Chromium, Electron or WinUI fixture")
    parser.add_argument("--seconds", type=int, help="Active input duration or timeout for each lock transition")
    args = parser.parse_args()
    runs = args.runs if args.runs is not None else (50 if args.scenario in {"browser", "browser-concurrency"} else 5)
    if os.name != "nt" or not 1 <= runs <= 500:
        parser.error("This scenario needs Windows and 1 to 500 runs.")
    if args.scenario == "windows-isolation":
        raise SystemExit(windows_isolation())
    root = Path(__file__).resolve().parent.parent
    sys.path[:0] = [str(root / "tests"), str(root / "server")]
    if args.scenario == "browser-concurrency":
        from benchmark_browser_sessions import benchmark_browser_sessions

        if not 1 <= args.parallel <= 16:
            parser.error("--parallel must be between 1 and 16.")
        print(json.dumps(benchmark_browser_sessions(runs, args.parallel)))
        return
    if args.scenario == "win32-monitor-scales":
        from benchmark_win32_session import monitor_scales

        print(json.dumps({"scenario": args.scenario, **monitor_scales()}))
        return
    if args.scenario in ("win32-active-input", "win32-lock-transition"):
        from benchmark_win32_session import active_input, lock_transition

        seconds = args.seconds if args.seconds is not None else (15 if args.scenario == "win32-active-input" else 25)
        if not 2 <= seconds <= 30:
            parser.error("--seconds must be between 2 and 30.")
        if args.scenario == "win32-active-input":
            result = active_input(seconds, DesktopProbe)
        else:
            result = lock_transition(seconds)
        print(json.dumps(result))
        raise SystemExit({"passed": 0, "failed": 1, "inconclusive": 2}[result["outcome"]])
    if args.scenario == "win32-desktop":
        raise SystemExit(benchmark_desktop(runs))
    if args.scenario in ("win32-chromium", "win32-electron", "win32-winui", "win32-winui-dpi"):
        from benchmark_win32_fixtures import chromium_executable, trial

        runtime = args.scenario.removeprefix("win32-")
        if runtime in ("electron", "winui", "winui-dpi") and args.executable is None:
            parser.error(f"Provide the path to the {runtime} fixture with --executable.")
        executable = args.executable.resolve() if args.executable else chromium_executable()
        if not executable.is_file() or executable.suffix.lower() != ".exe":
            parser.error("Provide an existing Windows executable. Chromium defaults to the full Playwright Chromium.")
        results = [trial(runtime, executable, DesktopProbe) for _ in range(runs)]
        counts = {outcome: sum(row["outcome"] == outcome for row in results)
                  for outcome in ("passed", "failed", "inconclusive")}
        print(json.dumps({"scenario": args.scenario, **counts, "trials": results}))
        raise SystemExit(1 if counts["failed"] else 2 if counts["inconclusive"] else 0)
    raise SystemExit(benchmark_browser(runs, interference=args.scenario == "browser-interference"))


if __name__ == "__main__":
    main()
