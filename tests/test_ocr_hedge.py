"""Gửi dự phòng khi Gemini trả lời chậm + chọn model Gemini nhanh."""
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ocr  # noqa: E402


class TestHedge(unittest.TestCase):
    def test_slow_first_call_uses_second(self):
        n = []
        lock = threading.Lock()

        def call():
            with lock:
                n.append(1)
                k = len(n)
            time.sleep(2 if k == 1 else 0.05)
            return k
        t = time.time()
        self.assertEqual(ocr.hedged(call, 0.2), 2)
        self.assertLess(time.time() - t, 1.5)

    def test_fast_call_single_request(self):
        n = []
        self.assertEqual(ocr.hedged(lambda: n.append(1) or "ok", 1), "ok")
        self.assertEqual(len(n), 1)

    def test_error_propagates(self):
        def bad():
            raise ocr.ExtractError("hết hạn mức", fatal=True)
        with self.assertRaisesRegex(ocr.ExtractError, "hết hạn mức"):
            ocr.hedged(bad, 0.1)

    def test_pick_fast_model(self):
        self.assertEqual(ocr.pick_default(["gemini-3.5-flash", "gemini-3.1-flash-lite"]), "gemini-3.1-flash-lite")
        self.assertEqual(ocr.pick_default(["gemini-2.5-flash", "gemini-3.5-flash"]), "gemini-3.5-flash")


if __name__ == "__main__":
    unittest.main()
