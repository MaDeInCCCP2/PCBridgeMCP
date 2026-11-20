"""Read-only grounding. Detector suggestions retain provenance and require inspection."""
import io
import math

from PIL import Image, ImageFilter


def overlap(a, b):
    x = max(0, min(a[0]+a[2], b[0]+b[2]) - max(a[0], b[0]))
    y = max(0, min(a[1]+a[3], b[1]+b[3]) - max(a[1], b[1]))
    return x*y / max(1, min(a[2]*a[3], b[2]*b[3]))


def outlines(image, limit):
    """Closed, high-contrast horizontal edges with aligned vertical sides."""
    gray = image.convert("L")
    gray.thumbnail((650, 650))
    sx, sy = image.width/gray.width, image.height/gray.height
    width, height = gray.size
    pixels = gray.filter(ImageFilter.FIND_EDGES).point(lambda n: 255 if n >= 55 else 0).load()
    top_edges = []
    boxes = []
    for y in range(2, height-2):
        start = None
        for x in range(2, width-1):
            if pixels[x, y] and x < width-2:
                if start is None:
                    start = x
                continue
            if start is None:
                continue
            l, r = start, x-1
            start = None
            if not 11 <= r-l <= 450:
                continue
            for a, b, t in reversed(top_edges):
                h = y-t
                if h > 105 or h < 8 or abs(l-a) > 3 or abs(r-b) > 3:
                    continue
                sides = sum(all(any(pixels[edge+dx, j] for dx in range(-2, 3)) for edge in (l, r))
                            for j in range(t+2, y-1, 2))
                if sides < .66*len(range(t+2, y-1, 2)):
                    continue
                box = [round(l*sx), round(t*sy), round((r-l+1)*sx), round((h+1)*sy)]
                if not any(overlap(box, old) > .8 for old in boxes):
                    boxes.append(box)
                break
            top_edges.append((l, r, y))
        if len(boxes) >= limit:
            break
        top_edges = [edge for edge in top_edges[-400:] if y-edge[2] <= 105]
    return [{"box": box, "label": "", "role": "outlined_region", "confidence": None,
             "source": "contour", "actionable": False} for box in boxes[:limit]]


def grounding(frame, query, exact, limit, ocr, find_text, ocr_box, adapter):
    with Image.open(io.BytesIO(frame["jpeg"])) as image:
        found = []
        diagnostics = {}
        try:
            lines = ocr(image)["lines"]
            hits = find_text(lines, query, exact) if query else [
                {"box": ocr_box(line["words"]), "text": line["text"], "score": None}
                for line in lines if line.get("words")]
            found += [{"box": h["box"], "label": h["text"], "role": "visible_text", "source": "ocr",
                       "text_match": h["score"], "confidence": None, "actionable": False} for h in hits[:limit]]
        except (OSError, ValueError) as exc:
            diagnostics["ocr_error"] = type(exc).__name__
        for contour in outlines(image, limit):
            labels = [e for e in found if e["source"] == "ocr" and overlap(contour["box"], e["box"]) >= .8]
            if labels:
                contour["label"] = " ".join(e["label"] for e in labels)
                contour["corroborated_by"] = ["ocr"]
            if not query or labels:
                found.append(contour)
        if adapter:
            try:
                candidates = adapter(frame, query)
                if not isinstance(candidates, list) or len(candidates) > 200:
                    raise ValueError("Invalid candidate list")
                for detection in candidates:
                    if not isinstance(detection, dict):
                        continue
                    box = detection.get("box")
                    if not isinstance(box, list) or len(box) != 4 or any(type(n) is not int for n in box):
                        continue
                    x, y, w, h = box
                    if not (0 <= x < x+w <= image.width and 0 <= y < y+h <= image.height):
                        continue
                    score = detection.get("confidence")
                    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
                        score = None
                    found.append({"box": box, "label": str(detection.get("label", ""))[:256],
                                  "role": str(detection.get("role", ""))[:64], "confidence": score,
                                  "source": "vision_adapter", "actionable": False})
            except (OSError, ValueError) as exc:
                diagnostics["adapter_error"] = getattr(exc, "code", type(exc).__name__)
        return found[:limit], diagnostics
