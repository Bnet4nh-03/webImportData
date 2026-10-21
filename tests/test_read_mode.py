"""Test chế độ đọc nhanh: chỉ đọc Mã trạm/Model/SN/Ngày; không khớp DB thì tự đọc đầy đủ (AI và DB giả lập)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import matching as M  # noqa: E402
import ocr  # noqa: E402
import server as S  # noqa: E402


def fake_result(quick: bool, sn: str) -> dict:
    f = {k: {"value": "", "uncertain": False, "note": "", "chars": None} for k, _ in ocr.FIELDS}
    f["serial"]["value"], f["ma_tram"]["value"], f["ngay_thay_the"]["value"] = sn, "C.DTH0126", "23/09/2026"
    if not quick:
        f["ticket"]["value"], f["mo_ta_loi"]["value"] = "8RW7G9DG1GXS", "A0302 mất kết nối"
    return {"is_label": True, "issue": "", "fields": f}


class TestReadMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        S.DATA = Path(self.tmp.name)
        S.IMAGES = S.DATA / "images"
        S.index = type("FakeIndex", (), {"ok": True, "count": 1})()  # giả lập DB có dữ liệu
        self.store = S.Store(S.hub, S.index)
        self.calls = []
        self.orig = ocr.extract
        self.sn = "253001005343"

        def extract(cfg, jpeg):
            self.calls.append(cfg.get("readMode"))
            return fake_result(ocr.is_quick(cfg), self.sn)
        ocr.extract = extract
        self.store._quick_ok = lambda res, fmt: res["fields"]["serial"]["value"] == "253001005343"

    def tearDown(self):
        ocr.extract = self.orig
        self.tmp.cleanup()

    def test_quick_matches_db_reads_once(self):
        res, rot, mode = self.store._read({"readMode": "quick", "dateFormat": "mdy"}, b"", "r1")
        self.assertEqual((self.calls, mode), (["quick"], "quick"))
        self.assertEqual(res["fields"]["ticket"]["value"], "")  # ticket sẽ lấy từ DB

    def test_quick_without_db_match_falls_back_to_full(self):
        self.sn = "999999999999"
        r = self.store.new_row(image="i1")
        self.store.rows.append(r)
        res, rot, mode = self.store._read({"readMode": "quick", "dateFormat": "mdy"}, b"", r["id"])
        self.assertEqual((self.calls, mode), (["quick", "full"], "quick+full"))
        self.assertEqual(res["fields"]["ticket"]["value"], "8RW7G9DG1GXS")
        self.assertIn("đọc đầy đủ", r["warn"])

    def test_full_mode_unchanged(self):
        self.store._read({"readMode": "full", "dateFormat": "mdy"}, b"", "r1")
        self.assertEqual(self.calls, ["full"])

    def test_row_override_full(self):
        r = self.store.new_row(image="i1", status="done")
        self.store.rows.append(r)
        S.worker.push = lambda *a, **k: None  # không chạy hàng đợi thật
        self.store.retry(r["id"], False, "full")
        self.assertEqual(r["readModeNext"], "full")

    def test_unlink_clear_for_manual_entry(self):
        r = self.store.new_row(status="done", values={"ticket_id": "8762710240", "sn": "25030000034", "station_code": "HNO1029",
                                                    "date_onsite": "10/4/2026", "original_phenomenon": "mô tả DB", "board": "B"})
        r.update(match={"status": "matched", "pk": "p1", "id": "8T2G2JIQ20AO", "how": "auto"}, src={"ticket_id": "fixed"},
                 tem={"ticket_id": "8762710240"}, edited={"original_phenomenon": True, "board": True})
        self.store.rows.append(r)
        self.store.match = lambda rid: None  # không cần DB thật
        self.store.choose(r["id"], "", clear=True)
        self.assertEqual({k: r["values"][k] for k in M.COMPARE_KEYS}, dict.fromkeys(M.COMPARE_KEYS, ""))
        self.assertEqual(r["values"]["board"], "B")  # ô sửa chữa giữ nguyên
        self.assertEqual((r["match"]["how"], r["match"]["pk"]), ("manual", ""))
        rematched = []
        self.store.match = lambda rid: rematched.append(rid)
        self.store.patch(r["id"], {"values": {"ticket_id": "8ABCDEF12345"}, "commit": True})
        self.assertEqual((r["values"]["ticket_id"], rematched), ("8ABCDEF12345", []))  # nhập tay: không đối chiếu lại

    def test_rotate_label_saves_new_image_and_reads_upright(self):
        import io
        from PIL import Image
        S.IMAGES.mkdir(parents=True, exist_ok=True)
        buf = io.BytesIO()
        Image.new("RGB", (40, 20), "white").save(buf, "JPEG")
        (S.IMAGES / "iold.jpg").write_bytes(buf.getvalue())
        r = self.store.new_row(image="iold", status="done")
        self.store.rows.append(r)
        self.store.rotate_label(r["id"], 90)
        self.assertNotEqual(r["image"], "iold")
        self.assertFalse((S.IMAGES / "iold.jpg").exists())
        self.assertEqual(Image.open(S.IMAGES / f"{r['image']}.jpg").size, (20, 40))
        self.assertTrue(r["upright"])
        with self.assertRaises(ValueError):
            self.store.rotate_label(r["id"], 45)
        # Đọc lại ảnh đã đứng: không xoay thêm theo chiều đã học (rotPref)
        seen = {}
        self.store.config.update(rotPref=90, readMode="full")
        self.store._read = lambda cfg, jpeg, rid: seen.update(cfg) or (fake_result(False, self.sn), 0, "full")
        self.store.match = lambda rid: None
        r["status"] = "queued"
        self.store.run_ocr(r["id"])
        self.assertEqual(seen.get("rotPref"), 0)

    def test_detected_orientation_read_first(self):
        rots, orig = [], (ocr.rotate_jpeg, ocr.detect_orientation, ocr.can_rotate)
        ocr.rotate_jpeg = lambda jpeg, deg: rots.append(deg) or jpeg
        ocr.can_rotate = lambda: True
        try:
            r = self.store.new_row(image="i1")
            self.store.rows.append(r)
            cfg = {"provider": "ollama", "rotPref": 90, "dateFormat": "mdy", "readMode": "full"}
            # nhận chiều chắc chắn 270 -> đọc 270 trước, khớp ngay -> 1 lần đọc
            ocr.detect_orientation = lambda jpeg: {"deg": 270, "sure": True}
            self.store._plausible = lambda res, fmt: 60
            self.assertEqual(self.store._read_label(cfg, b"", r["id"]), (fake_result(False, self.sn), 270))
            self.assertEqual(rots, [270])
            self.assertEqual(r["orient"]["deg"], 270)
            # đọc sai chiều (không khớp DB) -> vẫn thử các chiều khác, tối đa 3 lần
            rots.clear()
            self.store._plausible = lambda res, fmt: 0
            self.store._read_label(cfg, b"", r["id"])
            self.assertEqual(len(rots), 3)
            self.assertEqual(rots[0], 270)
            # không chắc -> thứ tự cũ theo chiều đã học
            rots.clear()
            ocr.detect_orientation = lambda jpeg: {"deg": None, "sure": False}
            self.store._plausible = lambda res, fmt: 60
            self.store._read_label(cfg, b"", r["id"])
            self.assertEqual(rots, [90])
            # ảnh đã xoay đứng -> không chạy nhận chiều, đọc 0° trước
            rots.clear()
            ocr.detect_orientation = lambda jpeg: self.fail("không được gọi khi ảnh đã đứng")
            self.store._read_label({**cfg, "rotPref": 0, "_upright": True}, b"", r["id"])
            self.assertEqual(rots, [0])
        finally:
            ocr.rotate_jpeg, ocr.detect_orientation, ocr.can_rotate = orig

    def test_codes_mode_reads_once_without_fallback(self):
        self.sn = "999999999999"  # dù SN không khớp DB: đã đọc Ticket nên không đọc lại đầy đủ
        res, rot, mode = self.store._read({"readMode": "codes", "dateFormat": "mdy"}, b"", "r1")
        self.assertEqual((self.calls, mode), (["codes"], "codes"))

    def test_codes_prompt_schema(self):
        keys = ocr.READ_MODES["codes"]
        self.assertEqual(set(keys), {"ticket", "serial", "ma_tram", "ngay_thay_the"})
        schema = ocr.subset_schema(keys)
        self.assertEqual(set(schema["properties"]["fields"]["required"]), set(keys))
        system, user = ocr.subset_prompt(keys)
        self.assertIn("BỎ QUA Model Trụ Sạc, Mã lỗi, Mô Tả Lỗi", system)
        self.assertIn("ticket", system.split("Với ")[1])  # ticket đọc từng ký tự
        prompt, lschema, jkeys = ocr.local_subset(keys)
        self.assertEqual(jkeys, {"ticket": "ticket", "sn": "serial", "ma_tram": "ma_tram", "date": "ngay_thay_the"})
        self.assertIn('"ticket":"..."', prompt)
        self.assertNotIn("mo_ta", prompt)

    def test_quick_prompt_and_schema_skip_ticket(self):
        self.assertNotIn("ticket", ocr.QUICK_SCHEMA["properties"]["fields"]["required"])
        self.assertEqual(set(ocr.QUICK_SCHEMA["properties"]["fields"]["properties"]), set(ocr.QUICK_FIELDS))
        cols = ocr.map_to_columns(ocr.normalize({"fields": {"serial": {"value": "1", "uncertain": False, "note": ""}}})["fields"], "mdy")
        self.assertEqual((cols["ticket_id"]["value"], cols["original_phenomenon"]["value"]), ("", ""))


if __name__ == "__main__":
    unittest.main()
