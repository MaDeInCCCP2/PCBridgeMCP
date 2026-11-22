"""Frame-bound Windows input. The exposed ids are opaque, explicit capabilities."""
import base64
from collections import OrderedDict
import ctypes
from ctypes import wintypes as wt
import hashlib
import io
import json
import re
import threading
import time
import uuid

from PIL import Image, ImageChops
from mcp.types import CallToolResult, ImageContent, TextContent


class VisualError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def fail(code, message):
    raise VisualError(code, message)


def result(data, frame=None):
    content = [TextContent(type="text", text=json.dumps(data, ensure_ascii=False))]
    if frame:
        content.append(ImageContent(type="image", mime_type="image/jpeg",
                                    data=base64.b64encode(frame["jpeg"]).decode("ascii")))
    return CallToolResult(content=content, structured_content=data, is_error=not data.get("ok", True))


def inside(rect, x, y):
    return rect[0] <= x < rect[2] and rect[1] <= y < rect[3]


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        fail("invalid_argument", f"{name} must be an integer in {low}..{high}")
    return value


def changed_components(mask, bounds, tile_size=64, limit=24):
    """Cluster neighboring tiles and bound changed pixels inside each cluster."""
    left, top, right, bottom = bounds
    remaining = {}
    for row in range(top // tile_size, (bottom - 1) // tile_size + 1):
        for col in range(left // tile_size, (right - 1) // tile_size + 1):
            x, y = col * tile_size, row * tile_size
            hit = mask.crop((x, y, min(x + tile_size, right), min(y + tile_size, bottom))).getbbox()
            if hit:
                remaining[col, row] = (hit[0] + x, hit[1] + y, hit[2] + x, hit[3] + y)
    regions = []
    while remaining:
        todo = [next(iter(remaining))]
        l, t, r, b = remaining.pop(todo[0])
        while todo:
            x, y = todo.pop()
            for cell in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                hit = remaining.pop(cell, None)
                if hit:
                    l, t, r, b = min(l, hit[0]), min(t, hit[1]), max(r, hit[2]), max(b, hit[3])
                    todo.append(cell)
        regions.append((l, t, r, b))
        if len(regions) == limit and remaining:
            return [bounds], True
    return regions, False


def offset_box(box, region):
    x, y = region[:2] if region else (0, 0)
    return [box[0] + x, box[1] + y, box[2] + x, box[3] + y]


class WindowsDesktop:
    BUTTONS = {"left": (0x02, 0x04, 1), "right": (0x08, 0x10, 2), "middle": (0x20, 0x40, 4)}

    def __init__(self, bridge):
        self.b = bridge
        self.u = bridge["_u32"]
        self.input = bridge.get("_ui32")
        h, n, d = wt.HWND, ctypes.c_int, wt.DWORD
        for name, restype, args in (
            ("IsWindow", wt.BOOL, [h]), ("IsWindowEnabled", wt.BOOL, [h]),
            ("GetForegroundWindow", h, []), ("GetAncestor", h, [h, wt.UINT]),
            ("WindowFromPoint", h, [wt.POINT]), ("GetDpiForWindow", wt.UINT, [h]),
            ("GetAsyncKeyState", ctypes.c_short, [n]),
            ("SetCursorPos", wt.BOOL, [n, n]), ("GetCursorPos", wt.BOOL, [ctypes.POINTER(wt.POINT)]),
            ("MonitorFromPoint", wt.HMONITOR, [wt.POINT, d]),
        ):
            fn = getattr(self.u, name)
            fn.restype, fn.argtypes = restype, args

    def select(self, query):
        query = query.strip().casefold()
        windows = self.b["_windows"]()
        if query.startswith("hwnd:"):
            hwnd = int(query[5:], 0)
            hits = [w for w in windows if w["hwnd"] == hwnd]
        elif not query:
            hits = [w for w in windows if w["hwnd"] == self.foreground()]
        else:
            exact = [w for w in windows if query in {w["title"].casefold(), w["exe"].casefold(),
                                                       w["exe"].casefold().removesuffix(".exe")}]
            hits = exact or [w for w in windows if query in (w["title"] + " " + w["exe"]).casefold()]
        if len(hits) != 1:
            fail("ambiguous_window" if hits else "window_not_found", "Select one window using hwnd:<id> from list_windows.")
        return hits[0]

    def identity(self, hwnd):
        import psutil
        if not hwnd or not self.u.IsWindow(hwnd):
            return None
        pid = wt.DWORD()
        thread = self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            created = psutil.Process(pid.value).create_time()
        except (psutil.Error, OSError):
            return None
        return {"hwnd": hwnd, "pid": pid.value, "created": created, "thread_id": thread} if thread else None

    def geometry(self, hwnd):
        if not self.u.IsWindowVisible(hwnd) or self.u.IsIconic(hwnd) or self.b["_cloaked"](hwnd):
            fail("window_hidden", "The target is hidden, cloaked or minimized.")
        return {"window_rect": self.b["_window_rect"](hwnd), "client_rect": self.b["_client_rect"](hwnd),
                "frame_rect": self.b["_frame_rect"](hwnd), "dpi": int(self.u.GetDpiForWindow(hwnd)),
                "desktop_rect": self.desktop_rect()}

    def desktop_rect(self):
        x, y = self.b["_virtual_origin"]()
        return x, y, x + self.u.GetSystemMetrics(78), y + self.u.GetSystemMetrics(79)

    def foreground(self):
        return int(self.u.GetForegroundWindow() or 0)

    def focus(self, hwnd):
        if self.u.IsIconic(hwnd):
            self.u.ShowWindow(hwnd, 9)
        requested = bool(self.u.SetForegroundWindow(hwnd))
        deadline = time.monotonic() + .4
        while self.foreground() != hwnd and time.monotonic() < deadline:
            time.sleep(.02)
        return {"activation_requested": requested, "acquired": self.foreground() == hwnd}

    def owner_at(self, x, y):
        point = wt.POINT(x, y)
        if not self.u.MonitorFromPoint(point, 0):
            return 0
        hwnd = self.u.WindowFromPoint(point)
        return int(self.u.GetAncestor(hwnd, 2) or hwnd or 0)

    def modal(self, hwnd):
        return not self.u.IsWindowEnabled(hwnd)

    def visible_sample(self, hwnd, rect):
        l, t, r, b = rect
        points = {(l + (r-l-1)*i//4, t + (b-t-1)*j//4) for i in range(5) for j in range(5)}
        return "sampled_clear" if all(self.owner_at(x, y) == hwnd for x, y in points) else "occluded"

    def grab(self, hwnd, geometry, rect, method, client):
        if method in {"auto", "dxgi", "window"} and (method != "auto" or self.b["flag"]("enable_dxgi_capture")):
            backend = "window" if method == "window" else "dxgi"
            try:
                image = self.b["_capture_window_backend"](hwnd, client, backend, geometry)
                return image, "printwindow" if backend == "window" else "dxgi_desktop"
            except (OSError, ValueError):
                if method != "auto":
                    raise
        return self.b["ImageGrab"].grab(bbox=rect, all_screens=True), "screen"

    def cursor(self):
        pos = wt.POINT()
        if not self.u.GetCursorPos(ctypes.byref(pos)):
            fail("input_failed", "Cannot read cursor position.")
        return pos.x, pos.y

    def failsafe(self):
        mouse = self.b["pyautogui"]
        return mouse.FAILSAFE and self.cursor() in mouse.FAILSAFE_POINTS

    def move(self, point):
        if not self.u.SetCursorPos(*point):
            fail("input_failed", "Cannot move cursor.")
        if self.cursor() != point:
            fail("cursor_moved", "Cursor is outside the requested point.")

    def key_code(self, name):
        return self.b["_vk_of"](name)

    def pressed(self, kind, value):
        key = value if kind == "key" else self.BUTTONS[value][2]
        return bool(self.u.GetAsyncKeyState(key) & 0x8000)

    def modifiers(self):
        return [vk for vk in (0x10, 0x11, 0x12, 0x5b, 0x5c) if self.pressed("key", vk)]

    def send(self, kind, value, up=False):
        b = self.b
        event = b["_INPUT"]()
        if kind in {"key", "unicode"}:
            vk, scan, flags = b["_vk_event"](value, up) if kind == "key" else (0, value, 4 | (2 if up else 0))
            event.type = 1
            event.u.ki = b["_KEYBDINPUT"](vk, scan, flags, 0, 0)
        else:
            flags = (self.BUTTONS[value][int(up)] if kind == "mouse" else 0x0800 if kind == "scroll" else 0x1000)
            data = int(value * 120) & 0xffffffff if kind in {"scroll", "hscroll"} else 0
            event.u.mi = b["_MOUSEINPUT"](0, 0, data, flags, 0, 0)
        if self.input.SendInput(1, ctypes.byref(event), ctypes.sizeof(event)) != 1:
            fail("input_rejected", "Windows rejected the input event.")

    def focus_events(self, duration, policy):
        events = []
        callback_type = ctypes.WINFUNCTYPE(None, wt.HANDLE, wt.DWORD, wt.HWND, wt.LONG,
                                         wt.LONG, wt.DWORD, wt.DWORD)
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD, wt.HMODULE, callback_type, wt.DWORD, wt.DWORD, wt.DWORD]
        u.SetWinEventHook.restype = wt.HANDLE
        u.UnhookWinEvent.argtypes = [wt.HANDLE]
        u.PeekMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
        u.DispatchMessageW.restype = wt.LPARAM
        started = time.monotonic()

        @callback_type
        def callback(hook, event, hwnd, obj, child, thread, timestamp):
            if len(events) < 200:
                events.append({"elapsed_ms": round((time.monotonic()-started)*1000),
                               "foreground": self.identity(int(hwnd or 0)), "event_tick_ms": timestamp})

        hook = u.SetWinEventHook(3, 3, None, callback, 0, 0, 0)
        if not hook:
            fail("focus_hook_failed", "Cannot subscribe to Windows foreground events.")
        try:
            initial = self.identity(self.foreground())
            message = wt.MSG()
            while time.monotonic()-started < duration:
                policy()
                for _ in range(100):
                    if not u.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                        break
                    u.DispatchMessageW(ctypes.byref(message))
                time.sleep(.015)
            return {"initial": initial, "events": events, "final": self.identity(self.foreground()),
                    "truncated": len(events) == 200}
        finally:
            u.UnhookWinEvent(hook)


class Controller:
    def __init__(self, desktop, policy, approve, *, history=None, persist=None, principal=lambda: "owner"):
        self.desktop, self.policy, self.approve = desktop, policy, approve
        self.persist = persist
        self.principal = principal
        self.lock = threading.RLock()
        self.input_lock = threading.Lock()
        self.windows = OrderedDict()
        self.frames = OrderedDict()
        self.operations = OrderedDict()
        for previous in (history or [])[-128:]:
            if not isinstance(previous, dict) or not isinstance(previous.get("operation_id"), str):
                continue
            if previous.get("status") == "running":
                previous = previous | {"ok": False, "status": "interrupted", "outcome": "unknown",
                                       "input_sent": None, "error_code": "outcome_unknown",
                                       "error": "Bridge restarted during the operation. Observe before continuing."}
            previous = dict(previous)
            owner = previous.pop("owner", "owner")
            if isinstance(owner, str):
                self.operations[(owner, previous["operation_id"])] = previous
        self._active = None

    @property
    def active(self):
        with self.lock:
            if self._active and self._active[0] == self.principal():
                return self._active[1]
            return None

    def checkpoint(self):
        if self.persist:
            self.persist([{**{k: v for k, v in entry.items() if k != "stop"}, "owner": owner}
                          for (owner, _), entry in self.operations.items()])

    def window(self, window_ref):
        with self.lock:
            target = self.windows.get((self.principal(), window_ref))
        if not target or self.desktop.identity(target["hwnd"]) != target:
            fail("window_changed", "Window reference has expired or its identity changed.")
        return target

    def open_window(self, query):
        selected = self.desktop.select(query)
        target = self.desktop.identity(selected["hwnd"])
        if not target or target["pid"] != selected["pid"]:
            fail("window_changed", "Target identity changed during window selection.")
        ref = uuid.uuid4().hex
        with self.lock:
            self.windows[(self.principal(), ref)] = target
            while len(self.windows) > 128:
                self.windows.popitem(last=False)
        return ref

    def frame(self, window_ref, capture_id, max_age=120000):
        with self.lock:
            frame = self.frames.get(capture_id)
        if not frame or frame["owner"] != self.principal() or frame["public"]["window_ref"] != window_ref:
            fail("capture_stale", "Capture is missing, expired or belongs to a different window reference.")
        if (time.monotonic()-frame["created"])*1000 > max_age:
            fail("capture_stale", "Capture is older than max_capture_age_ms.")
        return frame

    def observe(self, ref, method="auto", client=True, strict=False, front=False, max_side=1600,
                region=None, zoom=1):
        self.policy(front)
        if method not in {"auto", "screen", "window", "dxgi"}:
            fail("invalid_argument", "method must be auto, screen, window or dxgi.")
        integer(max_side, 128, 2400, "max_side")
        integer(zoom, 1, 4, "zoom")
        d = self.desktop
        target = self.window(ref)
        hwnd = target["hwnd"]
        focus = None
        if front:
            focus = d.focus(hwnd)
        started, timestamp = time.monotonic(), time.time()
        before = d.foreground()
        geometry = d.geometry(hwnd)
        rect = geometry["client_rect" if client else "frame_rect"]
        if region is not None:
            if len(region) != 4:
                fail("invalid_argument", "region must contain [x,y,width,height] in source pixels.")
            x, y, w, h = region
            if any(type(v) is not int for v in region) or min(x, y) < 0 or min(w, h) < 1 or                     x+w > rect[2]-rect[0] or y+h > rect[3]-rect[1]:
                fail("invalid_argument", "region falls outside the selected capture area.")
            box = (x, y, x+w, y+h)
        else:
            box = (0, 0, rect[2]-rect[0], rect[3]-rect[1])
        if min(box[2]-box[0], box[3]-box[1]) < 1 or (rect[2]-rect[0])*(rect[3]-rect[1]) > 24_000_000:
            fail("capture_bounds", "Capture area is empty or too large.")
        screen_rect = (rect[0]+box[0], rect[1]+box[1], rect[0]+box[2], rect[1]+box[3])
        visible_before = d.visible_sample(hwnd, screen_rect)
        if strict and (before != hwnd or visible_before != "sampled_clear" or d.modal(hwnd)):
            fail("focus_not_acquired" if before != hwnd else "target_occluded",
                 "Strict capture needs foreground focus and a clear sampled client area.")
        image, backend = d.grab(hwnd, geometry, rect, method, client)
        after = d.foreground()
        if self.window(ref) != target or d.geometry(hwnd) != geometry:
            fail("window_changed", "Window geometry changed during capture.")
        if image.size != (rect[2]-rect[0], rect[3]-rect[1]):
            fail("capture_geometry", "Backend pixels do not match the recorded window bounds.")
        self.policy(False)
        visible_after = d.visible_sample(hwnd, screen_rect)
        visibility = "sampled_clear" if visible_before == visible_after == "sampled_clear" else "occluded"
        if strict and (after != hwnd or visibility != "sampled_clear" or d.modal(hwnd)):
            fail("capture_changed", "Focus or sampled visibility changed during capture.")
        image = image.crop(box).convert("RGB")
        source_size = image.size
        scale = min(zoom, max_side / max(source_size))
        shape = tuple(max(1, round(dim*scale)) for dim in source_size)
        if shape != source_size:
            image = image.resize(shape, Image.Resampling.LANCZOS)
        stream = io.BytesIO()
        image.save(stream, "JPEG", quality=88)
        jpeg = stream.getvalue()
        capture_id = uuid.uuid4().hex
        public = {"capture_id": capture_id, "window_ref": ref, "requested_window": target,
                  "capture_method": backend, "requested_method": method, "target_visibility": visibility,
                  "foreground_before": d.identity(before), "foreground_after": d.identity(after),
                  "bring_to_front": focus, "geometry": geometry, "screen_rect": screen_rect,
                  "size": shape, "source_size": source_size, "timestamp": timestamp,
                  "capture_ms": round((time.monotonic()-started)*1000, 1),
                  "transform": {"origin": screen_rect[:2], "scale_x": source_size[0]/shape[0],
                                "scale_y": source_size[1]/shape[1], "dpi_scale": geometry["dpi"]/96},
                  "size_bytes": len(jpeg), "sha256": hashlib.sha256(jpeg).hexdigest()}
        frame = {"public": public, "jpeg": jpeg, "created": started, "owner": self.principal(), "client": client, "max_side": max_side,
                 "region": region, "zoom": zoom, "strict": strict, "method": method}
        with self.lock:
            self.frames[capture_id] = frame
            while len(self.frames) > 32:
                self.frames.popitem(last=False)
        return frame

    def point(self, frame, x, y, space):
        p = frame["public"]
        l, t, _, _ = p["screen_rect"]
        if space == "capture":
            if not inside((0, 0, *p["size"]), x, y):
                fail("point_outside_capture", "Point is outside the displayed capture.")
            sx, sy = p["transform"]["scale_x"], p["transform"]["scale_y"]
            x = l + min(p["source_size"][0]-1, int((x+.5)*sx))
            y = t + min(p["source_size"][1]-1, int((y+.5)*sy))
        elif space == "client":
            cx, cy, _, _ = p["geometry"]["client_rect"]
            x, y = x+cx, y+cy
        if not inside(p["screen_rect"], x, y):
            fail("point_outside_capture", "Point must be inside the photographed region.")
        return x, y

    def _guard(self, frame, target, max_age, stop, deadline, point=None, cursor=None):
        self.policy(True)
        if stop.is_set():
            fail("cancelled", "Operation cancelled.")
        if time.monotonic() > deadline:
            fail("operation_timeout", "Operation time limit reached.")
        if (time.monotonic()-frame["created"])*1000 > max_age:
            fail("capture_stale", "Capture expired during the operation.")
        d = self.desktop
        hwnd = target["hwnd"]
        if d.identity(hwnd) != target or d.geometry(hwnd) != frame["public"]["geometry"]:
            fail("window_changed", "Target identity, bounds, DPI, or desktop changed.")
        if d.modal(hwnd):
            fail("modal_blocked", "The target is disabled by a blocking window.")
        if d.foreground() != hwnd:
            fail("focus_lost", "Target window is no longer foreground.")
        if hasattr(d, "failsafe") and d.failsafe():
            fail("input_failsafe", "The pointer is at a PyAutoGUI fail-safe position.")
        if point is not None and d.owner_at(*point) != hwnd:
            fail("target_occluded", "The pointer location is outside the target window or covered.")
        if cursor is not None and d.cursor() != cursor:
            fail("cursor_moved", "Cursor moved away from the requested point.")

    def refresh(self, frame):
        return self.observe(frame["public"]["window_ref"], frame["method"], frame["client"], frame["strict"],
                            False, frame["max_side"], frame["region"], frame["zoom"])

    def verify(self, frame, region=None, condition="changed", threshold=1, *, detailed=False):
        integer(threshold, 1, 255, "threshold")
        if condition not in ("changed", "unchanged"):
            fail("invalid_argument", "condition must be changed or unchanged.")
        a = frame["public"]
        if region is not None and (len(region) != 4 or any(type(v) is not int for v in region) or
                                   not (0 <= region[0] < region[2] <= a["size"][0] and
                                        0 <= region[1] < region[3] <= a["size"][1])):
            fail("invalid_argument", "Verification region must be [left,top,right,bottom] inside capture pixels.")
        b = self.refresh(frame)
        bp = b["public"]
        compared = (a["geometry"] == bp["geometry"] and a["size"] == bp["size"] and
                    a["capture_method"] == bp["capture_method"] and
                    (a["capture_method"] not in {"screen", "dxgi_desktop"} or a["target_visibility"] == bp["target_visibility"] == "sampled_clear"))
        if not compared:
            return {"verified": False, "condition": condition, "evidence": "incomparable_capture"}, b
        with Image.open(io.BytesIO(frame["jpeg"])) as left, Image.open(io.BytesIO(b["jpeg"])) as right:
            if region:
                left, right = left.crop(region), right.crop(region)
            channels = ImageChops.difference(left, right).split()
            diff = ImageChops.lighter(ImageChops.lighter(channels[0], channels[1]), channels[2])
            hist = diff.histogram()
            changed_pixels = sum(hist[threshold:])
            mask = diff.point([0] * threshold + [255] * (256 - threshold)) if changed_pixels else None
            bounds = mask.getbbox() if mask is not None else None
            regions, coalesced = changed_components(mask, bounds) if detailed and bounds else ([], False)
        verification = {"verified": bool(changed_pixels) == (condition == "changed"), "condition": condition,
                        "changed_fraction": changed_pixels / sum(hist),
                        "changed_bounds": offset_box(bounds, region) if bounds else None,
                        "evidence": "pixel_comparison"}
        if detailed:
            verification["changed_regions"] = [offset_box(box, region) for box in regions]
            verification["regions_coalesced"] = coalesced
        return verification, b

    def validate_steps(self, steps):
        allowed = {
            "click": {"x", "y", "button", "hold_ms"},
            "double_click": {"x", "y", "button", "hold_ms"},
            "hover": {"x", "y"},
            "drag": {"x", "y", "to_x", "to_y", "button", "ms"},
            "scroll": {"x", "y", "amount", "axis"},
            "key": {"key", "hold_ms"},
            "type": {"text"},
            "wait": {"ms"},
            "capture": set(),
            "verify": {"region", "condition", "threshold", "ms"},
        }
        if not 1 <= len(steps) <= 20:
            fail("invalid_argument", "Provide 1..20 steps.")
        validated = []
        for step in steps:
            op = step.get("op")
            if not isinstance(op, str) or op not in allowed or set(step) - allowed[op] - {"op", "consequential"}:
                fail("invalid_argument", "Invalid step operation or fields.")
            s = dict(step)
            if "consequential" in s and type(s["consequential"]) is not bool:
                fail("invalid_argument", "consequential must be a boolean.")
            for field in allowed[op] & {"x", "y", "to_x", "to_y"}:
                integer(s.get(field), -100000, 100000, field)
            if op in {"click", "double_click", "drag"} and s.setdefault("button", "left") not in tuple(WindowsDesktop.BUTTONS):
                fail("invalid_argument", "Use left, right or middle mouse button.")
            if op in {"click", "double_click", "key"}:
                integer(s.setdefault("hold_ms", 0), 0, 3000, "hold_ms")
            if op == "key":
                raw = s.get("key")
                if not isinstance(raw, str):
                    fail("invalid_argument", "Key or shortcut is required.")
                s["keys"] = [self.desktop.key_code(k.strip().lower()) for k in raw.split("+")]
                if not 1 <= len(s["keys"]) <= 4 or None in s["keys"] or len(set(s["keys"])) != len(s["keys"]):
                    fail("invalid_argument", "Use 1..4 different known virtual keys.")
                if any(k in {0x5b, 0x5c, 0x5d} for k in s["keys"]) or (
                        any(k in {0x12, 0xa4, 0xa5} for k in s["keys"]) and
                        any(k in {0x09, 0x1b} for k in s["keys"])):
                    fail("invalid_argument", "Desktop-wide shortcuts are outside the window-bound route.")
            if op == "type":
                raw = s.get("text")
                if not isinstance(raw, str) or not 1 <= len(raw) <= 512 or "\x00" in raw:
                    fail("invalid_argument", "Provide up to 512 characters without NUL.")
                s["encoded"] = raw.replace("\r\n", "\n").encode("utf-16-le")
            if op == "scroll":
                if s.setdefault("axis", "vertical") not in ("vertical", "horizontal"):
                    fail("invalid_argument", "Scroll axis must be vertical or horizontal.")
                if integer(s.get("amount"), -20, 20, "amount") == 0:
                    fail("invalid_argument", "Scroll amount must be nonzero.")
            if op in {"wait", "drag", "verify"}:
                integer(s.setdefault("ms", 350 if op == "drag" else 0), 0, 5000, "ms")
            if op == "verify":
                if s.setdefault("condition", "changed") not in ("changed", "unchanged"):
                    fail("invalid_argument", "Verify condition must be changed or unchanged.")
                integer(s.setdefault("threshold", 1), 1, 255, "threshold")
                rect = s.get("region")
                if rect is not None and (not isinstance(rect, list) or len(rect) != 4 or
                                         any(type(v) is not int for v in rect)):
                    fail("invalid_argument", "Verification region must be [left,top,right,bottom].")
            validated.append(s)
        return validated

    def operation(self, operation_id, cancel=False):
        with self.lock:
            entry = self.operations.get((self.principal(), operation_id))
            if not entry:
                fail("operation_not_found", "Unknown or expired operation_id.")
            if cancel and entry["status"] == "running":
                entry["stop"].set()
            return {k: v for k, v in entry.items() if k != "stop"}

    def run(self, ref, capture_id, steps, operation_id, space, max_age, timeout):
        self.policy(True)
        if space not in {"capture", "client", "screen"}:
            fail("invalid_argument", "coordinate_space must be capture, client or screen.")
        integer(max_age, 100, 120000, "max_capture_age_ms")
        integer(timeout, 100, 30000, "timeout_ms")
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", operation_id):
            fail("invalid_argument", "operation_id must contain 8..80 letters, digits, underscores or hyphens.")
        operation_key = (self.principal(), operation_id)
        with self.lock:
            if operation_key in self.operations:
                return self.operation(operation_id), None
        steps = self.validate_steps(steps)
        frame = self.frame(ref, capture_id, max_age)
        target = self.window(ref)
        if frame["public"]["capture_method"] in {"screen", "dxgi_desktop"} and frame["public"]["target_visibility"] != "sampled_clear":
            fail("target_occluded", "Take a clear window observation before using screen pixels for input.")
        for s in steps:
            if "x" in s:
                s["point"] = self.point(frame, s["x"], s["y"], space)
                if s["op"] == "drag":
                    s["end"] = self.point(frame, s["to_x"], s["to_y"], space)
            if s["op"] == "verify" and s.get("region") is not None:
                x1,y1,x2,y2 = s["region"]
                width,height = frame["public"]["size"]
                if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
                    fail("invalid_argument", "Verification region is outside the displayed capture.")
        if not self.input_lock.acquire(blocking=False):
            fail("input_busy", "Another desktop input operation is active.")
        stop = threading.Event()
        record = {"operation_id": operation_id, "status": "running", "phase": "guard",
                  "window_ref": ref, "initial_capture_id": capture_id, "completed_steps": [],
                  "input_sent": False, "events_sent": 0, "postcondition_verified": None, "retry_action": False}
        with self.lock:
            if operation_key in self.operations:
                self.input_lock.release()
                return self.operation(operation_id), None
            self.operations[operation_key] = record | {"stop": stop}
            while len(self.operations) > 128:
                self.operations.popitem(last=False)
            self._active = operation_key
            try:
                self.checkpoint()
            except OSError:
                self.operations.pop(operation_key, None)
                self._active = None
                self.input_lock.release()
                fail("operation_history_unavailable", "Cannot commit deduplication state before input.")
        held = []
        released = []
        cleanup_failed = []
        d = self.desktop
        final_frame = None
        start = time.monotonic()
        deadline = start+timeout/1000

        def publish():
            with self.lock:
                self.operations[operation_key] = record | {"completed_steps": list(record["completed_steps"]), "stop": stop}

        def guard(point=None, cursor=None):
            self._guard(frame, target, max_age, stop, deadline, point, cursor)

        def pause(ms, point=None):
            until = time.monotonic()+ms/1000
            while True:
                guard(point, point)
                remain = until-time.monotonic()
                if remain <= 0:
                    return
                stop.wait(min(.015, remain))

        def send(kind, value, up=False):
            d.send(kind, value, up)
            record["events_sent"] += 1
            record["input_sent"] = True

        def down(kind, value, point=None):
            guard(point, point)
            if kind != "unicode" and d.pressed(kind, value):
                fail("input_already_held", "A requested key or button is already held.")
            send(kind, value)
            held.append((kind, value))

        def up(kind, value, point=None):
            guard(point, point)
            send(kind, value, True)
            held.remove((kind, value))

        def move(point):
            guard(point)
            d.move(point)
            record["input_sent"] = True
            guard(point)
            if d.cursor() != point:
                fail("cursor_moved", "Cursor changed before the input event.")

        last = None
        try:
            guard()
            if any(s.get("consequential") for s in steps):
                record["phase"] = "confirmation"
                publish()
                if not self.approve(target):
                    fail("confirmation_denied", "Local confirmation was not granted.")
                # Confirmation has its own deadline. Target and original capture still have to be current.
                deadline = time.monotonic()+timeout/1000
                guard()
            for i, s in enumerate(steps):
                op = s["op"]
                record.update(phase=op, current_step=i)
                publish()
                started = time.monotonic()
                point = s.get("point")
                guard(point)
                if op in {"click", "double_click", "hover", "drag", "scroll", "key", "type"}:
                    if d.modifiers():
                        fail("input_already_held", "A modifier is already held.")
                    record["postcondition_verified"] = None
                if op in {"click", "double_click", "hover", "drag", "scroll"}:
                    move(point)
                if op in {"click", "double_click"}:
                    for n in range(2 if op == "double_click" else 1):
                        if n:
                            pause(70, point)
                            move(point)
                        down("mouse", s["button"], point)
                        pause(s["hold_ms"], point)
                        up("mouse", s["button"], point)
                elif op == "drag":
                    down("mouse", s["button"], point)
                    end = s["end"]
                    parts = max(1, s["ms"]//20)
                    current = point
                    for n in range(1, parts+1):
                        pause(s["ms"]/parts, current)
                        current = tuple(round(a+(b-a)*n/parts) for a, b in zip(point, end))
                        move(current)
                    up("mouse", s["button"], end)
                elif op == "scroll":
                    guard(point, point)
                    send("scroll" if s["axis"] == "vertical" else "hscroll", s["amount"])
                elif op == "key":
                    for key in s["keys"]:
                        down("key", key)
                    pause(s["hold_ms"])
                    for key in reversed(s["keys"]):
                        up("key", key)
                elif op == "type":
                    encoded = s["encoded"]
                    for n in range(0, len(encoded), 2):
                        unit = int.from_bytes(encoded[n:n+2], "little")
                        if unit in (9, 10, 13):
                            kind, value = "key", 9 if unit == 9 else 13
                        else:
                            kind, value = "unicode", unit
                        down(kind, value)
                        up(kind, value)
                elif op == "wait":
                    pause(s["ms"])
                elif op == "capture":
                    final_frame = self.refresh(frame)
                    guard()
                    frame = final_frame
                elif op == "verify":
                    pause(s["ms"])
                    last, final_frame = self.verify(frame, s.get("region"), s["condition"], s["threshold"])
                    guard()
                    record["postcondition_verified"] = last["verified"]
                    record["verification"] = last
                    if not last["verified"]:
                        fail("postcondition_not_verified", "Pixel condition was not verified. Observe before any new input.")
                    frame = final_frame
                record["completed_steps"].append({"step": i, "op": op, "started_ms": round((started-start)*1000),
                                                  "completed_ms": round((time.monotonic()-start)*1000)})
            record["ok"] = True
            record["status"] = "completed"
        except Exception as exc:
            code = getattr(exc, "code", "outcome_unknown" if record["input_sent"] else "operation_failed")
            record.update(ok=False, status="cancelled" if code == "cancelled" else "failed",
                          error_code=code, error=str(exc),
                          outcome="unknown" if record["input_sent"] else "not_executed")
        finally:
            for kind, value in reversed(held):
                try:
                    d.send(kind, value, True)
                    released.append(kind)
                except Exception:
                    cleanup_failed.append(kind)
            record.update(released_owned_inputs=released, release_failures=cleanup_failed,
                          elapsed_ms=round((time.monotonic()-start)*1000))
            if final_frame:
                record["observation"] = final_frame["public"]
            with self.lock:
                self.operations[operation_key] = record | {"stop": stop}
                self._active = None
                try:
                    self.checkpoint()
                except OSError:
                    record["history_persistence"] = "final_update_failed"
            self.input_lock.release()
        return record, final_frame
