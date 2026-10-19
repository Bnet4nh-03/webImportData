"""Đo chất lượng đối chiếu ticket (matching.py) trên ccts_tickets.db thật — chỉ đọc, không gọi AI.

    python tools/eval_matching.py              # bộ đọc thật + 500 tem mô phỏng
    python tools/eval_matching.py --n 2000     # nhiều mẫu mô phỏng hơn

Kết quả mỗi tem: ĐÚNG (tự khớp đúng ticket) | NHẦM (tự khớp SAI ticket — lỗi nguy hiểm nhất, không được tăng)
| ?N (nhiều ticket gần giống, người dùng chọn) | KHÔNG (không tìm thấy). "trong ?N" = ticket đúng nằm trong nhóm gần giống.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import matching as M  # noqa: E402

DB = r"D:\00_WORKSPACE\01_Python\04_Takedata_CCVN\ccts_tickets.db"
FMT = "mdy"

# Chữ AI (qwen3-vl:4b) đã thật sự đọc trên tem thật (state.json + các lần đo 15-16/10/2026), kèm ticket đúng.
# Ngày ghi như AI đọc trên tem (ngày/tháng/năm).
REAL_READS = [
    ("8T2G2JIQ20AO", "8762710240", "25030000034", "HNO1029", "4/10/2026"),
    ("8T2G2JIQ20AO", "8716710290", "25030000034", "HNO1029", "4/10/2026"),
    ("8T2G2JIQ20AO", "85162510240", "25030000034", "HNO1029", "4/10/2026"),
    ("8T2G2JIQ20AO", "87162570240", "23030000032", "HNO1029", "4/10/2026"),
    ("8T2G2JIQ20AO", "8726270240", "23030000037", "HNO1029", "4/10/2026"),
    ("8T2G2JIQ20AO", "8716710240", "2503000034", "HNO1029", "4/10/2026"),
    ("8QHGBI898WLC", "80#G81838W", "253001005049", "C-HCM-4381", "1/3/2020"),
    ("8QHGBI898WLC", "80#G81838WL", "253001005049", "C4CM4381", "1/3/2020"),
    ("8QHGBI898WLC", "8@#G8I838W", "253001005049", "C.4CM-281", "17/5/2020"),
    ("8R2S92SC0740", "8R 2S92S C0746", "core 120 kw", "HNO 0813", "15/9/2026"),
    ("8R2S92SC0740", "8R 2S 92S C0746", "Y core 120 kW", "HNo 0813", "15/9/2026"),
    ("8S5N9YG6C2YO", "8S5N9Y66C2Y0", "25240A000169", "HNV04598", "25/9"),
    ("1116193878290857984", "M16193878290857984", "242401000135", "T050031", "17/6/2026"),
]

_ALNUM = "0123456789ABCDEFGHJKLMNPRTUVWXY"


BASELINE = False  # --baseline: cách khớp cũ (không đuôi/đầu mã, không sửa năm) để so sánh


def tem_values(ticket: str, sn: str, station: str, day: str, ref: date) -> dict:
    """Giá trị như sau bước OCR (ocr.map_to_columns): ngày tem dd/mm/yyyy -> định dạng bảng."""
    d = M.tem_day(day) if BASELINE else M.tem_day_fixed(day, ref)
    return {"ticket_id": ticket, "sn": sn, "station_code": station, "date_onsite": M.fmt_day(d, FMT) if d else day}


def judge(idx: M.TicketIndex, values: dict, truth: str) -> tuple[str, int]:
    d = M.decide(values, idx.search_for(values), FMT)
    if d["status"] == "matched":
        return ("ĐÚNG" if d["best"]["ticket"]["id"] == truth else "NHẦM"), d["alts"]
    if d["status"] == "ambiguous":
        ties = [r for r in d["ranked"] if r["score"] >= d["ranked"][0]["score"] - 10]
        return ("?N có" if any(r["ticket"]["id"] == truth for r in ties) else "?N không"), d["alts"]
    return "KHÔNG", 0


def corrupt(s: str, k: int, rnd: random.Random) -> str:
    s = list(s)
    for _ in range(k):
        if not s:
            break
        i = rnd.randrange(len(s))
        op = rnd.random()
        if op < 0.75:
            s[i] = rnd.choice(_ALNUM)
        elif op < 0.88:
            del s[i]
        else:
            s.insert(i, rnd.choice(_ALNUM))
    return "".join(s)


def synth(con: sqlite3.Connection, n: int, seed: int) -> list[tuple]:
    """Tem mô phỏng từ ticket thật 90 ngày gần nhất, cài lỗi giống kiểu AI đọc sai đã thấy trên tem thật."""
    rnd = random.Random(seed)
    # Mẫu cố định theo seed (ORDER BY random() của SQLite không đặt seed được -> mỗi lần một bộ khác, không so sánh được)
    pks = [pk for (pk,) in con.execute(
        "SELECT ccts_ticket_pk FROM tickets WHERE length(ccts_ticket_id) = 12 AND create_time_norm >= '2026-07-15' "
        "ORDER BY ccts_ticket_pk")]
    pick = rnd.sample(pks, min(len(pks), n * 3))
    rows = []
    for i in range(0, len(pick), 500):
        chunk = pick[i:i + 500]
        got = {r[0]: r[1:] for r in con.execute(
            f"SELECT ccts_ticket_pk, ccts_ticket_id, charge_box_id, station_code, raw_json FROM tickets "
            f"WHERE ccts_ticket_pk IN ({','.join('?' * len(chunk))})", chunk)}
        rows += [got[pk] for pk in chunk if pk in got]
    out = []
    for tid, sn, st, raw in rows:
        on = M.parse_day(json.loads(raw).get("onSiteCompleteTime"), FMT)
        if not (on and sn and st):
            continue
        # Ticket: 60% sai 1-2 ký tự đầu (AI hay đọc "8T…" thành "87…"), thêm 0-3 lỗi ở phần còn lại.
        t = tid
        if rnd.random() < 0.6:
            t = t[0] + rnd.choice(_ALNUM) + (rnd.choice(_ALNUM) if rnd.random() < 0.4 else t[2]) + t[3:]
        t = t[:3] + corrupt(t[3:], rnd.choice((0, 0, 1, 1, 2, 3)), rnd)
        # SN: 0-3 lỗi, 10% đọc nhầm sang chữ khác hẳn (vd "core 120 kw")
        s = "core 120 kw" if rnd.random() < 0.1 else corrupt(sn, rnd.choice((0, 0, 1, 2, 3)), rnd)
        # Trạm: AI bỏ "C.", 0-2 lỗi
        stn = corrupt(st.split(".", 1)[-1], rnd.choice((0, 0, 1, 2)), rnd)
        # Ngày tem: lệch -1..+1 so với onsite; 12% sai năm (2020), 8% không đọc được
        d = on + timedelta(days=rnd.choice((-1, 0, 0, 1)))
        y = 2020 if rnd.random() < 0.12 else d.year
        day = "" if rnd.random() < 0.08 else f"{d.day}/{d.month}/{y}"
        out.append((tid, t, s, stn, day, on + timedelta(days=rnd.randrange(0, 4))))  # ngày chụp tem: sau onsite 0-3 ngày
        if len(out) >= n:
            break
    return out


def report(name: str, results: list[str]) -> None:
    c = Counter(results)
    n = len(results)
    keys = ("ĐÚNG", "NHẦM", "?N có", "?N không", "KHÔNG")
    print(f"{name:20} n={n:4} | " + " | ".join(f"{k} {100 * c[k] / n:5.1f}%" for k in keys))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("-v", action="store_true", help="in từng tem đọc thật")
    ap.add_argument("--baseline", action="store_true", help="đo cách khớp cũ để so sánh")
    a = ap.parse_args()
    global BASELINE
    BASELINE = a.baseline
    M.PARTIAL_ID = not a.baseline
    sys.stdout.reconfigure(encoding="utf-8")
    idx = M.TicketIndex(DB)
    idx.ensure()
    con = sqlite3.connect(f"file:{Path(DB).as_posix()}?mode=ro", uri=True)

    real = []
    for truth, t, s, st, day in REAL_READS:
        res, alts = judge(idx, tem_values(t, s, st, day, date(2026, 10, 15)), truth)
        real.append(res)
        if a.v:
            print(f"  {truth:20} đọc {t!r:22} sn {s!r:16} trạm {st!r:12} ngày {day!r:11} -> {res} {f'({alts})' if alts else ''}")
    report("Đọc thật (AI)", real)

    sims = synth(con, a.n, a.seed)
    report("Mô phỏng", [judge(idx, tem_values(t, s, st, day, ref), truth)[0] for truth, t, s, st, day, ref in sims])


if __name__ == "__main__":
    main()
