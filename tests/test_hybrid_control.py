"""Checks the window and process contracts without touching user windows."""
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from PIL import Image, ImageDraw

SERVER = Path(__file__).resolve().parent.parent / "server"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V = load("visual_control")
G = load("visual_grounding")
P = load("process_jobs")


class Desktop:
    def __init__(self):
        self.active = 12
        self.covered = False
        self.offset = -130
        self.dpi = 144
        self.pixel = "blue"
        self.events = []
        self.position = (50, 50)
        self.held = set()
        self.sent = threading.Event()

    def select(self, query):
        return {"hwnd": 12, "pid": 42}

    def identity(self, hwnd):
        return {"hwnd": hwnd, "pid": 42, "created": 123.0} if hwnd else None

    def geometry(self, hwnd):
        rect = (self.offset, -50, self.offset+200, 50)
        return {"window_rect": rect, "frame_rect": rect, "client_rect": rect,
                "desktop_rect": (-500, -200, 500, 500), "dpi": self.dpi}

    def foreground(self):
        return self.active

    def focus(self, hwnd):
        return {"activation_requested": False, "acquired": self.active == hwnd}

    def modal(self, hwnd):
        return False

    def visible_sample(self, hwnd, rect):
        return "occluded" if self.covered else "sampled_clear"

    def owner_at(self, x, y):
        return 99 if self.covered else 12

    def grab(self, hwnd, geometry, rect, method, client):
        return Image.new("RGB", (200, 100), self.pixel), "screen"

    def key_code(self, name):
        return {"w": 87, "ctrl": 17}.get(name)

    def modifiers(self):
        return []

    def pressed(self, kind, value):
        return (kind, value) in self.held

    def send(self, kind, value, up):
        self.events.append((kind, value, up))
        self.sent.set()
        if up:
            self.held.remove((kind, value))
        else:
            self.held.add((kind, value))

    def move(self, position):
        self.position = position

    def cursor(self):
        return self.position


class VisualControl(unittest.TestCase):
    def setUp(self):
        self.desktop = Desktop()
        self.controller = V.Controller(self.desktop, lambda write: None, lambda w: False)
        self.ref = self.controller.open_window("hwnd:12")
        self.frame = self.controller.observe(self.ref, strict=True, max_side=128, region=[10, 5, 80, 60], zoom=2)
        self.capture_id = self.frame["public"]["capture_id"]

    def run_steps(self, steps, name="operation_01"):
        return self.controller.run(self.ref, self.capture_id, steps, name, "capture", 60000, 3000)

    def test_negative_coordinates_dpi_and_separate_observation(self):
        other_ref = self.controller.open_window("hwnd:12")
        other = self.controller.observe(other_ref, region=[1, 2, 110, 50])
        self.assertNotEqual(self.capture_id, other["public"]["capture_id"])
        self.assertEqual(self.frame["public"]["transform"]["dpi_scale"], 1.5)
        self.assertEqual(self.controller.point(self.frame, 0, 0, "capture"), (-120, -45))
        with self.assertRaisesRegex(V.VisualError, "different window"):
            self.controller.frame(other_ref, self.capture_id)
        response, _ = self.run_steps([{"op": "click", "x": 12, "y": 25}])
        self.assertTrue(response["ok"])
        self.assertEqual(self.desktop.position, (-113, -30))
        self.assertEqual(len(self.desktop.events), 2)

    def test_moved_resized_dpi_and_stale(self):
        for changed in ("offset", "dpi"):
            original = getattr(self.desktop, changed)
            setattr(self.desktop, changed, original+1)
            response, _ = self.run_steps([{"op": "key", "key": "w"}], "operation_"+changed)
            self.assertEqual(response["error_code"], "window_changed")
            self.assertFalse(response["input_sent"])
            setattr(self.desktop, changed, original)
        self.frame["created"] -= 200
        with self.assertRaisesRegex(V.VisualError, "older"):
            self.run_steps([{"op": "key", "key": "w"}])

    def test_strict_capture_refuses_foreground_failure_and_occlusion(self):
        self.desktop.active = 99
        with self.assertRaisesRegex(V.VisualError, "foreground"):
            self.controller.observe(self.ref, strict=True, front=True)
        self.desktop.active = 12
        self.desktop.covered = True
        with self.assertRaisesRegex(V.VisualError, "sampled client area"):
            self.controller.observe(self.ref, strict=True)
        diagnostic = self.controller.observe(self.ref)
        self.assertEqual(diagnostic["public"]["target_visibility"], "occluded")

    def test_focus_loss_and_cancel_release_input_and_deduplicate(self):
        for cancel in (False, True):
            self.desktop.events.clear()
            self.desktop.sent.clear()
            self.desktop.active = 12
            name = f"operation_{cancel}"
            out = []
            thread = threading.Thread(target=lambda: out.append(self.run_steps(
                [{"op":"key", "key":"w", "hold_ms":1000}], name)[0]))
            thread.start()
            self.assertTrue(self.desktop.sent.wait(1))
            if cancel:
                self.controller.operation(name, cancel=True)
            else:
                self.desktop.active = 99
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(out[0]["error_code"], "cancelled" if cancel else "focus_lost")
            self.assertTrue(out[0]["input_sent"])
            self.assertEqual(out[0]["outcome"], "unknown")
            self.assertEqual(self.desktop.events, [("key",87,False), ("key",87,True)])
            repeated, _ = self.run_steps([{"op":"key", "key":"w"}], name)
            self.assertEqual(repeated, out[0])
            self.assertEqual(len(self.desktop.events), 2)

    def test_change_verification_and_unverified_input(self):
        outcome, _ = self.run_steps([{"op":"key", "key":"w"}, {"op":"verify", "condition":"changed"}])
        self.assertTrue(outcome["input_sent"])
        self.assertFalse(outcome["postcondition_verified"])
        self.desktop.pixel = "red"
        status, _ = self.controller.verify(self.frame, [2, 2, 10, 10])
        self.assertTrue(status["verified"])
        self.assertEqual(status["evidence"], "pixel_comparison")

    def test_change_bounds_include_region_offset_and_empty_comparison(self):
        same, _ = self.controller.verify(self.frame, [2, 3, 10, 12], "unchanged")
        self.assertTrue(same["verified"])
        self.assertIsNone(same["changed_bounds"])
        self.desktop.pixel = "red"
        changed, _ = self.controller.verify(self.frame, [2, 3, 10, 12], threshold=128)
        self.assertEqual(changed["changed_bounds"], [2, 3, 10, 12])
        self.assertEqual(changed["changed_fraction"], 1.0)

    def test_dirty_regions_group_adjacent_tiles_and_bound_fragmented_output(self):
        mask = Image.new("L", (240, 160))
        draw = ImageDraw.Draw(mask)
        draw.rectangle((60, 8, 68, 9), fill=255)
        draw.rectangle((194, 10, 202, 15), fill=255)
        expected = [(60, 8, 69, 10), (194, 10, 203, 16)]
        self.assertEqual(V.changed_components(mask, mask.getbbox()), (expected, False))
        self.assertEqual(V.changed_components(mask, mask.getbbox(), limit=1), ([(60, 8, 203, 16)], True))
        self.desktop.pixel = "red"
        diff, _ = self.controller.verify(self.frame, [2, 3, 10, 12], threshold=128, detailed=True)
        self.assertEqual(diff["changed_regions"], [[2, 3, 10, 12]])
        self.assertFalse(diff["regions_coalesced"])

    def test_clients_cannot_access_each_others_refs_or_cancel_visual_operations(self):
        owner = ["first"]
        history = []
        scoped = V.Controller(self.desktop, lambda write: None, lambda window: False,
                              principal=lambda: owner[0], persist=history.extend)
        ref = scoped.open_window("hwnd:12")
        frame = scoped.observe(ref)["public"]["capture_id"]
        result, _ = scoped.run(ref, frame, [{"op": "wait", "ms": 0}], "same_id_01", "capture", 60000, 3000)
        self.assertTrue(result["ok"])
        owner[0] = "second"
        with self.assertRaisesRegex(V.VisualError, "Window reference"):
            scoped.observe(ref)
        with self.assertRaisesRegex(V.VisualError, "Capture is missing"):
            scoped.frame(ref, frame)
        with self.assertRaisesRegex(V.VisualError, "Unknown"):
            scoped.operation("same_id_01", cancel=True)
        other_ref = scoped.open_window("hwnd:12")
        other_frame = scoped.observe(other_ref)["public"]["capture_id"]
        other, _ = scoped.run(other_ref, other_frame, [{"op": "wait", "ms": 0}], "same_id_01", "capture", 60000, 3000)
        self.assertEqual(other["window_ref"], other_ref)
        restored = V.Controller(self.desktop, lambda write: None, lambda window: False,
                                principal=lambda: owner[0], history=history)
        self.assertEqual(restored.operation("same_id_01")["window_ref"], other_ref)
        owner[0] = "first"
        self.assertEqual(restored.operation("same_id_01")["window_ref"], ref)

    def test_parse_all_steps_before_any_input_and_require_confirmation(self):
        with self.assertRaises(V.VisualError):
            self.run_steps([{"op":"key", "key":"w"}, {"op":"drag", "x":5}])
        denied, _ = self.run_steps([{"op":"key", "key":"w", "consequential":True}])
        self.assertEqual(denied["error_code"], "confirmation_denied")
        self.assertEqual(self.desktop.events, [])

    def test_running_operation_survives_restart_as_unknown(self):
        stored = [{"operation_id":"known_123", "status":"running", "input_sent":False, "retry_action":False}]
        restored = V.Controller(self.desktop, lambda write: None, lambda window: False, history=stored)
        state, _ = restored.run("new_ref", "new_frame", [{"op":"key", "key":"w"}], "known_123", "capture", 60000, 3000)
        self.assertEqual(state["outcome"], "unknown")
        self.assertIsNone(state["input_sent"])
        self.assertEqual(self.desktop.events, [])

    def test_outline_without_models_and_adapter_errors(self):
        im = Image.new("RGB", (300, 150), "black")
        ImageDraw.Draw(im).rectangle((30,30,123,63), outline="white", width=3)
        buffer = io.BytesIO()
        im.save(buffer, "JPEG")
        frame = {"jpeg": buffer.getvalue(), "public": {"size": im.size}}
        found, diagnostics = G.grounding(frame, "", True, 10, lambda _: {"lines":[]}, None, None,
                                         lambda *_: (_ for _ in ()).throw(ValueError("offline")))
        self.assertTrue(any(c["source"] == "contour" for c in found))
        self.assertTrue(all(not c["actionable"] for c in found))
        self.assertIn("adapter_error", diagnostics)


class StructuredProcess(unittest.TestCase):
    def test_argv_environment_and_artifact_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            actual, argv = P.process_command(sys.executable, ['q" ⛄', "& $env:A", ""], tmp, Path, "none")
            self.assertEqual(argv, [actual, 'q" ⛄', "& $env:A", ""])
            env = P.process_environment({"PCBRIDGE_PROFILE_TEST": "1"}, {"PCBRIDGE_DELTA_TEST": "value"})
            self.assertEqual(env["PCBRIDGE_PROFILE_TEST"], "1")
            self.assertNotIn("PCBRIDGE_DELTA_TEST", os.environ)
            source = path / "input.exe"
            source.write_bytes(b"abc")
            data = P.artifacts_snapshot({"artifact_max_bytes": 1024, "artifacts": [{"source": str(source),
                "name": source.name}]}, {"job_id": "demo"}, path / "new")
            self.assertEqual(data[0]["sha256"], "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
            self.assertEqual(Path(data[0]["path"]).read_bytes(), b"abc")


if __name__ == "__main__":
    unittest.main()
