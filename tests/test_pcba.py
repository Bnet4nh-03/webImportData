"""Test nhập lên hệ thống PCBA (máy chủ giả lập theo API của trang /intake, không gọi web thật)."""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pcba_api as PB  # noqa: E402


class Resp:
    def __init__(self, data=None, status=200):
        self.data, self.status_code, self.ok = data, status, 200 <= status < 300

    def json(self):
        if self.data is None:
            raise ValueError
        return self.data


class FakePcba:
    """Giả lập API: kiểm tra đăng nhập, Idempotency-Key (gửi lại cùng khóa -> trả kết quả cũ), version phiếu."""

    def __init__(self, open_ticket=None, fail_on=None):
        self.headers, self.calls, self.idem = {}, [], {}
        self.logged = False
        self.open_ticket, self.fail_on = open_ticket, fail_on
        self.ticket = None
        self.created = 0
        self.att = 0

    def request(self, method, url, json=None, headers=None, timeout=None):
        path = url.split("/api", 1)[1]
        self.calls.append((method, path, json))
        key = (headers or {}).get("Idempotency-Key")
        if key and key in self.idem:
            return self.idem[key]
        if self.fail_on and self.fail_on(method, path):
            self.fail_on = None
            return Resp({"error": {"code": "INTERNAL", "message": "Lỗi tạm thời"}}, 500)
        r = self._handle(method, path, json or {})
        if key and r.ok:
            self.idem[key] = r
        return r

    def _ticket(self, **kw):
        self.ticket.update(kw, version=self.ticket["version"] + 1)
        return Resp({"ticket": dict(self.ticket)})

    def _handle(self, method, path, body):
        if path == "/auth/login":
            if body != {"username": "kythuat", "password": "mk123"}:
                return Resp({"error": {"code": "INVALID_CREDENTIALS", "message": "Sai tài khoản hoặc mật khẩu"}}, 401)
            self.logged = True
            return Resp({"user": {"username": "kythuat", "displayName": "Kỹ thuật"}})
        if not self.logged:
            return Resp({"error": {"code": "UNAUTHENTICATED", "message": "Chưa đăng nhập"}}, 401)
        if path == "/board-models":
            return Resp({"items": [{"id": 7, "code": "12070000153", "name": "HV Core V2.8"}]})
        if path == "/contractors":
            return Resp({"items": [{"id": 3, "name": "SE HN"}, {"id": 4, "name": "EC"}]})
        if path == "/settings/photo":
            return Resp({"photo": {"fullMaxEdge": 800, "fullQuality": 0.8, "thumbMaxEdge": 100}})
        if path.startswith("/boards/by-sn/"):
            return Resp({"board": {"boardModelId": 7, "boardModelCode": "12070000153", "boardModelName": "HV Core"},
                         "openTicket": self.open_ticket, "history": [{"status": "CLOSED"}]})
        if path == "/tickets" and method == "POST":
            self.created += 1
            self.ticket = {"id": 55, "version": 1, "status": "RECEIVED", "create": body}
            return Resp({"id": 55, "ticketNo": "PB-0055", "ticket": dict(self.ticket)})
        if path == "/tickets/55/attachments":
            self.att += 1
            return Resp({"attachment": {"id": self.att}, "upload": {"fullUrl": f"https://r2/f{self.att}",
                                                                     "thumbUrl": f"https://r2/t{self.att}",
                                                                     "contentType": "image/jpeg"}})
        if path.startswith("/attachments/") and path.endswith("/confirm"):
            return Resp({"attachment": {"id": int(path.split("/")[2]), "confirmed": True}})
        if path == "/tickets/55/transitions":
            assert body["version"] == self.ticket["version"], "sai version"
            ev, res = body["event"], body.get("result")
            if ev == "initial_test":
                return self._ticket(status={"PASS": "CLOSED", "FAIL": "REPAIRING", "DISCARD": "DISCARDED"}[res])
            if ev == "retest":
                return self._ticket(status="CLOSED" if res == "PASS" else "REPAIRING")
            if ev == "station_test":
                return self._ticket(station=res)
        if path == "/tickets/55" and method == "PATCH":
            assert body["version"] == self.ticket["version"]
            return self._ticket(failureCause=body["failureCause"])
        return Resp({"error": {"code": "NOT_FOUND", "message": path}}, 404)


class TestPcba(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.img = str(Path(self.tmp.name) / "tem.jpg")
        Image.new("RGB", (1600, 1200), "white").save(self.img)
        self.puts = []
        self.put = mock.patch("requests.put", lambda url, data, headers, timeout: (self.puts.append(url), Resp({}))[1])
        self.put.start()

    def tearDown(self):
        self.put.stop()
        self.tmp.cleanup()

    def job(self, **v):
        values = {"board": "12070000153 - HV Core V2.8 - C", "board_sn": " pb-sn 001 ", "contractor": "SE HN",
                  "test_tool_result": "FAIL", "failure_cause": "Hỏng tụ C13", "disposition": "Thay tụ",
                  "after_repair_result": "PASS", "charging_station_test_status": "OK", "ticket_id": "8T2G2JIQ20AO",
                  "sn": "250301000037", "date_onsite": "10/5/2026", "station_code": "C.HNO1029",
                  "original_phenomenon": "A0302 mất kết nối", **v}
        return PB.Job(values=values, photos_before=[self.img], photos_after=[self.img], qr_raw="PB-SN 001",
                      sn_source="QR")

    def client(self, fake):
        return PB.Client("kythuat", "mk123", session=fake)

    def test_norm_sn_like_web(self):
        self.assertEqual(PB.norm_sn(" ab\tc\x01 d "), "AB C D")
        self.assertIsNone(PB.norm_sn("   "))
        with self.assertRaises(PB.PcbaError):
            PB.norm_sn("x" * 101)

    def test_full_fail_flow(self):
        fake = FakePcba()
        prog = PB.submit(self.client(fake), self.job(), {})
        self.assertEqual((prog["ticketNo"], prog["status"]), ("PB-0055", "CLOSED"))
        body = fake.ticket["create"]
        self.assertEqual((body["sn"], body["snSource"], body["qrRaw"], body["snMissingReason"]), ("PB-SN 001", "QR", "PB-SN 001", None))
        self.assertEqual((body["boardModelId"], body["contractorId"], body["dateOnsite"]), (7, 3, "2026-10-05"))
        self.assertEqual(body["detailedRemarks"], "SN trụ sạc: 250301000037")
        events = [c[2]["event"] for c in fake.calls if c[1].endswith("/transitions")]
        self.assertEqual(events, ["initial_test", "retest", "station_test"])
        self.assertEqual(fake.ticket["station"], "OK")
        self.assertEqual(len(self.puts), 4)  # 2 ảnh x (ảnh đầy đủ + ảnh nhỏ)

    def test_open_ticket_blocks_create(self):
        fake = FakePcba(open_ticket={"id": 9, "ticketNo": "PB-0009"})
        with self.assertRaisesRegex(PB.PcbaError, "PB-0009"):
            PB.submit(self.client(fake), self.job(), {})
        self.assertEqual(fake.created, 0)

    def test_resume_after_error_no_duplicate(self):
        fake = FakePcba(fail_on=lambda m, p: p.endswith("/transitions"))
        prog = {}
        with self.assertRaises(PB.PcbaError):
            PB.submit(self.client(fake), self.job(), prog)
        self.assertEqual((prog["ticketNo"], fake.created), ("PB-0055", 1))
        PB.submit(self.client(fake), self.job(), prog)  # nhập lại: làm tiếp
        self.assertEqual(fake.created, 1)
        self.assertEqual(fake.att, 2)  # ảnh không bị tải lại
        self.assertEqual(prog["status"], "CLOSED")

    def test_no_sn_reason_and_discard(self):
        fake = FakePcba()
        job = self.job(board_sn="", sn_missing_reason="Tem hỏng", test_tool_result="Discard", failure_cause="",
                       disposition="Cháy IC nguồn")
        job.photos_after = []
        prog = PB.submit(self.client(fake), job, {})
        body = fake.ticket["create"]
        self.assertEqual((body["sn"], body["snMissingReason"], body["qrRaw"]), (None, "LABEL_DAMAGED", None))
        tr = [c[2] for c in fake.calls if c[1].endswith("/transitions")]
        self.assertEqual(tr, [{"version": 1, "event": "initial_test", "result": "DISCARD", "reason": "Cháy IC nguồn"}])
        self.assertEqual(prog["status"], "DISCARDED")

    def test_wrong_password(self):
        with self.assertRaisesRegex(PB.PcbaError, "Sai tài khoản"):
            PB.Client("kythuat", "sai", session=FakePcba()).login()

    def test_validate(self):
        errs = PB.validate(self.job(board_sn="", sn_missing_reason="", test_tool_result="", board=""))
        self.assertTrue(any("Board" in e for e in errs))
        self.assertTrue(any("Check SN" in e for e in errs))
        self.assertTrue(any("test_tool_result" in e for e in errs))
        self.assertEqual(PB.validate(self.job()), [])
        self.assertTrue(any("lý do" in e for e in PB.validate(self.job(test_tool_result="Discard", disposition="", failure_cause=""))))

    def test_jpeg_pair_sizes(self):
        p = PB.jpeg_pair(self.img, {"fullMaxEdge": 800, "thumbMaxEdge": 100})
        self.assertEqual((p["width"], p["height"]), (800, 600))
        self.assertEqual(Image.open(io.BytesIO(p["thumb"])).size, (100, 75))


class TestServerPcba(unittest.TestCase):
    """Server: quét QR -> SN bo mạch + Check SN; nút 📤 chạy luồng PCBA, ghi số phiếu vào dòng."""

    def setUp(self):
        import matching as M
        import server as S
        self.S = S
        self.tmp = tempfile.TemporaryDirectory()
        S.DATA = Path(self.tmp.name)
        S.IMAGES = S.DATA / "images"
        S.IMAGES.mkdir()
        S.index = M.TicketIndex(str(S.DATA / "khong_co.db"))
        S.store = S.Store(S.hub, S.index)
        S.store.config["importTarget"] = "pcba"
        self.fake = FakePcba()
        S.pcba._c = PB.Client("kythuat", "mk123", session=self.fake)
        self.put = mock.patch("requests.put", lambda url, data, headers, timeout: Resp({}))
        self.put.start()
        Image.new("RGB", (400, 300), "white").save(S.IMAGES / "item.jpg")
        self.r = S.store.new_row(image="item", status="done", values={
            "board": "12070000153 - HV Core V2.8 - C", "test_tool_result": "PASS", "contractor": "EC"})
        S.store.rows.append(self.r)

    def tearDown(self):
        self.put.stop()
        self.S.pcba.reset()
        self.tmp.cleanup()

    def test_qr_then_check_sn(self):
        S, r = self.S, self.r
        with mock.patch("threading.Thread"):  # chạy Check SN ngay trong test
            S.store.set_qr(r["id"], "  pb-sn 777\n")
        self.assertEqual((r["values"]["board_sn"], r["qr"]["source"]), ("PB-SN 777", "QR"))
        S.store.sn_check(r["id"])
        self.assertEqual((r["snCheck"]["status"], r["snCheck"]["found"], r["snCheck"]["boardModelCode"]),
                         ("ok", True, "12070000153"))
        S.store.patch(r["id"], {"values": {"board_sn": "PB-SN 778"}})  # gõ tay: không còn là QR
        self.assertEqual((r["qr"]["source"], r["snCheck"]), ("MANUAL", None))

    def test_import_creates_ticket_and_saves_number(self):
        S, r = self.S, self.r
        self.assertTrue(any("Check SN" in e for e in S.store.tg_preview(r["id"])["errors"]))
        r["values"]["sn_missing_reason"] = "Không có tem"
        pv = S.store.tg_preview(r["id"])
        self.assertEqual((pv["errors"], pv["target"]), ([], "pcba"))
        with mock.patch.object(S.tgworker, "push"):
            S.store.tg_enqueue(r["id"], False)
        S.tgworker.run_pcba(r["id"])
        self.assertEqual((r["tg"]["status"], r["tg"]["deviceCode"]), ("done", "PB-0055"))
        self.assertEqual(self.fake.ticket["create"]["snMissingReason"], "NO_LABEL")
        with self.assertRaises(ValueError):  # đã nhập: không nhập lại nếu không xác nhận
            S.store.tg_enqueue(r["id"], False)


if __name__ == "__main__":
    unittest.main()
