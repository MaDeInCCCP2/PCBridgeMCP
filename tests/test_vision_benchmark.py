import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image


APP = Path(__file__).resolve().parents[1]


class VisionBenchmark(unittest.TestCase):
    def test_scores_ranked_rpc_candidates_and_does_not_print_image_path_or_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            Image.new("RGB", (20, 20)).save(root / "test-image.jpg")
            cases = root / "cases.json"
            cases.write_text(json.dumps([
                {"image": "test-image.jpg", "query": "fixture label", "boxes": [[2, 3, 4, 5]]}
            ]), encoding="utf-8")
            adapter = root / "detector.py"
            adapter.write_text(
                "import json, sys\n"
                "for line in sys.stdin:\n"
                "    req = json.loads(line)\n"
                "    assert req['operation'] == 'detect' and req['arguments']['size'] == [20, 20]\n"
                "    candidates = [{'box': [-1, 0, 4, 5]}, {'box': [9, 9, 3, 3]}, {'box': [2, 3, 4, 5]}]\n"
                "    print(json.dumps({'id': req['id'], 'ok': True, 'result': {'candidates': candidates}}), flush=True)\n",
                encoding="utf-8",
            )
            completed = subprocess.run([sys.executable, str(APP / "tools" / "benchmark_vision.py"), str(cases),
                                        "--adapter", str(adapter)], capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("fixture label", completed.stdout)
        self.assertNotIn("test-image.jpg", completed.stdout)
        result, = json.loads(completed.stdout)["adapters"]
        self.assertEqual((result["cases"], result["error_count"], result["top1_hits"], result["top5_hits"]), (1, 0, 0, 1))
        self.assertGreaterEqual(result["p95_ms"], result["p50_ms"])
