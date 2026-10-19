"""Tra cứu ccts_tickets.db (chỉ đọc) và đối chiếu giá trị AI đọc từ tem với ticket.

Các trường so sánh (cột bảng <- trường DB):
    ticket_id            <- ccts_ticket_id
    sn                   <- charge_box_id
    date_onsite          <- onSiteCompleteTime (raw_json), chỉ lấy ngày
    original_phenomenon  <- error_desc (bỏ dòng ngày giờ / mã / số điện thoại)
    station_code         <- station_code
Ngoài ra: contractor suy ra từ người phụ trách; charge_box_model dùng để gợi ý Board (phía web).
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

# ---------- Chuẩn hóa mã ----------

# Ký tự viết tay dễ nhầm -> cùng một ký tự.
_CONFUSE = str.maketrans({"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "B": "8", "G": "6"})


def _alnum(s: str | None) -> str:
    return "".join(ch for ch in (s or "").upper() if ch.isascii() and ch.isalnum())


def norm_id(s: str | None) -> str:
    return "".join(ch for ch in (s or "").upper() if ch.isascii() and ch.isalnum()).translate(_CONFUSE)


def hamming_le(a: str, b: str, k: int) -> bool:
    d = 0
    for x, y in zip(a, b):
        if x != y:
            d += 1
            if d > k:
                return False
    return True


def lev_le1(a: str, b: str) -> bool:
    """Khoảng cách Levenshtein <= 1 với hai chuỗi lệch độ dài đúng 1."""
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def lcs_ratio(a: str, b: str) -> float:
    """Độ giống theo dãy ký tự chung dài nhất (sau chuẩn hóa ký tự dễ nhầm): 1 = giống hệt."""
    x, y = norm_id(a), norm_id(b)
    if not x or not y:
        return 0.0
    prev = [0] * (len(y) + 1)
    for cx in x:
        cur = [0]
        for j, cy in enumerate(y, 1):
            cur.append(prev[j - 1] + 1 if cx == cy else max(prev[j], cur[j - 1]))
        prev = cur
    return 2 * prev[-1] / (len(x) + len(y))


def lev(a: str, b: str, mx: int = 3) -> int:
    if abs(len(a) - len(b)) > mx:
        return mx + 1
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i]
        best = i
        for j in range(1, len(b) + 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1])))
            best = min(best, cur[j])
        if best > mx:
            return mx + 1
        prev = cur
    return prev[len(b)]


# ---------- So khớp theo từng ký tự ----------
# AI trả về, với mỗi vị trí của mã, chuỗi các ký tự có thể ở đó (khả năng cao nhất đứng đầu),
# vd. ticket "8MHEMQ0423EQ" -> ["8","M","H","E","M","QO","0O","4","2Z","3","E","QO0"]. "?" = không đọc được.

_EQUIV: dict[str, str] = defaultdict(str)
for _g in ("0OQD", "1IL7", "2Z", "5S", "6G", "8B", "UV", "MN", "4A"):
    for _c in _g:
        _EQUIV[_c] += _g


def char_sets(chars: list[str] | None, value: str = "") -> list[str | None] | None:
    """Danh sách ký tự có thể ở mỗi vị trí (chữ hoa, chỉ chữ/số); None = ký tự không đọc được.
    Bỏ vị trí là dấu chấm/gạch. Trả None nếu không dùng được (thiếu hoặc lệch độ dài so với value)."""
    if not chars:
        return None
    out: list[str | None] = []
    for c in chars:
        c = str(c or "").upper()
        if "?" in c or not c:
            out.append(None)
            continue
        alnum = "".join(ch for ch in c if ch.isascii() and ch.isalnum())
        if alnum:
            out.append(alnum)
    want = sum(1 for ch in (value or "").upper() if (ch.isascii() and ch.isalnum()) or ch == "?")
    return out if out and (not value or len(out) == want) else None


def char_cost(cands: str | None, ch: str) -> float:
    """0 = đúng ký tự AI chọn; 0.1 = nằm trong các khả năng AI đưa ra; 0.6 = ký tự dễ nhầm; 1 = khác hẳn."""
    if cands is None:
        return 0.5
    if ch == cands[0]:
        return 0
    if ch in cands:
        return 0.1
    if any(ch in _EQUIV[c] for c in cands):
        return 0.6
    return 1


def wdist(sets: list[str | None], target: str, mx: float = 3) -> float:
    """Khoảng cách Levenshtein có trọng số giữa mẫu ký tự của AI và mã trong DB."""
    t = _alnum(target)
    if abs(len(sets) - len(t)) > mx:
        return mx + 1
    prev = [float(j) for j in range(len(t) + 1)]
    for i in range(1, len(sets) + 1):
        cur = [float(i)]
        for j in range(1, len(t) + 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + char_cost(sets[i - 1], t[j - 1])))
        if min(cur) > mx:
            return mx + 1
        prev = cur
    return prev[len(t)]


def char_regex(sets: list[str | None]) -> re.Pattern:
    """Mẫu tìm nhanh trong DB: mỗi vị trí nhận các ký tự AI đưa ra + ký tự dễ nhầm với chúng."""
    parts = []
    for c in sets:
        if c is None:
            parts.append(".")
        else:
            pool = set(c) | {e for x in c for e in _EQUIV[x]}
            parts.append("[" + re.escape("".join(sorted(pool))) + "]")
    return re.compile("^" + "".join(parts) + "$")


def char_diff(value: str, sets: list[str | None] | None, target: str) -> list[dict]:
    """So từng ký tự tem với DB (cùng độ dài): ok | resolved (AI phân vân, DB chọn 1 trong các khả năng) | bad."""
    v = [ch for ch in (value or "").upper() if (ch.isascii() and ch.isalnum()) or ch == "?"]
    t = list(_alnum(target))
    if not v or len(v) != len(t):
        return []
    out = []
    for i, (a, b) in enumerate(zip(v, t)):
        cands = sets[i] if sets and i < len(sets) else None
        if a == b and (not cands or len(cands) == 1):
            state = "ok"
        elif a == b or (cands and b in cands) or a == "?":
            state = "resolved"
        else:
            state = "bad"
        out.append({"tem": a, "db": b, "alts": cands or "", "state": state})
    return out


# ---------- Ngày ----------
# Định dạng ngày ghi vào cột Date onsite: "mdy" (8/1/2026, như bot PCBA-CCVN), "iso" (2026-08-01), "dmy" (01/08/2026).
DATE_FORMATS = ("mdy", "iso", "dmy")


def fmt_day(d: date | None, fmt: str) -> str:
    if not d:
        return ""
    if fmt == "iso":
        return d.isoformat()
    if fmt == "dmy":
        return f"{d.day:02d}/{d.month:02d}/{d.year}"
    return f"{d.month}/{d.day}/{d.year}"


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y if y > 99 else 2000 + y, m, d)
    except ValueError:
        return None


def parse_day(s: str | None, fmt: str) -> date | None:
    """Ngày từ "2026-08-01 08:47:35" hoặc a/b/yyyy (hiểu theo fmt)."""
    s = (s or "").strip()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return _mk(int(m[1]), int(m[2]), int(m[3]))
    m = re.match(r"^(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{2}|\d{4})$", s)
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        return _mk(y, a, b) if fmt == "mdy" else _mk(y, b, a)
    return None


def tem_day(s: str | None) -> date | None:
    """Ngày viết trên tem luôn là ngày/tháng/năm."""
    return parse_day(s, "dmy")


TEM_DAY_WINDOW = 120  # ngày thay thế trên tem hợp lý: tối đa 120 ngày trước ngày chụp, 7 ngày sau


def tem_day_fixed(s: str | None, ref: date | None = None) -> date | None:
    """Ngày trên tem, sửa năm khi AI đọc sai năm (đã gặp: "3/1/2020", "8/11/2078") hoặc thiếu năm ("25/9"):
    giữ ngày/tháng, lấy năm của ngày chụp (hoặc năm trước) nếu ra một ngày hợp lý quanh ngày chụp."""
    ref = ref or date.today()
    s = (s or "").strip()
    d = tem_day(s)
    if not d:
        m = re.match(r"^(\d{1,2})\s*[/.\-]\s*(\d{1,2})$", s)  # thiếu năm
        d = _mk(ref.year, int(m[2]), int(m[1])) if m else None
        if not d:
            return None
    if -TEM_DAY_WINDOW <= (d - ref).days <= 7:
        return d
    for y in (ref.year, ref.year - 1):
        c = _mk(y, d.month, d.day)
        if c and -TEM_DAY_WINDOW <= (c - ref).days <= 7:
            return c
    return d


# ---------- Ticket ----------

KEEP = (
    "cctsTicketPk", "cctsTicketId", "thirdTicketId", "cctsTicketName", "cctsTicketStatus",
    "chargeBoxId", "chargeBoxModel", "stationCode", "address", "errorCode", "errorName",
    "errorDesc", "createTime", "occurrenceTime", "onSiteCompleteTime", "assistantName",
    "cctsTicketOwnerUserName",
)


def slim(j: dict) -> dict:
    s = lambda v: "" if v is None else str(v).strip()  # noqa: E731
    return {
        "pk": s(j.get("cctsTicketPk")),
        "id": s(j.get("cctsTicketId")),
        "thirdId": s(j.get("thirdTicketId")),
        "name": s(j.get("cctsTicketName")),
        "status": s(j.get("cctsTicketStatus")),
        "sn": s(j.get("chargeBoxId")),
        "model": s(j.get("chargeBoxModel")),
        "station": s(j.get("stationCode")),
        "address": s(j.get("address")),
        "errorCode": s(j.get("errorCode")),
        "errorName": s(j.get("errorName")),
        "errorDesc": s(j.get("errorDesc")),
        "createTime": s(j.get("createTime")),
        "onsiteTime": s(j.get("onSiteCompleteTime")),
        "people": "; ".join(x for x in (s(j.get("assistantName")), s(j.get("cctsTicketOwnerUserName"))) if x),
    }


class TicketIndex:
    """Chỉ mục trong RAM: mã ticket và SN đã chuẩn hóa -> khóa ticket. Tự nạp lại khi DB thay đổi."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.loaded_mtime = 0.0
        self.checked_at = 0.0
        self.count = 0
        self.by_id: dict[str, list[str]] = {}
        self.ids_by_len: dict[int, list[tuple[str, str]]] = {}
        self.by_sn: dict[str, list[str]] = {}
        self.sns_by_len: dict[int, list[str]] = {}
        self.raw_ids_by_len: dict[int, list[tuple[str, str]]] = {}
        self.raw_sns_by_len: dict[int, list[str]] = {}
        self.raw_sn_norm: dict[str, str] = {}
        self.by_station: dict[str, list[str]] = {}  # mã trạm -> ticket, mới nhất trước
        # Đuôi mã ticket: 3-4 ký tự cuối + SN/trạm + ngày gần như luôn chỉ ra đúng 1 ticket (99,4% trên DB).
        self.by_tail4: dict[str, list[str]] = {}
        self.by_tail3: dict[str, list[str]] = {}
        self.pk_station: dict[str, str] = {}
        # Mã ticket loại 12 ký tự tăng theo thời gian: 2 ký tự đầu đoán được từ ngày tạo.
        self.heads_by_day: dict[date, set[str]] = {}

    @property
    def ok(self) -> bool:
        return Path(self.db_path).exists()

    def connect(self) -> sqlite3.Connection:
        uri = "file:" + Path(self.db_path).as_posix() + "?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=10)

    def _mtime(self) -> float:
        p = Path(self.db_path)
        wal = p.with_name(p.name + "-wal")
        return max(p.stat().st_mtime, wal.stat().st_mtime if wal.exists() else 0)

    def ensure(self) -> None:
        if not self.ok:
            return
        now = time.time()
        if self.count and now - self.checked_at < 60:
            return
        with self.lock:
            self.checked_at = now
            mtime = self._mtime()
            if self.count and mtime == self.loaded_mtime:
                return
            t0 = time.time()
            by_id: dict[str, list[str]] = defaultdict(list)
            ids_by_len: dict[int, list[tuple[str, str]]] = defaultdict(list)
            by_sn: dict[str, list[str]] = defaultdict(list)
            raw_ids: dict[int, list[tuple[str, str]]] = defaultdict(list)
            raw_sns: dict[str, str] = {}
            by_station: dict[str, list[tuple[str, str]]] = defaultdict(list)
            by_tail4: dict[str, list[str]] = defaultdict(list)
            by_tail3: dict[str, list[str]] = defaultdict(list)
            pk_station: dict[str, str] = {}
            heads: dict[date, set[str]] = defaultdict(set)
            with self.connect() as con:
                rows = con.execute(
                    "SELECT ccts_ticket_pk, ccts_ticket_id, third_ticket_id, charge_box_id, station_code, create_time FROM tickets"
                ).fetchall()
            for pk, tid, third, sn, station, created in rows:
                n = norm_id(tid)
                if n:
                    by_id[n].append(pk)
                    ids_by_len[len(n)].append((n, pk))  # chỉ mã CCTS mới tìm gần đúng
                    raw = _alnum(tid)
                    raw_ids[len(raw)].append((raw, pk))
                    if len(n) >= 8:
                        by_tail4[n[-4:]].append(pk)
                        by_tail3[n[-3:]].append(pk)
                    if len(n) == 12 and created:
                        d = parse_day(created, "mdy")
                        if d:
                            heads[d].add(n[:2])
                n = norm_id(third)
                if n:
                    by_id[n].append(pk)
                n = norm_id(sn)
                if n:
                    by_sn[n].append(pk)
                    raw_sns.setdefault(_alnum(sn), n)
                n = norm_id(station)
                if n:
                    by_station[n].append((created or "", pk))
                    pk_station[pk] = n
            sns_by_len: dict[int, list[str]] = defaultdict(list)
            for n in by_sn:
                sns_by_len[len(n)].append(n)
            raw_sns_by_len: dict[int, list[str]] = defaultdict(list)
            for raw in raw_sns:
                raw_sns_by_len[len(raw)].append(raw)
            self.raw_ids_by_len, self.raw_sns_by_len, self.raw_sn_norm = dict(raw_ids), dict(raw_sns_by_len), raw_sns
            self.by_station = {k: [pk for _, pk in sorted(v, reverse=True)] for k, v in by_station.items()}
            self.by_id, self.ids_by_len = dict(by_id), dict(ids_by_len)
            self.by_sn, self.sns_by_len = dict(by_sn), dict(sns_by_len)
            self.by_tail4, self.by_tail3, self.pk_station = dict(by_tail4), dict(by_tail3), pk_station
            self.heads_by_day = dict(heads)
            self.count = len(rows)
            self.loaded_mtime = mtime
            print(f"[db] {self.count:,} ticket, {len(by_sn):,} SN — {time.time() - t0:.1f}s", flush=True)

    def _fuzzy_ids(self, q: str) -> list[str]:
        if len(q) < 6:
            return []
        out = [pk for n, pk in self.ids_by_len.get(len(q), ()) if hamming_le(n, q, 2)]
        for ln in (len(q) - 1, len(q) + 1):  # thiếu/thừa một ký tự
            out += [pk for n, pk in self.ids_by_len.get(ln, ()) if (n[:2] == q[:2] or n[-2:] == q[-2:]) and lev_le1(n, q)]
        return out

    def _fuzzy_sns(self, q: str) -> list[str]:
        if len(q) < 6:
            return []
        out: list[str] = []
        for n in self.sns_by_len.get(len(q), ()):
            if hamming_le(n, q, 1):
                out += self.by_sn[n]
        return out

    def heads_near(self, d: date | None, tol: int = 3) -> set[str]:
        """2 ký tự đầu của mã ticket (loại 12 ký tự) được tạo trong khoảng ngày d ± tol."""
        if not d:
            return set()
        out: set[str] = set()
        for k in range(-tol, tol + 1):
            out |= self.heads_by_day.get(d + timedelta(days=k), set())
        return out

    def _near_stations(self, q: str) -> list[str]:
        if len(q) < 5:
            return []
        # AI hay bỏ "C." / "B." (trạm đổi pin) ở đầu mã trạm (vd "HNO1029" thay vì "C.HNO1029")
        near = [k for k in ((q, "C" + q, "8" + q) if PARTIAL_ID else (q,)) if k in self.by_station]  # norm_id: "B" -> "8"
        for k in self.by_station:
            if k not in near and ((len(k) == len(q) and hamming_le(k, q, 1)) or (abs(len(k) - len(q)) == 1 and lev_le1(k, q))):
                near.append(k)
        return near[:6]

    def _char_ids(self, sets: list[str | None]) -> list[str]:
        """Ticket ID trong DB khớp mẫu ký tự (mọi vị trí nằm trong các khả năng AI đưa ra)."""
        rx = char_regex(sets)
        return [pk for raw, pk in self.raw_ids_by_len.get(len(sets), ()) if rx.match(raw)]

    def _char_sns(self, sets: list[str | None]) -> list[str]:
        rx = char_regex(sets)
        out: list[str] = []
        for raw in self.raw_sns_by_len.get(len(sets), ()):
            if rx.match(raw):
                out += self.by_sn[self.raw_sn_norm[raw]]
        return out

    def search_for(self, values: dict, chars: dict | None = None, limit: int = 80, fmt: str = "mdy") -> list[dict]:
        """Ticket ứng viên cho một dòng: theo Ticket ID (đúng, gần đúng, mẫu ký tự, đầu mã đoán từ ngày, đuôi mã),
        SN (đúng, gần đúng, mẫu ký tự); nếu chưa chắc thì thêm ticket của các trạm có mã gần giống."""
        if not self.ok:
            return []
        self.ensure()
        chars = chars or {}
        pks: dict[str, None] = {}
        q = norm_id(values.get("ticket_id"))
        day = parse_day(values.get("date_onsite"), fmt)
        st_near = self._near_stations(norm_id(values.get("station_code")))
        if q:
            hits = list(self.by_id.get(q) or [])
            if not hits:
                hits = self._fuzzy_ids(q)
                if chars.get("ticket_id"):
                    hits += self._char_ids(chars["ticket_id"])
                # Đầu mã đoán từ ngày: AI hay đọc sai 1-2 ký tự đầu ("87…" thay vì "8T…").
                if PARTIAL_ID and 11 <= len(q) <= 13:
                    for h in self.heads_near(day):
                        fixed = h + q[2:] if len(q) == 12 else h + q[-10:]
                        hits += self.by_id.get(fixed) or self._fuzzy_ids(fixed)
                # Đuôi mã: 4 ký tự cuối gần như chỉ ra 1 ticket; 3 ký tự cuối + trạm gần giống (khi SN đọc hỏng).
                if PARTIAL_ID and len(q) >= 4:
                    hits += (self.by_tail4.get(q[-4:]) or [])[:30]
                if PARTIAL_ID and len(q) >= 3 and st_near:
                    near = set(st_near)
                    hits += [pk for pk in self.by_tail3.get(q[-3:], ()) if self.pk_station.get(pk) in near]
            pks.update(dict.fromkeys(hits))
        q = norm_id(values.get("sn"))
        if q:
            hits = list(self.by_sn.get(q) or [])
            if not hits:
                hits = self._fuzzy_sns(q)
                if chars.get("sn"):
                    hits += self._char_sns(chars["sn"])
            pks.update(dict.fromkeys(hits))
        strong = bool(pks) and any(norm_id(values.get(k)) in idx for k, idx in (("ticket_id", self.by_id), ("sn", self.by_sn)))
        if not strong:
            # Mã ticket/SN đọc chưa ra: thêm ticket gần đây của các trạm có mã giống (lệch tối đa 1 ký tự),
            # rồi để phần chấm điểm so từng phần mã ticket/SN.
            for st in st_near:
                pks.update(dict.fromkeys(self.by_station[st][:40]))
        keys = list(pks)[: max(1, min(limit, 500))]
        return self._load(keys) if keys else []

    def _load(self, keys: list[str]) -> list[dict]:
        out = []
        with self.connect() as con:
            for i in range(0, len(keys), 400):
                chunk = keys[i : i + 400]
                sql = f"SELECT raw_json FROM tickets WHERE ccts_ticket_pk IN ({','.join('?' * len(chunk))})"
                for (raw,) in con.execute(sql, chunk):
                    try:
                        out.append(slim(json.loads(raw)))
                    except ValueError:
                        continue
        return out

    def search(self, ticket_id: str, sn: str, limit: int = 60) -> list[dict]:
        if not self.ok:
            return []
        self.ensure()
        pks: dict[str, None] = {}  # giữ thứ tự, bỏ trùng
        q = norm_id(ticket_id)
        if q:
            for pk in self.by_id.get(q) or self._fuzzy_ids(q):
                pks[pk] = None
        q = norm_id(sn)
        if q:
            for pk in self.by_sn.get(q) or self._fuzzy_sns(q):
                pks[pk] = None
        keys = list(pks)[: max(1, min(limit, 500))]
        return self._load(keys) if keys else []

    def get(self, pk: str) -> dict | None:
        if not self.ok or not pk:
            return None
        found = self._load([pk])
        return found[0] if found else None


# ---------- Chấm điểm ----------

DAY_TOL = 1  # ngày trên tem lệch ngày onsite tối đa 1 ngày vẫn coi là khớp
# Khớp theo một phần mã ticket (đuôi mã, đầu mã đoán từ ngày) + mã trạm thiếu "C."/"B.". Tắt = cách khớp cũ.
PARTIAL_ID = True


def id_sim(a: str, b: str, pts: tuple[int, int, int]) -> int:
    x, y = norm_id(a), norm_id(b)
    if not x or not y:
        return 0
    if x == y:
        return pts[0]
    if len(x) < 6:
        return 0
    d = lev(x, y, 2)
    return pts[1] if d == 1 else pts[2] if d == 2 else 0


def char_sim(sets: list[str | None], target: str, pts: tuple[int, int, int]) -> int:
    if not target:
        return 0
    d = wdist(sets, target, 3)
    # Mọi ký tự nằm trong các khả năng AI đưa ra (tối đa ~6 chỗ phân vân) vẫn tính là khớp hoàn toàn.
    return pts[0] if d <= 0.6 else pts[1] if d <= 1.3 else pts[2] if d <= 2.3 else 0


def score(v: dict, t: dict, fmt: str, chars: dict | None = None) -> dict:
    chars = chars or {}

    def sim(key: str, target: str, pts: tuple[int, int, int]) -> int:
        plain = id_sim(v.get(key, ""), target, pts)
        sets = chars.get(key)
        return max(plain, char_sim(sets, target, pts)) if sets else plain

    def soft(key: str, target: str, hi: int, lo: int) -> int:
        """Đọc sai nhiều ký tự nhưng phần lớn dãy ký tự vẫn trùng (vd. SRV7G9DMGXS ~ 8RW7G9DG1GXS)."""
        r = lcs_ratio(v.get(key, ""), target)
        return hi if r >= 0.75 else lo if r >= 0.62 else 0

    id_pts = max(sim("ticket_id", t["id"], (100, 70, 40)), id_sim(v.get("ticket_id", ""), t["thirdId"], (100, 70, 40)))
    id_pts = id_pts or soft("ticket_id", t["id"], 40, 25)
    sn_pts = sim("sn", t["sn"], (50, 30, 0)) or soft("sn", t["sn"], 30, 15)
    # AI hay bỏ "C." ở đầu mã trạm: so cả với phần sau dấu chấm.
    st_pts = max(sim("station_code", t["station"], (30, 20, 0)),
                 sim("station_code", t["station"].split(".", 1)[-1], (30, 20, 0)) if PARTIAL_ID else 0)
    date_pts = 0
    d = parse_day(v.get("date_onsite"), fmt)
    if d:
        on = parse_day(t["onsiteTime"], fmt)
        cr = parse_day(t["createTime"], fmt)
        if on and abs((on - d).days) <= DAY_TOL:
            date_pts = 25
        elif cr and -1 <= (d - cr).days <= 30:
            date_pts = 8  # ngày thay nằm sau ngày tạo ticket
    # Đuôi mã chỉ phân biệt được khi đi cùng ngày onsite (±1) VÀ SN hoặc trạm (đuôi + ngày một mình: 22,7%).
    id_pts = max(id_pts, tail_head_pts(v.get("ticket_id", ""), t["id"], date_pts > 0,
                                       date_pts >= 25 and (sn_pts >= 30 or st_pts >= 20)))
    return {"total": id_pts + sn_pts + st_pts + date_pts, "id": id_pts, "sn": sn_pts, "station": st_pts, "date": date_pts}


def tail_head_pts(read: str, target: str, date_ok: bool, tail_ok: bool = True) -> int:
    """Điểm mã ticket khi AI đọc sai một phần (đo trên DB: đuôi 3-4 ký tự + SN/trạm + ngày gần như duy nhất).
    - đầu mã đoán từ ngày: ngày tem khớp ticket -> 2 ký tự đầu lấy theo ticket, so phần còn lại;
    - đuôi mã: 4 ký tự cuối trùng 60, 3 ký tự cuối trùng 40.
    Chỉ tính khi ngày tem khớp ticket và độ dài mã đọc gần bằng mã thật: đuôi một mình (không ngày) trùng ngẫu nhiên
    rất nhiều, nhất là mã số 19 chữ số cũ (đã gặp: AI đọc "…0240" khớp nhầm mã 1027621270214410240 của 1 năm trước)."""
    x, y = norm_id(read), norm_id(target)
    if not PARTIAL_ID or not date_ok or len(x) < 4 or len(y) < 8 or abs(len(x) - len(y)) > 1:
        return 0
    head = 0
    if len(y) == 12 and 11 <= len(x) <= 13:
        head = id_sim(y[:2] + (x[2:] if len(x) == 12 else x[-10:]), y, (85, 65, 40))
    tail = (60 if x[-4:] == y[-4:] else 40 if x[-3:] == y[-3:] else 0) if tail_ok else 0
    return max(head, tail)


def rank(values: dict, tickets: list[dict], fmt: str, limit: int | None = 8, chars: dict | None = None) -> list[dict]:
    out = []
    for t in tickets:
        s = score(values, t, fmt, chars)
        if s["id"] > 0 or s["sn"] > 0 or (s["station"] > 0 and s["date"] > 0):
            out.append({"ticket": t, "score": s["total"], "parts": s})
    out.sort(key=lambda r: r["ticket"]["createTime"] or "", reverse=True)  # bằng điểm: ticket mới hơn lên trước
    out.sort(key=lambda r: r["score"], reverse=True)
    return out if limit is None else out[:limit]


CONTRACTORS = ("EC", "ES", "ITS", "HTE", "SE HN", "SE HCM")


def infer_contractor(t: dict) -> str:
    """Nhà thầu suy ra từ tên người phụ trách (vd. "ES_Engineer 23; Esmanager" -> ES)."""
    names = [n for n in re.split(r";\s*", t.get("people") or "") if n]
    found = set()
    for c in CONTRACTORS:
        rx = re.compile("^" + re.sub(r"\s+", "[ _]?", c) + r"(?:[_\s\d]|manager|$)", re.I)
        if any(rx.search(n) for n in names):
            found.add(c)
    return found.pop() if len(found) == 1 else ""


def clean_desc(t: dict) -> str:
    """error_desc bỏ các dòng chỉ là ngày giờ, mã (vd. CC_22KW), số điện thoại, ExpectedDeliveryDate."""
    lines = []
    for ln in (t.get("errorDesc") or "").splitlines():
        ln = ln.strip()
        if not ln or re.match(r"^\d{4}-\d{2}-\d{2}", ln) or re.fullmatch(r"[A-Z0-9_.\-]+", ln) \
                or re.match(r"^ExpectedDeliveryDate", ln, re.I) or re.fullmatch(r"[\d\s+]+", ln):
            continue
        lines.append(ln)
    text = " ".join(lines).strip()
    return text or " ".join(x for x in (t.get("errorCode"), t.get("errorName")) if x).strip()


def ticket_values(t: dict, fmt: str) -> dict:
    return {
        "ticket_id": t["id"],
        "sn": t["sn"],
        "date_onsite": fmt_day(parse_day(t["onsiteTime"], fmt), fmt),
        "original_phenomenon": clean_desc(t),
        "station_code": t["station"],
        "contractor": infer_contractor(t),
    }


# Cột đối chiếu: ticket là nguồn đúng, ghi đè giá trị AI đọc (trừ ô người dùng đã gõ tay).
COMPARE_KEYS = ("ticket_id", "sn", "date_onsite", "original_phenomenon", "station_code")
# Cột chỉ điền khi trống.
FILL_KEYS = ("contractor",)


def decide(values: dict, tickets: list[dict], fmt: str, chars: dict | None = None) -> dict:
    """-> {status: matched|ambiguous|none, best, alts, apply, ranked}"""
    ranked = rank(values, tickets, fmt, None, chars)
    if not ranked or ranked[0]["score"] < 50:
        return {"status": "none", "best": None, "alts": len(ranked), "apply": {}, "ranked": ranked}
    best = ranked[0]
    # Nhiều ticket điểm gần bằng nhau và tem không có ticket ID khớp -> chưa chắc ticket nào.
    ties = [r for r in ranked if r["score"] >= best["score"] - 10]
    p = best["parts"]
    # Chỉ tự chọn khi mã ticket gần đúng, hoặc có SN/trạm khớp: mã giống lờ mờ + ngày thì chưa đủ.
    evidence = p["id"] >= 70 or p["sn"] > 0 or p["station"] > 0 or not PARTIAL_ID
    sure = evidence and (p["id"] >= 70 or len(ties) == 1)
    group = [best] if sure else ties
    apply = {}
    for key in COMPARE_KEYS + FILL_KEYS:
        vals = {ticket_values(r["ticket"], fmt)[key] for r in group}
        if len(vals) == 1:
            v = vals.pop()
            if v:
                apply[key] = v
    return {"status": "matched" if sure else "ambiguous", "best": best if sure else None, "alts": len(ties), "apply": apply, "ranked": ranked}


def same_value(key: str, a: str, b: str, fmt: str) -> bool:
    if key == "date_onsite":
        da, db = parse_day(a, fmt), parse_day(b, fmt)
        return bool(da and db and da == db)
    if key in ("ticket_id", "sn", "station_code"):
        return (a or "").strip().upper() == (b or "").strip().upper()
    return " ".join((a or "").split()).lower() == " ".join((b or "").split()).lower()
