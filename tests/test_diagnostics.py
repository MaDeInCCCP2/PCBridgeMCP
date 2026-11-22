import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from diagnostics import DIAGNOSTIC_LIMIT, recent_diagnostics


class DiagnosticsReader(unittest.TestCase):
    def test_rotated_log_partial_record_and_bad_fields(self):
        template = {"event": "tool_finished", "tool": "file_info", "ts": 1780000000,
                    "duration_ms": 7.5, "outcome": "ok", "correlation_id": "c" * 32}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "diagnostics.jsonl"
            path.with_suffix(".old.jsonl").write_text(json.dumps(template) + "\n", encoding="utf-8")
            newer = {**template, "correlation_id": "d" * 32, "ts": template["ts"] + 5,
                     "error_code": "n/a: private text", "duration_ms": 12}
            invalid = {**template, "tool": "a path or arbitrary text", "correlation_id": "a" * 32}
            path.write_text("\n".join((json.dumps(template), json.dumps(newer), json.dumps(invalid),
                                        "bad JSON", json.dumps({**template, "correlation_id": "b" * 32}))),
                            encoding="utf-8")
            result = recent_diagnostics(path)
            self.assertEqual([event["correlation_id"] for event in result], ["d" * 32, "c" * 32])
            self.assertIsNone(result[0]["error_code"])

    def test_history_reports_only_requested_client_aggregates(self):
        template = {"event": "tool_finished", "tool": "list_files", "ts": 1780000000,
                    "duration_ms": 7.5, "outcome": "ok"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "diagnostics.jsonl"
            events = [{**template, "client_id": owner, "correlation_id": f"{i:032x}"}
                      for i, owner in enumerate(("client-a", "client-b"))]
            path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
            result = recent_diagnostics(path, principal="client-b")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["correlation_id"], f"{1:032x}")
        self.assertNotIn("client_id", result[0])

    def test_event_count_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "diagnostics.jsonl"
            with path.open("w", encoding="utf-8") as log:
                for i in range(DIAGNOSTIC_LIMIT + 5):
                    log.write(json.dumps({"event": "tool_finished", "tool": "list_files", "ts": 1780000000+i,
                                          "duration_ms": 7.5, "outcome": "ok", "correlation_id": f"{i:032x}"}) + "\n")
            result = recent_diagnostics(path)
            self.assertEqual(len(result), DIAGNOSTIC_LIMIT)
            self.assertEqual(result[-1]["ts"], 1780000005)
