"""Khớp ticket khi AI đọc sai một phần mã: đầu mã đoán từ ngày, đuôi mã + SN/trạm + ngày, sửa năm (DB nhỏ tự tạo)."""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import matching as M  # noqa: E402

TICKETS = [
    # pk, ticket, SN, trạm, ngày tạo, onsite
    ("p1", "8T2G2JIQ20AO", "250301000037", "C.HNO1029", "2026-10-04 08:00:00", "2026-10-05 10:00:00"),
    ("p2", "8T7K9PQR2XLA", "250301000037", "C.HNO1029", "2026-10-04 09:00:00", "2026-10-05 11:00:00"),  # cùng SN, cùng ngày
    ("p3", "1027621270214410240", "250301000091", "C.HNO1029", "2025-10-05 08:00:00", "2025-10-06 08:00:00"),  # mã 19 số cũ, đuôi "0240"
    ("p4", "8R2S92SC0740", "252601000250", "C.HNO0813", "2026-09-29 08:00:00", "2026-09-30 08:00:00"),
    ("p5", "8RZZ71KQ0740", "259999000001", "C.DNI0001", "2026-09-29 08:00:00", "2026-09-30 08:00:00"),  # cùng đuôi, khác trạm/SN
    ("p6", "8QHGBI898WLC", "253001005049", "C.HCM1284", "2026-09-16 08:00:00", "2026-09-17 08:00:00"),
]


class TestPartialTicket(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # Windows: SQLite chưa nhả file kịp
        db = Path(cls.tmp.name) / "t.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tickets (ccts_ticket_pk TEXT PRIMARY KEY, ccts_ticket_id TEXT, third_ticket_id TEXT, "
                    "charge_box_id TEXT, station_code TEXT, create_time TEXT, raw_json TEXT)")
        for pk, tid, sn, st, ct, on in TICKETS:
            raw = {"cctsTicketPk": pk, "cctsTicketId": tid, "chargeBoxId": sn, "stationCode": st, "createTime": ct, "onSiteCompleteTime": on}
            con.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?)", (pk, tid, "", sn, st, ct, json.dumps(raw)))
        con.commit()
        con.close()
        cls.idx = M.TicketIndex(str(db))
        cls.idx.ensure()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def decide(self, ticket, sn, station, day):
        v = {"ticket_id": ticket, "sn": sn, "station_code": station, "date_onsite": day}
        d = M.decide(v, self.idx.search_for(v), "mdy")
        return d["status"], d["best"]["ticket"]["id"] if d["best"] else None

    def test_head_guessed_from_date(self):
        # AI đọc "87…" thay vì "8T…", phần còn lại đúng; SN đọc hỏng
        self.assertEqual(self.decide("872G2JIQ20AO", "core 120 kw", "", "10/5/2026"), ("matched", "8T2G2JIQ20AO"))

    def test_heads_index(self):
        self.assertIn("8T", self.idx.heads_near(date(2026, 10, 6)))
        self.assertNotIn("8T", self.idx.heads_near(date(2026, 9, 1)))

    def test_tail_plus_station_when_sn_garbage(self):
        # Trạm thiếu "C.", SN đọc sai hẳn, mã đọc sai đầu + giữa nhưng đúng 4 ký tự cuối
        self.assertEqual(self.decide("8RXX9XXX0740", "core 120 kw", "HNO 0813", "9/30/2026"), ("matched", "8R2S92SC0740"))

    def test_tail_alone_does_not_match(self):
        # Chỉ trùng đuôi "0740" + ngày, không có SN/trạm khớp: không được tự chọn
        status, got = self.decide("8XXXXXXX0740", "core 120 kw", "", "9/30/2026")
        self.assertNotEqual(status, "matched")

    def test_old_19_digit_ticket_not_matched_by_tail(self):
        # Ca thật: AI đọc "85162510240" — đuôi "0240" trùng mã 19 số của 1 năm trước cùng trạm
        status, got = self.decide("85162510240", "25030000034", "HNO1029", "10/4/2026")
        self.assertNotEqual(got, "1027621270214410240")

    def test_same_sn_same_day_needs_ticket(self):
        # Cùng SN, cùng ngày có 2 ticket: không đọc được ticket -> không tự chọn
        self.assertEqual(self.decide("", "250301000037", "HNO1029", "10/5/2026")[0], "ambiguous")
        # Đọc được đuôi mã -> chọn đúng
        self.assertEqual(self.decide("8XXXXXXQ20AO", "250301000037", "HNO1029", "10/5/2026"), ("matched", "8T2G2JIQ20AO"))

    def test_station_without_prefix_scores(self):
        t = {"id": "X", "thirdId": "", "sn": "", "station": "C.HNO0813", "onsiteTime": "", "createTime": ""}
        self.assertEqual(M.score({"station_code": "HNO0813"}, t, "mdy")["station"], 30)

    def test_switch_off_restores_old_behaviour(self):
        M.PARTIAL_ID = False
        try:
            self.assertNotEqual(self.decide("8RXX9XXX0740", "core 120 kw", "HNO 0813", "9/30/2026")[1], "8R2S92SC0740")
        finally:
            M.PARTIAL_ID = True


class TestYearFix(unittest.TestCase):
    REF = date(2026, 10, 15)

    def test_cases(self):
        f = lambda s: M.tem_day_fixed(s, self.REF)  # noqa: E731
        self.assertEqual(f("25/9"), date(2026, 9, 25))  # thiếu năm
        self.assertEqual(f("4/10/2026"), date(2026, 10, 4))  # đúng sẵn
        self.assertEqual(f("15/9/2020"), date(2026, 9, 15))  # sai năm -> năm của ngày chụp
        self.assertEqual(M.tem_day_fixed("20/12/2020", date(2027, 1, 10)), date(2026, 12, 20))  # tem tháng 12, chụp tháng 1
        self.assertEqual(f("1/3/2020"), date(2020, 3, 1))  # sửa năm vẫn quá xa ngày chụp: giữ nguyên, không bịa
        self.assertIsNone(f("xx"))

    def test_map_to_columns_flags_fixed_year(self):
        import ocr
        fields = {"ngay_thay_the": {"value": "15/9/2020", "uncertain": False, "note": ""}}
        col = ocr.map_to_columns(fields, "mdy", self.REF)["date_onsite"]
        self.assertEqual(col["value"], "9/15/2026")
        self.assertTrue(col["uncertain"])
        self.assertIn("đã sửa năm", col["note"])


if __name__ == "__main__":
    unittest.main()
