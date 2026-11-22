import tempfile
import unittest
import time
from pathlib import Path

from test_computer_use import module


router = module("automation_router")
diagnostics = module("diagnostics")
BACKENDS = {
    "browser": {"available": True, "input_enabled": True},
    "native": {"tools": ["list_windows"]},
    "uia": {"available": True, "input_enabled": True},
    "visual": {"available": True, "input_enabled": True},
    "win32_desktop": {"available": True, "input_enabled": True},
}


class Router(unittest.TestCase):
    def test_headless_browser_matches_noninterference_constraints(self):
        result = router.plan(BACKENDS, "browser", "act")
        self.assertEqual(result["selected"], "browser")
        self.assertTrue(result["candidates"][0]["prerequisite"]["headless"])

    def test_never_relaxes_focus_or_physical_input_constraints(self):
        self.assertIsNone(router.plan(BACKENDS, "desktop", "act")["selected"])
        result = router.plan(BACKENDS, "desktop", "act", avoid_focus=False)
        self.assertEqual([c["backend"] for c in result["candidates"]], ["uia"])
        result = router.plan(BACKENDS, "desktop", "act", avoid_focus=False, avoid_physical_input=False)
        self.assertEqual([c["backend"] for c in result["candidates"]], ["uia", "visual"])

    def test_win32_desktop_requires_an_explicit_target_and_approved_application(self):
        result = router.plan(BACKENDS, "win32_desktop", "act")
        candidate, = result["candidates"]
        self.assertEqual(candidate["tool"], "desktop_action")
        self.assertTrue(candidate["experimental"])
        self.assertTrue(candidate["prerequisite"]["requires_approved_app"])
        read_only = {**BACKENDS, "win32_desktop": {"available": True, "input_enabled": False}}
        self.assertIsNone(router.plan(read_only, "win32_desktop", "act")["selected"])
        self.assertEqual(router.plan(read_only, "win32_desktop", "observe")["selected"], "win32_desktop")

    def test_history_changes_order_only_when_allowed_candidates_have_evidence(self):
        candidates = router.plan(BACKENDS, "desktop", "observe")["candidates"]
        records = [
            {"ts": 7, "tool": candidate["tool"], "duration_ms": 10,
             "outcome": "ok" if candidate["backend"] == "visual" else "error"}
            for candidate in candidates for _ in range(5)
        ]
        self.assertEqual(router.rank_by_history(candidates, records, 5)["selected"], "visual")
        self.assertEqual(router.rank_by_history(candidates, records, 8)["selected"], "uia")
        self.assertEqual(router.rank_by_history(candidates[:1], records, 5)["selected"], "uia")
        self.assertIsNone(router.rank_by_history([], records, 5)["selected"])

    def test_diagnostics_excludes_arbitrary_result_data_and_error_details(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            log = diagnostics.Diagnostics(path)
            log.tool_finished("correlation", "owner", "browser_action", time.monotonic(),
                              error=ValueError("SENSITIVE-ERROR"),
                              result={"value": "SENSITIVE-VALUE", "error_code": "browser_failed"})
            logs = path.read_text(encoding="utf-8")
            self.assertIn("browser_failed", logs)
            self.assertNotIn("SENSITIVE", logs)
