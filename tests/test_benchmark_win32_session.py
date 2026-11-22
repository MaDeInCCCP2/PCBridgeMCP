from contextlib import contextmanager
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "tools"))
sys.path.insert(0, str(APP / "server"))
import benchmark_win32_session as benchmark


@contextmanager
def fixture():
    yield object(), {"session_id": "candidate", "pid": 42}


class Win32SessionBenchmark(unittest.TestCase):
    def check_lock(self, events_during_action, expected):
        events = []

        @contextmanager
        def notifications():
            yield events, lambda: None

        def wait(buffer, *_):
            if not buffer:
                buffer.append(benchmark.SESSION_LOCK)
                return 0
            if len(buffer) == 1:
                buffer.append(benchmark.SESSION_UNLOCK)
            return 1

        def invoke(_sessions, _sid, args):
            if args == "while-locked":
                events.extend(events_during_action)
            return True

        with (patch.object(benchmark, "session_notifications", notifications),
              patch.object(benchmark, "fixture", fixture),
              patch.object(benchmark, "ready", return_value="window"),
              patch.object(benchmark, "prepare", side_effect=lambda _, __, ___, name: name),
              patch.object(benchmark, "invoke", side_effect=invoke),
              patch.object(benchmark, "wait_for_event", side_effect=wait),
              patch("builtins.print")):
            report = benchmark.lock_transition(2)
        self.assertTrue(report["lock_observed"])
        self.assertTrue(report["unlock_observed"])
        self.assertTrue(report["after_unlock"])
        self.assertEqual(report["outcome"], expected)
        return report["action_while_locked"]

    def test_success_only_if_action_completed_before_unlock(self):
        self.assertTrue(self.check_lock([], "passed"))
        self.assertIsNone(self.check_lock([benchmark.SESSION_UNLOCK], "inconclusive"))

    def check_active(self, ticks, expected):
        probe = SimpleNamespace(result=lambda pids: {"outcome": "passed"})

        @contextmanager
        def start_probe():
            yield probe

        clock = SimpleNamespace(monotonic=iter([100, 101, 104]).__next__, sleep=lambda _: None)
        win32 = SimpleNamespace(GetLastInputInfo=iter(ticks).__next__)
        with (patch.dict(sys.modules, {"win32api": win32}),
              patch.object(benchmark, "fixture", fixture),
              patch.object(benchmark, "ready", return_value="window"),
              patch.object(benchmark, "prepare"),
              patch.object(benchmark, "invoke", return_value=True),
              patch.object(benchmark, "time", clock),
              patch("builtins.print")):
            result = benchmark.active_input(2, start_probe)
        self.assertEqual(result["outcome"], expected)
        self.assertTrue(result["all_actions_verified"])
        return result["actions_with_input_activity"]

    def test_no_input_is_inconclusive_even_when_the_fixture_succeeds(self):
        self.assertEqual(self.check_active([42, 42], "inconclusive"), 0)
        self.assertEqual(self.check_active([42, 45], "passed"), 1)
