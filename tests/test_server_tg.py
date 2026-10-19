"""Test server: hàng đợi nhập bot, lưu mã thiết bị vào dòng, chống nhập trùng (bot giả lập, thư mục data tạm)."""
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import matching as M  # noqa: E402
import server as S  # noqa: E402
from test_telegram_bot import BOARD, FakeBot  # noqa: E402


class TestServerTelegram(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        S.DATA = Path(cls.tmp.name)
        S.IMAGES = S.DATA / "images"
        S.index = M.TicketIndex(str(S.DATA / "khong_co.db"))
        S.store = S.Store(S.hub, S.index)
        cls.bots = []
        S.tgworker.driver_factory = lambda cfg, log: cls.bots.append(FakeBot()) or cls.bots[-1]
        S.tgworker.start()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def add_row(self, **values):
        img = "itest" + str(len(S.store.rows))
        (S.IMAGES / f"{img}.jpg").write_bytes(b"\xff\xd8fake")
        base = {"board": BOARD, "test_tool_result": "PASS", "sn": "252101000196", "date_onsite": "8/29/2026"}
        base.update(values)
        r = S.store.new_row(image=img, values=base, status="done")
        S.store.rows.append(r)
        return r

    def wait(self, r, status=("done", "error", "uncertain"), t=5):
        end = time.time() + t
        while time.time() < end and (r.get("tg") or {}).get("status") not in status:
            time.sleep(0.05)
        return r["tg"]

    def test_import_saves_device_code_and_blocks_duplicate(self):
        r = self.add_row()
        S.store.tg_enqueue(r["id"], force=False)
        tg = self.wait(r)
        self.assertEqual(tg["status"], "done", tg.get("error"))
        self.assertEqual(tg["deviceCode"], "12070000153BP")
        self.assertEqual(tg["repairId"], "958")
        self.assertEqual(tg["n"], tg["total"])
        self.assertTrue(any("Repair #958" in line for line in tg["log"]))
        self.assertIn("12070000153BP", (S.DATA / "state.json").read_text("utf-8"))
        self.assertIn('"event": "done"', (S.DATA / "telegram_log.jsonl").read_text("utf-8"))
        with self.assertRaises(ValueError):
            S.store.tg_enqueue(r["id"], force=False)
        S.store.tg_enqueue(r["id"], force=True)  # nhập lại có chủ ý: giữ lịch sử lần trước
        tg = self.wait(r, ("done",))
        self.assertEqual(tg["history"][0]["deviceCode"], "12070000153BP")

    def test_emergency_stop_stops_running_and_clears_queue(self):
        class SlowBot(FakeBot):
            def messages_after(self, mid):
                time.sleep(0.03)
                return super().messages_after(mid)

        bots = []
        old = S.tgworker.driver_factory
        S.tgworker.driver_factory = lambda cfg, log: bots.append(SlowBot()) or bots[-1]
        try:
            a, b = self.add_row(), self.add_row()
            S.store.tg_enqueue(a["id"], force=False)
            S.store.tg_enqueue(b["id"], force=False)
            end = time.time() + 5
            while time.time() < end and (a.get("tg") or {}).get("n", 0) < 4:
                time.sleep(0.01)
            res = S.tgworker.stop()
            self.assertEqual(res["running"], a["id"])
            self.assertEqual(res["dropped"], 1)
            tg = self.wait(a)
            self.assertEqual(tg["status"], "error")
            self.assertIn("dừng khẩn cấp", tg["error"])
            self.assertEqual(bots[0].state, "idle")  # đã bấm Hủy trên bot
            self.assertEqual(b["tg"]["status"], "")
            self.assertEqual(len(bots), 1)  # dòng chờ không được chạy
            S.store.tg_enqueue(b["id"], force=False)  # sau khi dừng vẫn nhập tiếp bình thường
            self.assertEqual(self.wait(b)["status"], "done")
        finally:
            S.tgworker.driver_factory = old

    def test_invalid_row_rejected_before_queue(self):
        r = self.add_row(board="")
        with self.assertRaises(ValueError) as cm:
            S.store.tg_enqueue(r["id"], force=False)
        self.assertIn("Board", str(cm.exception))
        self.assertIsNone(r.get("tg"))

    def test_preview(self):
        r = self.add_row(contractor="EC")
        pv = S.store.tg_preview(r["id"])
        self.assertEqual(pv["errors"], [])
        self.assertEqual(pv["steps"][3]["value"], "EC")


if __name__ == "__main__":
    unittest.main()
