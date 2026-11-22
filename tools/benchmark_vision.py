"""Benchmark trusted local detector scripts over explicitly labelled screenshots."""
import argparse
import json
from pathlib import Path
from statistics import median
import sys
import tempfile
from time import perf_counter

from PIL import Image

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP / "server"))
from computer_use import ComputerUseClient
from visual_grounding import adapter_detections


def load_cases(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, list) or not 1 <= len(data) <= 500:
        raise ValueError("Use a JSON array of 1 to 500 labelled cases.")
    cases = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict) or not isinstance(item.get("image"), str):
            raise ValueError(f"Case {index} needs an image path.")
        query = item.get("query")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 256:
            raise ValueError(f"Case {index} needs a query of 1 to 256 characters.")
        image = (path.parent / item["image"]).resolve(strict=True)
        with Image.open(image) as source:
            if source.format != "JPEG" or not all(1 <= side <= 2400 for side in source.size):
                raise ValueError(f"Case {index} needs a JPEG within 2400 by 2400 pixels.")
            size = source.size
        boxes = item.get("boxes")
        if not isinstance(boxes, list) or not 1 <= len(boxes) <= 16:
            raise ValueError(f"Case {index} needs 1 to 16 accepted target boxes.")
        valid = adapter_detections([{"box": box} for box in boxes], size)
        if len(valid) != len(boxes):
            raise ValueError(f"Case {index} has a target box outside the image or in the wrong format.")
        cases.append((image, size, query, boxes))
    return cases


def iou(a, b):
    w = max(0, min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0]))
    h = max(0, min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1]))
    intersection = w * h
    return intersection / (a[2] * a[3] + b[2] * b[3] - intersection)


def benchmark(client, cases, threshold):
    timings, top1, top5, failures = [], 0, 0, 0
    for image, size, query, boxes in cases:
        start = perf_counter()
        try:
            raw = client.call("detect", {"image_path": str(image), "size": size, "query": query})
            if not isinstance(raw, dict):
                raise ValueError("Invalid detector reply")
            ranked = adapter_detections(raw.get("candidates"), size)[:5]
            hits = [max(iou(candidate["box"], box) for box in boxes) >= threshold for candidate in ranked]
            top1 += bool(hits and hits[0])
            top5 += any(hits)
        except (ValueError, OSError):
            failures += 1
        timings.append((perf_counter() - start) * 1000)
    return {"cases": len(cases), "error_count": failures, "top1_hits": top1, "top5_hits": top5,
            "p50_ms": round(median(timings), 1),
            "p95_ms": round(sorted(timings)[(95 * len(timings) + 99) // 100 - 1], 1)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="JSON array of {image, query, boxes} cases")
    parser.add_argument("--adapter", type=Path, action="append", required=True, help="Trusted local Python script")
    parser.add_argument("--iou", type=float, default=.5, help="Minimum intersection over union for a hit")
    args = parser.parse_args()
    if not 0 < args.iou <= 1:
        parser.error("--iou must be greater than 0 and at most 1")
    try:
        cases = load_cases(args.manifest)
        adapters = [path.resolve(strict=True) for path in args.adapter]
        if any(path.suffix.lower() != ".py" or not path.is_file() for path in adapters):
            raise ValueError("Adapters must be Python script files.")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    reports = []
    with tempfile.TemporaryDirectory(prefix="pcbridge-vision-benchmark-") as directory:
        for index, adapter in enumerate(adapters):
            client = ComputerUseClient(adapter, Path(directory) / f"adapter-{index}.log",
                                       timeout=8, kill_tree=True, close_at_exit=False)
            try:
                reports.append({"adapter": adapter.name, **benchmark(client, cases, args.iou)})
            finally:
                client.close()
    print(json.dumps({"iou_threshold": args.iou, "adapters": reports}))
    return int(any(report["error_count"] for report in reports))


if __name__ == "__main__":
    raise SystemExit(main())
