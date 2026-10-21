"""Test quy trình nhập Telegram bằng bot giả lập (tin nhắn/nút lấy nguyên văn từ lịch sử chat thật).

    python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import telegram_bot as T  # noqa: E402

BOARD = "12070000153 - HV Core V2.8 - Core 60/80/120/150Kw"
BOARDS = ["12010000047 - OCPP_SINGLE_V3.9", BOARD, "12050000012 - WIFI ETH V1.0"]
X = [[T.CANCEL]]


class FakeBot:
    """Bot giả lập theo workflow.json. Bấm/gửi -> bot trả lời bằng tin mới."""

    def __init__(self, devices=63, silent=False, silent_after_final=False, reask_date=False):
        self.msgs, self.mid, self.state = [], 1000, None
        self.devices, self.silent, self.silent_after_final, self.reask_date = devices, silent, silent_after_final, reask_date
        self.sent, self.clicks, self.photos = [], [], []
        self.data, self.photo_n = {}, 0

    # --- bot nói ---
    def bot(self, text, kb=None, state=None):
        self.mid += 1
        self.msgs.append({"mid": self.mid, "out": False, "text": text, "kb": kb or []})
        if state:
            self.state = state

    def user(self, text):
        self.mid += 1
        self.msgs.append({"mid": self.mid, "out": True, "text": text, "kb": []})

    # --- giao diện driver ---
    def open_chat(self):
        pass

    def last_mid(self):
        return self.mid

    def messages_after(self, mid):
        return [m for m in self.msgs if m["mid"] > mid]

    def last_bot_message(self):
        return next((m for m in reversed(self.msgs) if not m["out"]), None)

    def send_text(self, text):
        self.sent.append(text)
        self.user(text)
        if self.silent:
            return
        if text == "/repair":
            return self.bot("🔧 Quản lý Repair - chọn 1 thao tác:", [["➕ Thêm mới"], ["🔍 Tra cứu theo Code (Admin)"], ["🔎 Tra cứu chi tiết (Admin)"], ["✏️ Cập nhật (Admin)"]], "menu")
        if text == "/cancel":
            return self.bot("❎ Đã hủy.", state="idle")
        self.answer(text)

    def send_photo(self, path):
        self.photos.append((self.state, Path(path).name))
        self.user("")
        if self.state == "before":
            self.photo_n += 1
            self.bot(f"✅ Đã lưu ảnh {self.photo_n}/2 (TRƯỚC KHI TEST). Gửi thêm ảnh, hoặc bấm nút '✅ Xong / Bỏ qua' bên trên:", [[T.DONE_PHOTOS], [T.CANCEL]])
        elif self.state == "after_photo":
            self.photo_n += 1
            self.bot(f"✅ Đã lưu ảnh {self.photo_n}/3 (SAU KHI SỬA XONG VÀ PASS). Gửi thêm ảnh, hoặc bấm nút '✅ Xong / Bỏ qua' bên trên:", [[T.DONE_PHOTOS], [T.CANCEL]])
        elif self.state == "verify":
            self.finish()

    def click(self, mid, label):
        msg = next(m for m in self.msgs if m["mid"] == mid)
        assert label in T.labels(msg), (label, msg)
        self.clicks.append(label)
        if label == T.CANCEL:
            return self.bot("❎ Đã hủy.", state="idle")
        self.answer(label)

    # --- logic bot ---
    def answer(self, v):
        s = self.state
        if s == "menu" and v == "➕ Thêm mới":
            self.bot("➕ Thêm Repair mới\n\nBước 1: Chọn Board:", [[b] for b in BOARDS] + X, "board")
        elif s == "board":
            self.data["board"] = v.split(" - ")[0]
            if self.devices:
                self.bot(f"Board này đã có {self.devices} thiết bị. Chọn thao tác:", [["🔍 Tìm thiết bị", "➕ Thêm mới"], [T.CANCEL]], "device")
            else:
                self.bot(f"ℹ️ Chưa có thiết bị nào của board '{self.data['board']}' trước đây.\nMã thiết bị mới sẽ được tự động tạo khi lưu record.")
                self.contractor()
        elif s == "device":
            self.bot("✅ Sẽ tạo mã thiết bị mới khi lưu record.")
            self.contractor()
        elif s == "contractor":
            self.bot("✅ Đã bỏ trống nhà thầu (có thể cập nhật sau qua /repair update)." if v.startswith("🚫") else f"✅ Đã chọn nhà thầu: {v}")
            self.photo_n = 0
            self.bot("📷 Gửi ảnh TRƯỚC KHI TEST (bắt buộc tối thiểu 1, tối đa 2 ảnh).\nVui lòng gửi ảnh - tùy chọn bỏ qua chỉ xuất hiện sau khi đủ số ảnh tối thiểu.", X, "before")
        elif s == "before" and v == T.DONE_PHOTOS:
            self.bot("Bước 3: Kết quả test (test_tool_result):", [["✅ PASS"], ["❌ FAIL"], ["🗑 Discard"], X[0]], "test")
        elif s == "test":
            self.data["test"] = v
            if v in ("✅ PASS", "🗑 Discard"):  # lịch sử chat: cả hai sang thẳng Ticket ID
                self.ticket()
            else:
                self.bot("❌ FAIL - Nhập failure_cause (nguyên nhân lỗi):\nGõ /cancel để hủy.", state="cause")
        elif s == "cause":
            self.bot("Nhập disposition (vd: repair U12, repair C46):\nGõ /cancel để hủy.", state="disp")
        elif s == "disp":
            self.bot("Kết quả sau khi sửa (after_repair_result):", [["✅ PASS", "❌ FAIL"], X[0]], "after")
        elif s == "after":
            self.photo_n = 0
            self.bot("📷 Gửi ảnh SAU KHI SỬA XONG VÀ PASS (tối đa 3 ảnh).\nGửi ảnh, hoặc bấm nút '✅ Xong / Bỏ qua' bên trên khi xong.", [[T.DONE_PHOTOS], X[0]], "after_photo")
        elif s == "after_photo" and v == T.DONE_PHOTOS:
            self.bot("Trạng thái kiểm tra vận hành tại trạm sạc (charging_station_test_status):", [["⏳ Pending", "✅ OK"], X[0]], "station_status")
        elif s == "station_status":
            self.ticket()
        elif s == "ticket":
            self.data["ticket"] = None if v == T.NO_TICKET else v
            self.bot("ℹ️ Đã bỏ qua ticket ID." if v == T.NO_TICKET else "⚠️ Không tìm thấy thông tin ticket qua API. Vui lòng nhập thủ công.")
            self.bot("Nhập SN (Serial Number):", [[T.SKIP], X[0]], "sn")
        elif s == "sn":
            self.bot("Nhập Date onsite (định dạng vd 8/17/2026):", [[T.SKIP], X[0]], "date")
        elif s == "date":
            self.data["date"] = v
            if self.reask_date:
                return self.bot("Nhập Date onsite (định dạng vd 8/17/2026):", [[T.SKIP], X[0]], "date")
            self.bot("Nhập Original phenomenon (hiện tượng lỗi ban đầu):", [[T.SKIP], X[0]], "phen")
        elif s == "phen":
            self.bot("Nhập Station code:", [[T.SKIP], X[0]], "station")
        elif s == "station":
            self.bot("📷 Gửi ảnh Failure verification photo (ảnh xác minh hiện tượng lỗi thực tế):", [[T.SKIP], X[0]], "verify")
        elif s == "verify" and v == T.SKIP:
            self.finish()
        else:
            raise AssertionError(f"bot giả lập không hiểu {v!r} ở trạng thái {s}")

    def contractor(self):
        self.bot("Chọn nhà thầu thực hiện sửa chữa:\n(Nếu không thấy tên nhà thầu cần chọn, bấm '🚫 Bỏ trống' - có thể dùng /contractor add để thêm mới, rồi cập nhật lại record sau qua /repair update)",
                 [[c] for c in T.CONTRACTORS] + [[T.NO_CONTRACTOR], X[0]], "contractor")

    def ticket(self):
        self.bot("Nhập ticket ID:", [[T.NO_TICKET], X[0]], "ticket")

    def finish(self):
        if self.silent_after_final:
            self.silent = True
            return
        b = self.data["board"]
        if self.data["test"] == "🗑 Discard":  # nguyên văn Repair #924
            return self.bot(f"✅ Đã tạo Repair #958\n  Board: {b}\n  Thiết bị (code): -\n  Test tool result: DISCARD\n  Disposition: Discard\n"
                            f"  Ticket ID: {self.data.get('ticket')}\n  ℹ️ Đã bỏ qua ticket ID - các field liên quan đã được nhập thủ công.", state="idle")
        self.bot(f"✅ Đã tạo Repair #958\n  Board: {b}\n  Thiết bị (code): {b}BP\n  Test tool result: {self.data['test'].split()[-1]}\n"
                 f"  Ticket ID: {self.data.get('ticket')}\n  ℹ️ Không tìm thấy qua API - các field SN/Date onsite/Original phenomenon/Station code/Failure verification photo đã được nhập thủ công.", state="idle")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.img = str(Path(self.tmp.name) / "tem.jpg")
        Path(self.img).write_bytes(b"\xff\xd8fake")

    def tearDown(self):
        self.tmp.cleanup()

    def job(self, **values):
        base = {"board": BOARD, "test_tool_result": "PASS", "ticket_id": "8PHKM4VCI1HC", "sn": "252101000196",
                "date_onsite": "8/29/2026", "station_code": "HNO1486"}
        base.update(values)
        return T.RepairJob(values=base, photos_before=[self.img])

    def run_job(self, bot, job, **kw):
        steps = []
        r = T.Runner(bot, job, progress=steps.append, timeout=kw.pop("timeout", 0.3), poll=0.01, **kw)
        return r, steps


class TestFlow(Base):
    def test_pass_flow_returns_device_code(self):
        bot = FakeBot()
        r, steps = self.run_job(bot, self.job())
        res = r.run()
        self.assertEqual(res["repair_id"], "958")
        self.assertEqual(res["device_code"], "12070000153BP")
        self.assertEqual(bot.clicks, ["➕ Thêm mới", BOARD, "➕ Thêm mới", T.NO_CONTRACTOR, T.DONE_PHOTOS, "✅ PASS", T.SKIP, T.SKIP])
        self.assertEqual(bot.sent, ["/repair", "8PHKM4VCI1HC", "252101000196", "8/29/2026", "HNO1486"])
        self.assertEqual(steps[-1]["n"], steps[-1]["total"])
        self.assertEqual(steps[-1]["total"], 12)

    def test_fail_flow_with_after_photos_and_contractor(self):
        bot = FakeBot(devices=0)
        job = self.job(test_tool_result="FAIL", failure_cause="C11, C13 no problem\nU22 normal", disposition="Remove C11, C13",
                       after_repair_result="PASS", charging_station_test_status="OK", contractor="SE HN", ticket_id="",
                       date_onsite="2026-09-06")
        job.photos_after = [self.img, self.img]
        r, steps = self.run_job(bot, job)
        res = r.run()
        self.assertEqual(res["device_code"], "12070000153BP")
        self.assertIn("C11, C13 no problem / U22 normal", bot.sent)
        self.assertIn("9/6/2026", bot.sent)
        self.assertEqual([k for k, _ in bot.photos], ["before", "after_photo", "after_photo"])
        self.assertIn("✅ OK", bot.clicks)
        self.assertIn(T.NO_TICKET, bot.clicks)
        self.assertEqual(steps[-1]["total"], 17)

    def test_pass_skips_fail_steps_even_if_fail_fields_filled(self):
        bot = FakeBot()
        job = self.job(failure_cause="không được gửi", disposition="không được gửi", after_repair_result="PASS",
                       charging_station_test_status="OK")
        job.photos_after = [self.img]
        r, steps = self.run_job(bot, job)
        r.run()
        self.assertNotIn("không được gửi", bot.sent)
        self.assertEqual([k for k, _ in bot.photos], ["before"])  # ảnh sau sửa không gửi
        self.assertNotIn("✅ OK", bot.clicks)
        self.assertEqual(steps[-1]["total"], 12)

    def test_discard_skips_to_ticket_and_has_no_device_code(self):
        bot = FakeBot()
        r, steps = self.run_job(bot, self.job(test_tool_result="Discard", ticket_id=""))
        res = r.run()
        self.assertIn("🗑 Discard", bot.clicks)
        self.assertEqual(res["repair_id"], "958")
        self.assertEqual(res["device_code"], "")
        self.assertEqual(res["test_tool_result"], "DISCARD")
        self.assertEqual(next(s["n"] for s in steps if s["step"] == "ticket"), 7)  # ngay sau bước 6 "Kết quả test"
        self.assertEqual((steps[-1]["n"], steps[-1]["total"]), (12, 12))
        self.assertTrue(steps[-1]["detail"].endswith("không có mã thiết bị"))

    def test_bot_asking_cause_with_empty_value_stops(self):
        bot = FakeBot()
        orig = bot.answer

        def answer(v):  # bot giả định đổi luồng: PASS cũng hỏi failure_cause
            if bot.state == "test" and v == "✅ PASS":
                return bot.bot("❌ FAIL - Nhập failure_cause (nguyên nhân lỗi):\nGõ /cancel để hủy.", state="cause")
            orig(v)
        bot.answer = answer
        with self.assertRaises(T.BotError) as cm:
            self.run_job(bot, self.job())[0].run()
        self.assertIn("failure_cause", str(cm.exception))
        self.assertNotIn("", bot.sent)
        self.assertEqual(bot.sent[-1], "/cancel")

    def test_two_before_photos_then_done(self):
        bot = FakeBot()
        job = self.job()
        job.photos_before = [self.img, self.img, self.img]  # chỉ gửi tối đa 2
        self.run_job(bot, job)[0].run()
        self.assertEqual([k for k, _ in bot.photos], ["before", "before"])

    def test_dmy_date_converted_to_bot_format(self):
        bot = FakeBot()
        job = self.job(date_onsite="06/09/2026")
        job.date_format = "dmy"
        self.run_job(bot, job)[0].run()
        self.assertIn("9/6/2026", bot.sent)


class TestSafety(Base):
    def test_validation_blocks_before_sending(self):
        bot = FakeBot()
        job = self.job(board="", test_tool_result="Discard")
        job.photos_before = []
        with self.assertRaises(T.BotError) as cm:
            self.run_job(bot, job)[0].run()
        self.assertIn("Board", str(cm.exception))
        self.assertEqual(bot.sent, [])

    def test_fail_requires_fields(self):
        errs = T.validate(self.job(test_tool_result="FAIL"))
        self.assertTrue(any("failure_cause" in e for e in errs))
        self.assertTrue(any("charging_station_test_status" in e for e in errs))

    def test_bot_offline_times_out_not_uncertain(self):
        bot = FakeBot(silent=True)
        with self.assertRaises(T.BotError) as cm:
            self.run_job(bot, self.job())[0].run()
        self.assertFalse(cm.exception.uncertain)
        self.assertIn("không phản hồi", str(cm.exception))

    def test_silence_after_final_step_is_uncertain_and_not_cancelled(self):
        bot = FakeBot(silent_after_final=True)
        with self.assertRaises(T.BotError) as cm:
            self.run_job(bot, self.job())[0].run()
        self.assertTrue(cm.exception.uncertain)
        self.assertNotIn("/cancel", bot.sent)

    def test_unknown_board_cancels_session(self):
        bot = FakeBot()
        with self.assertRaises(T.BotError):
            self.run_job(bot, self.job(board="99999999999 - Không tồn tại"))[0].run()
        self.assertEqual(bot.clicks[-1], T.CANCEL)
        self.assertEqual(bot.state, "idle")

    def test_reasked_prompt_stops_and_cancels(self):
        bot = FakeBot(reask_date=True)
        with self.assertRaises(T.BotError) as cm:
            self.run_job(bot, self.job())[0].run()
        self.assertIn("hỏi lại", str(cm.exception))
        self.assertEqual(bot.clicks[-1], T.CANCEL)

    def test_stale_menu_ignored(self):
        bot = FakeBot()
        orig = bot.answer

        def answer(v):  # bot trả lời thêm một lệnh /repair cũ đang tồn đọng
            orig(v)
            if v == "➕ Thêm mới" and bot.state == "board":
                bot.bot("🔧 Quản lý Repair - chọn 1 thao tác:", [["➕ Thêm mới"]])
        bot.answer = answer
        res = self.run_job(bot, self.job())[0].run()
        self.assertEqual(res["repair_id"], "958")

    def test_emergency_stop_before_final_cancels_without_record(self):
        bot = FakeBot()
        with self.assertRaises(T.Stopped) as cm:
            self.run_job(bot, self.job(), stop=lambda: bot.state == "sn")[0].run()
        self.assertFalse(cm.exception.uncertain)
        self.assertEqual(bot.clicks[-1], T.CANCEL)
        self.assertEqual(bot.state, "idle")
        self.assertFalse(any(m["text"].startswith("✅ Đã tạo Repair") for m in bot.msgs))

    def test_emergency_stop_after_final_is_uncertain(self):
        bot = FakeBot(silent_after_final=True)
        with self.assertRaises(T.Stopped) as cm:
            self.run_job(bot, self.job(), stop=lambda: bot.silent)[0].run()
        self.assertTrue(cm.exception.uncertain)
        self.assertNotIn(T.CANCEL, bot.clicks)

    def test_emergency_stop_before_start_sends_nothing(self):
        bot = FakeBot()
        with self.assertRaises(T.Stopped):
            self.run_job(bot, self.job(), stop=lambda: True)[0].run()
        self.assertEqual(bot.sent, [])

    def test_late_cancel_from_previous_row_is_ignored(self):
        # Ca thật 21/10 17:43: dòng 5 hết giờ -> app hủy; "❎ Đã hủy." tới muộn, rơi vào phiên của dòng 6
        bot = FakeBot()
        orig = bot.send_text

        def send_text(text):
            orig(text)
            if text == "/repair":  # tin hủy muộn của phiên trước tới ngay sau /repair, trước menu
                bot.msgs.insert(-1, {"mid": bot.mid - 0.5, "out": False, "text": "❎ Đã hủy.", "kb": []})
        bot.send_text = send_text
        res = self.run_job(bot, self.job())[0].run()
        self.assertEqual(res["repair_id"], "958")

    def test_stale_messages_before_start_are_drained(self):
        bot = FakeBot()
        bot.bot("❎ Đã hủy.")  # tin tồn của phiên trước, có sẵn trước khi bắt đầu
        late = []
        orig = bot.messages_after

        def messages_after(mid):
            if not late:  # lần đọc đầu trong lúc dọn: bot gửi thêm 1 tin muộn
                late.append(1)
                bot.bot("⚠️ Phiên thêm Repair đã hết hạn hoặc không còn tồn tại")
            return orig(mid)
        bot.messages_after = messages_after
        r, _ = self.run_job(bot, self.job(), timeout=0.6)
        logs = []
        r.log = logs.append
        res = r.run()
        self.assertEqual(res["repair_id"], "958")
        self.assertTrue(any("tin tồn" in l for l in logs))

    def test_timeout_reports_step_nudges_once_and_diagnoses(self):
        bot = FakeBot()
        orig = bot.answer

        def answer(v):  # bot im lặng sau khi chọn nhà thầu
            if bot.state == "contractor":
                return
            orig(v)
        bot.answer = answer
        calls = []
        bot.nudge = lambda: calls.append("nudge")
        bot.diagnose = lambda: "trang visible | tin cuối của bot #1: 'Chọn nhà thầu'"
        r, _ = self.run_job(bot, self.job(), timeout=0.4)
        logs = []
        r.log = logs.append
        with self.assertRaises(T.BotError) as cm:
            r.run()
        self.assertIn("sau bước: Nhà thầu", str(cm.exception))
        self.assertEqual(calls, ["nudge"])
        self.assertTrue(any(l.startswith("Chẩn đoán:") for l in logs))

    def test_parse_created_real_message(self):
        text = ("✅ Đã tạo Repair #957\n  Board: 12070000153\n  Thiết bị (code): 12070000153BO\n  Test tool result: PASS\n"
                "  Ticket ID: 8PHKM4VCI1HC\n  ℹ️ Không tìm thấy qua API - các field ... đã được nhập thủ công.")
        p = T.parse_created(text)
        self.assertEqual((p["repair_id"], p["device_code"], p["ticket_id"]), ("957", "12070000153BO", "8PHKM4VCI1HC"))

    def test_preview_lists_every_step(self):
        pv = T.preview(self.job(contractor="EC"))
        self.assertEqual(len(pv), 12)
        self.assertEqual(pv[3]["value"], "EC")
        self.assertEqual(pv[-1]["value"], T.SKIP)


if __name__ == "__main__":
    unittest.main()
