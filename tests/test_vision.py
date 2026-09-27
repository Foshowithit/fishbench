"""Vision: local barcode heuristic + config selection + graceful failure."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fishbench.vision import (  # noqa: E402
    VisionConfig, _parse_jsonish, local_scan, scan_image,
)

try:
    import io

    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def make_stripes(w=240, h=90, bar=6) -> bytes:
    img = Image.new("L", (w, h), 255)
    px = img.load()
    for x in range(0, w, bar * 2):
        for dx in range(bar):
            for y in range(5, h - 5):
                px[x + dx, y] = 0
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def make_flat(w=240, h=90, val=128) -> bytes:
    img = Image.new("L", (w, h), val)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class ConfigTests(unittest.TestCase):
    def test_local_without_url(self):
        cfg = VisionConfig.from_env({})
        self.assertEqual(cfg.backend, "local")

    def test_llm_with_url(self):
        cfg = VisionConfig.from_env({"FISHBENCH_VISION_BASE_URL": "http://v"})
        self.assertEqual(cfg.backend, "llm")

    def test_model_floor(self):
        cfg = VisionConfig.from_env({})
        self.assertEqual(cfg.model, "deepseek-v4.1-flash")


@unittest.skipUnless(HAS_PIL, "Pillow required for image tests")
class LocalScanTests(unittest.TestCase):
    def test_stripes_look_like_barcode(self):
        r = local_scan(make_stripes(), "barcode.png")
        self.assertTrue(r.looks_like_barcode, r.detail)
        self.assertTrue(r.beep)
        self.assertEqual(r.detector, "local")

    def test_flat_gray_does_not_beep(self):
        r = local_scan(make_flat(), "wall.png")
        self.assertFalse(r.looks_like_barcode, r.detail)
        self.assertFalse(r.beep)

    def test_confidence_in_range(self):
        for blob in (make_stripes(), make_flat()):
            r = local_scan(blob, "x.png")
            self.assertGreaterEqual(r.confidence, 0.0)
            self.assertLessEqual(r.confidence, 1.0)

    def test_dict_serializable(self):
        d = local_scan(make_stripes(), "x.png").to_dict()
        import json
        json.dumps(d)  # must not raise
        self.assertIn("beep", d)


class DegradationTests(unittest.TestCase):
    def test_garbage_bytes_do_not_crash(self):
        # Undecodable bytes: either a VisionError-carrying graceful path in
        # scan_image, or a guessed result from the no-Pillow fallback.
        try:
            r = scan_image(b"\x00\x01\x02 not an image", "junk.png")
        except Exception as e:  # pragma: no cover
            self.fail(f"scan_image raised: {e}")
        self.assertIsInstance(r.looks_like_barcode, bool)

    def test_scan_image_local_backend_selection(self):
        cfg = VisionConfig(backend="local")
        if not HAS_PIL:
            r = scan_image(b"anything", "f", cfg)
            self.assertEqual(r.detector, "guessed")
        else:
            r = scan_image(make_flat(), "f", cfg)
            self.assertEqual(r.detector, "local")

    def test_llm_backend_without_url_falls_to_local(self):
        cfg = VisionConfig(backend="llm", base_url="")
        r = scan_image(b"x", "f", cfg)
        # no LLM lane → local attempted; undecodable bytes land on "error"
        self.assertIn(r.detector, ("local", "guessed", "error"))


class JsonishTests(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(_parse_jsonish('{"label": "fish", "looks_like_barcode": true}'),
                         {"label": "fish", "looks_like_barcode": True})

    def test_fenced_json(self):
        out = _parse_jsonish('```json\n{"confidence": 0.9}\n```')
        self.assertEqual(out["confidence"], 0.9)

    def test_surrounding_prose(self):
        out = _parse_jsonish('Sure! Here you go: {"label": "banana"} hope that helps')
        self.assertEqual(out["label"], "banana")

    def test_garbage_returns_empty(self):
        self.assertEqual(_parse_jsonish("i have no idea"), {})


if __name__ == "__main__":
    unittest.main()
