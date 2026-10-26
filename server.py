"""Server OCR tem hàng lỗi: một bảng dữ liệu dùng chung cho máy tính và điện thoại.

    python server.py                 # mở cho cả mạng LAN (điện thoại cùng Wi-Fi), cổng 8000
    python server.py --local         # chỉ máy này
    python server.py --db "D:/.../ccts_tickets.db" --port 8000

Server giữ toàn bộ bảng + ảnh trong thư mục data/, gọi AI (Gemini/Ollama) để đọc tem,
đối chiếu với ccts_tickets.db (mở chỉ đọc), rồi đẩy mọi thay đổi tới tất cả thiết bị
đang mở web (Server-Sent Events). Chỉ dùng thư viện chuẩn của Python.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import posixpath
import queue
import re
import socket
import sys
import threading
import time
import uuid
import zipfile
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import matching as M
import ocr
import ccts_login as CL
import dbsync as DS
import pcba_api as PB
import telegram_bot as TG

DEFAULT_DB = r"D:\00_WORKSPACE\01_Python\04_Takedata_CCVN\ccts_tickets.db"
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
IMAGES = DATA / "images"
STATIC = ("/index.html", "/css/", "/js/", "/favicon")

# Cột cố định (trùng js/columns.js). Cột do người dùng thêm có id bắt đầu bằng "c".
FIXED_KEYS = (
    "board", "contractor", "test_tool_result", "failure_cause", "disposition", "after_repair_result",
    "charging_station_test_status", "ticket_id", "sn", "date_onsite", "original_phenomenon", "station_code",
    "failure_verification", "board_sn", "sn_missing_reason",
)
# Ảnh kèm theo cột: kind -> (trường lưu danh sách ảnh trên dòng, tên file khi tải .zip)
PHOTO_KINDS = {"after": ("photos", "sau_sua"), "verify": ("vphotos", "xac_minh")}
MATCH_KEYS = ("ticket_id", "sn", "station_code", "date_onsite")  # sửa tay các ô này -> đối chiếu lại
ROW_DEFAULTS = {"after_repair_result": "PASS", "charging_station_test_status": "Pending"}  # giá trị dòng mới
# Không tự điền (để trống, người dùng nhập nếu cần). Giá trị tem/DB vẫn xem được trong ngăn chi tiết.
NO_AUTOFILL = ("original_phenomenon",)

DEFAULT_CONFIG = {
    "provider": "gemini",
    "geminiKey": "",
    "geminiModel": "gemini-flash-latest",
    "ollamaUrl": "http://localhost:11434",
    "ollamaModel": "qwen3-vl:4b",
    "parallel": 2,
    "dateFormat": "mdy",
    "speed": "fast",  # fast: tắt "suy nghĩ" của Gemini khi đọc tem | careful: để model tự suy nghĩ (chậm hơn)
    "aiConfirm": True,  # chưa chắc ticket nào -> gửi ảnh + ticket ứng viên cho AI chọn
    "geminiConfirmModel": "gemini-flash-latest",  # model cho bước xác nhận (nên mạnh hơn model đọc)
    # full: đọc cả 7 ô | codes: Ticket/SN/Mã trạm/Ngày (bỏ Model, Mã lỗi, Mô tả) |
    # quick: Mã trạm/Model/SN/Ngày, Ticket lấy từ DB
    "readMode": "full",
    "aiMaxSide": 1024,  # thu nhỏ ảnh gửi AI (px cạnh dài, 0 = giữ nguyên): nhanh hơn ~40% với Ollama trên CPU
    "tgCdp": TG.DEFAULT_CDP,  # cổng debug của Edge mở Telegram Web (profile riêng cho bot)
    "tgTimeout": 45,  # giây chờ bot trả lời mỗi bước
    # Đồng bộ ccts_tickets.db từ web CNPowercore (⚙️ → Đồng bộ DB). Cookie/token chỉ lưu ở data/config.json.
    "cctsSso": "",
    "cctsToken": "",
    # Tự đăng nhập lấy cookie/token mỗi lần đồng bộ (mật khẩu mã hoá DPAPI, xem ccts_login.py).
    "cctsUser": "",
    "cctsPass": "",
    # Nhập bo mạch: "pcba" = hệ thống PCBA Team (web, mặc định từ 26/10/2026), "telegram" = bot cũ.
    "importTarget": "pcba",
    "pcbaUser": "",
    "pcbaPass": "",  # mã hoá DPAPI như cctsPass
    "pcbaBase": PB.BASE,
    "cctsToolDir": DS.DEFAULT_TOOL_DIR,
    "cctsPageSize": 100,
    "cctsDelay": 3,
}
SECRET_KEYS = ("geminiKey", "cctsSso", "cctsToken", "cctsPass", "pcbaPass")  # không bao giờ gửi về trình duyệt
TG_ACTIVE = ("queued", "running")

uid = lambda p: p + uuid.uuid4().hex[:10]  # noqa: E731
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


# ---------- Đẩy sự kiện tới các thiết bị ----------

class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients: dict[str, tuple[queue.Queue, str]] = {}

    def add(self, label: str) -> tuple[str, queue.Queue]:
        cid, q = uid("d"), queue.Queue()
        with self.lock:
            self.clients[cid] = (q, label)
        self.presence()
        return cid, q

    def remove(self, cid: str) -> None:
        with self.lock:
            self.clients.pop(cid, None)
        self.presence()

    def send(self, event: dict) -> None:
        msg = json.dumps(event, ensure_ascii=False)
        with self.lock:
            for q, _ in self.clients.values():
                q.put(msg)

    def presence(self) -> None:
        with self.lock:
            devices = [label for _, label in self.clients.values()]
        self.send({"type": "presence", "devices": devices})


# ---------- Dữ liệu ----------

class Store:
    """Bảng dùng chung. Mọi thay đổi đi qua đây: ghi file data/state.json rồi phát sự kiện."""

    def __init__(self, hub: Hub, index: M.TicketIndex):
        self.hub = hub
        self.index = index
        self.lock = threading.RLock()
        DATA.mkdir(exist_ok=True)
        IMAGES.mkdir(exist_ok=True)
        self.state = {"rows": [], "cols": [], "colDefaults": {}}
        self.config = dict(DEFAULT_CONFIG)
        try:
            self.state.update(json.loads((DATA / "state.json").read_text("utf-8")))
        except (OSError, ValueError):
            pass
        try:
            self.config.update(json.loads((DATA / "config.json").read_text("utf-8")))
        except (OSError, ValueError):
            pass

    # --- lưu / phát ---
    def save(self) -> None:
        atomic_write(DATA / "state.json", json.dumps(self.state, ensure_ascii=False).encode("utf-8"))

    def save_config(self) -> None:
        atomic_write(DATA / "config.json", json.dumps(self.config, ensure_ascii=False, indent=2).encode("utf-8"))

    def emit_row(self, r: dict) -> None:
        self.save()
        self.hub.send({"type": "row", "row": r})

    def emit_meta(self) -> None:
        self.save()
        self.hub.send({"type": "meta", "cols": self.state["cols"], "colDefaults": self.state["colDefaults"]})

    def snapshot(self) -> dict:
        with self.lock:
            return {"type": "state", **self.state, "dateFormat": self.config["dateFormat"]}

    # --- tiện ích ---
    @property
    def rows(self) -> list[dict]:
        return self.state["rows"]

    def find(self, rid: str) -> dict | None:
        return next((r for r in self.rows if r["id"] == rid), None)

    def need(self, rid: str) -> dict:
        r = self.find(rid)
        if not r:
            raise KeyError("Không tìm thấy dòng (có thể đã bị xóa trên thiết bị khác).")
        return r

    def allowed_keys(self) -> set[str]:
        return set(FIXED_KEYS) | {c["id"] for c in self.state["cols"]}

    def new_row(self, **extra) -> dict:
        values = dict(ROW_DEFAULTS)
        values.update({c["id"]: c["def"] for c in self.state["cols"] if c.get("def")})
        values.update({k: v for k, v in self.state["colDefaults"].items() if v})  # 📌 của người dùng thắng
        r = {
            "id": uid("r"), "createdAt": int(time.time() * 1000), "image": None, "photos": [], "vphotos": [],
            "status": "manual", "error": "", "warn": "", "checked": False, "chars": {}, "diff": {},
            "values": values, "flags": {}, "edited": {}, "tem": {}, "src": {},
            "match": None, "ticket": None, "dbv": {}, "tg": None,
        }
        r.update(extra)
        return r

    # ---------- Đối chiếu ticket ----------
    def _revert(self, r: dict, keep_manual: bool = False) -> None:
        """Gỡ các giá trị lấy từ ticket, trả về như tem."""
        for k, src in (r.get("src") or {}).items():
            if r["edited"].get(k):
                continue
            if src == "fixed":
                r["values"][k] = r["tem"].get(k, r["values"].get(k, ""))
            elif src == "ticket":
                r["values"][k] = ""
        r["src"] = {}
        r["dbv"] = {}
        r["diff"] = {}
        if not (keep_manual and (r.get("match") or {}).get("how") == "manual"):
            r["match"] = None
            r["ticket"] = None

    def _apply(self, r: dict, apply: dict) -> None:
        fmt = self.config["dateFormat"]
        r["dbv"] = dict(apply)
        chars = r.get("chars") or {}
        r["diff"] = {k: d for k in ("ticket_id", "sn", "station_code")
                     if (d := M.char_diff(r["tem"].get(k, ""), chars.get(k), apply.get(k, "")))}
        for k, v in apply.items():
            if k in NO_AUTOFILL:
                continue  # vẫn hiện ở cột DB trong ngăn chi tiết (dbv), không ghi vào ô
            cur = r["values"].get(k, "")
            if M.same_value(k, cur, v, fmt):
                r["values"][k] = v
                r["src"][k] = "same"
            elif r["edited"].get(k) and cur:
                r["src"][k] = "mismatch"  # người dùng đã gõ tay: không ghi đè, chỉ báo khác
                continue
            elif not cur:
                r["values"][k] = v
                r["src"][k] = "ticket"
            elif k in M.COMPARE_KEYS:
                r["values"][k] = v
                r["src"][k] = "fixed"
            else:
                continue  # contractor đã có giá trị: giữ nguyên
            r["flags"].pop(k, None)

    def _match_input(self, r: dict) -> dict:
        """Giá trị gốc (tem / gõ tay) để so khớp, không tính ô vừa điền từ ticket."""
        v = dict(r["values"])
        for k, src in (r.get("src") or {}).items():
            if src == "fixed":
                v[k] = r["tem"].get(k, v.get(k, ""))
            elif src == "ticket":
                v.pop(k, None)
        return v

    def _match_chars(self, r: dict) -> dict:
        """Ký tự AI đọc cho các ô mã chưa bị người dùng sửa tay."""
        return {k: v for k, v in (r.get("chars") or {}).items() if v and not r["edited"].get(k)}

    def _ai_confirm(self, r_snapshot: dict, ranked: list[dict]) -> dict | None:
        """Chưa chắc ticket nào: gửi ảnh tem + tối đa 10 ticket ứng viên cho AI chọn. Kết quả nhớ theo ảnh + ứng viên."""
        img = r_snapshot.get("image")
        cfg = dict(self.config)
        if not img or not ranked or not cfg.get("aiConfirm", True) or ocr.ready(cfg):
            return None
        top = [x["ticket"] for x in ranked[:10]]
        key = img + "|" + ",".join(t["pk"] for t in top)
        cached = r_snapshot.get("confirm") or {}
        if cached.get("key") == key:
            return cached
        fmt = cfg["dateFormat"]
        cands = [{"pk": t["pk"], "id": t["id"], "sn": t["sn"], "station": t["station"],
                  "date": M.fmt_day(M.parse_day(t["onsiteTime"], fmt), "dmy")} for t in top]
        try:
            jpeg = (IMAGES / f"{img}.jpg").read_bytes()
            t0 = time.time()
            res = ocr.confirm(cfg, jpeg, cands)
            res["ms"] = int((time.time() - t0) * 1000)
        except (ocr.ExtractError, OSError) as e:
            res = {"pk": "", "confidence": "error", "reason": str(e)}
        res["key"] = key
        return res

    def match(self, rid: str) -> None:
        """Đối chiếu một dòng với DB. Tra cứu DB ngoài khóa, ghi kết quả trong khóa."""
        fmt = self.config["dateFormat"]
        with self.lock:
            r = self.find(rid)
            if not r or r["status"] in ("queued", "processing"):
                return
            m = r.get("match") or {}
            manual_pk = m.get("pk") if m.get("how") == "manual" else None
            if m.get("how") == "manual" and not manual_pk:
                return  # người dùng đã bỏ khớp
            inp = self._match_input(r)
            chars = self._match_chars(r)
            snap = {"image": r.get("image"), "confirm": r.get("confirm")}
        if manual_pk:
            t = self.index.get(manual_pk)
            with self.lock:
                r = self.find(rid)
                if not r or not t:
                    return
                self._revert(r, keep_manual=True)
                r["match"]["id"] = t["id"]
                r["ticket"] = t
                self._apply(r, {k: v for k, v in M.ticket_values(t, fmt).items() if v})
                self.emit_row(r)
            return
        cands = self.index.search_for(inp, chars, fmt=fmt)
        d = M.decide(inp, cands, fmt, chars)
        how = "auto"
        conf = None
        if d["status"] != "matched" and d["ranked"]:
            conf = self._ai_confirm(snap, d["ranked"])
            pick = next((x for x in d["ranked"] if conf and x["ticket"]["pk"] == conf["pk"]), None)
            if pick and conf["confidence"] in ("high", "medium"):
                d = {**d, "status": "matched", "best": pick, "alts": 1,
                     "apply": {k: v for k, v in M.ticket_values(pick["ticket"], fmt).items() if v}}
                how = "ai"
        with self.lock:
            r = self.find(rid)
            if not r or (r.get("match") or {}).get("how") == "manual":
                return
            self._revert(r)
            best = d["best"]
            r["match"] = {"status": d["status"], "pk": best["ticket"]["pk"] if best else "",
                          "id": best["ticket"]["id"] if best else "", "score": best["score"] if best else 0,
                          "alts": d["alts"], "how": how}
            if conf:
                r["confirm"] = conf
            r["ticket"] = best["ticket"] if best else None
            self._apply(r, d["apply"])
            self.emit_row(r)

    def choose(self, rid: str, pk: str | None, clear: bool = False) -> None:
        """pk: chọn ticket; "" = bỏ khớp (clear: xoá trống các ô Ticket/SN/ngày/mô tả/trạm để nhập tay);
        None = về tự động."""
        with self.lock:
            r = self.need(rid)
            self._revert(r)
            if pk == "" and clear:
                for k in M.COMPARE_KEYS:  # giá trị AI đọc sai / DB khớp nhầm: bỏ hết, người dùng gõ lại
                    r["values"][k] = ""
                    r["edited"].pop(k, None)
                    r["flags"].pop(k, None)
                r["diff"] = {}
                if r["status"] in ("queued", "processing", "error"):
                    # Chuyển sang nhập tay: huỷ lượt đọc AI của dòng này (kết quả đang đọc dở sẽ bị bỏ qua).
                    r["status"], r["error"] = "manual", ""
            if pk is None:
                r["match"] = None
            elif pk == "":
                r["match"] = {"status": "none", "pk": "", "id": "", "score": 0, "alts": 0, "how": "manual"}
            else:
                r["match"] = {"status": "matched", "pk": pk, "id": "", "score": 0, "alts": 1, "how": "manual"}
            self.emit_row(r)
        self.match(rid)

    def candidates(self, rid: str) -> list[dict]:
        fmt = self.config["dateFormat"]
        with self.lock:
            r = self.need(rid)
            inp = self._match_input(r)
            chars = self._match_chars(r)
        ranked = M.rank(inp, self.index.search_for(inp, chars, fmt=fmt), fmt, 10, chars)
        return [{"ticket": x["ticket"], "score": x["score"], "parts": x["parts"],
                 "values": M.ticket_values(x["ticket"], fmt)} for x in ranked]

    # ---------- Sửa dữ liệu ----------
    def patch(self, rid: str, body: dict) -> None:
        keys = self.allowed_keys()
        rematch = snlook = False
        with self.lock:
            r = self.need(rid)
            for k, v in (body.get("values") or {}).items():
                if k not in keys:
                    continue
                v = str(v)[:2000]
                if k == "board_sn" and v != r["values"].get(k, ""):
                    r["qr"], r["snCheck"] = {"source": "MANUAL"}, None  # gõ tay: không còn là SN quét QR
                    snlook = bool(v.strip())
                r["values"][k] = v
                r["edited"][k] = True
                r["flags"].pop(k, None)
                r["src"].pop(k, None)
                rematch = rematch or k in MATCH_KEYS
            if "checked" in body:
                r["checked"] = bool(body["checked"])
                if r["checked"]:
                    r["flags"] = {}
            self.emit_row(r)
        if rematch and body.get("commit") and (r.get("match") or {}).get("how") != "manual":
            self.match(rid)
        if snlook and body.get("commit"):
            threading.Thread(target=self.sn_check, args=(rid,), daemon=True).start()

    # ---------- Check SN bo mạch (hệ thống PCBA) ----------
    def set_qr(self, rid: str, raw: str) -> None:
        """SN đọc từ mã QR trên bo mạch (quét bằng camera trên web)."""
        sn = PB.norm_sn(raw)
        if not sn:
            raise ValueError("Mã QR trống.")
        with self.lock:
            r = self.need(rid)
            r["values"]["board_sn"] = sn
            r["values"]["sn_missing_reason"] = ""
            r["edited"]["board_sn"] = True
            r["qr"] = {"source": "QR", "raw": str(raw)[:500]}
            r["snCheck"] = None
            self.emit_row(r)
        threading.Thread(target=self.sn_check, args=(rid,), daemon=True).start()

    def sn_check(self, rid: str) -> None:
        """Tra SN bo mạch trên hệ thống PCBA: đã có bo này chưa, model gì, có phiếu đang mở không."""
        with self.lock:
            r = self.find(rid)
            sn = ((r or {}).get("values") or {}).get("board_sn", "").strip()
            if not r:
                return
            if not sn:
                r["snCheck"] = None
                self.emit_row(r)
                return
            r["snCheck"] = {"status": "checking", "sn": sn}
            self.emit_row(r)
        try:
            info = {"status": "ok", **PB.lookup(pcba.client(), PB.norm_sn(sn))}
        except (PB.PcbaError, CL.LoginError) as e:
            info = {"status": "error", "sn": sn, "error": str(e)}
        with self.lock:
            r = self.find(rid)
            if not r or r["values"].get("board_sn", "").strip() != sn:
                return  # SN đã đổi trong lúc kiểm tra
            r["snCheck"] = {**info, "at": int(time.time() * 1000)}  # web tự điền Board theo model (nếu ô Board trống)
            self.emit_row(r)

    def patch_many(self, changes: list[dict]) -> int:
        """Dán / điền / xoá nhiều ô cùng lúc (kiểu Excel): [{id, values}] -> ghi file một lần, rồi đối chiếu lại
        DB cho các dòng đổi Ticket/SN/trạm/ngày (trừ dòng nhập tay)."""
        keys = self.allowed_keys()
        rematch, n = [], 0
        with self.lock:
            for ch in changes[:2000]:
                r = self.find(str(ch.get("id") or ""))
                if not r:
                    continue
                for k, v in (ch.get("values") or {}).items():
                    if k not in keys:
                        continue
                    if k == "board_sn" and str(v) != r["values"].get(k, ""):
                        r["qr"], r["snCheck"] = {"source": "MANUAL"}, None
                    r["values"][k] = str(v)[:2000]
                    r["edited"][k] = True
                    r["flags"].pop(k, None)
                    r["src"].pop(k, None)
                    n += 1
                    if k in MATCH_KEYS and (r.get("match") or {}).get("how") != "manual" and r["id"] not in rematch:
                        rematch.append(r["id"])
            self.save()
            for ch in changes[:2000]:
                r = self.find(str(ch.get("id") or ""))
                if r:
                    self.hub.send({"type": "row", "row": r})
        if rematch:
            threading.Thread(target=lambda: [self.match(i) for i in rematch], daemon=True).start()
        return n

    def use_value(self, rid: str, key: str, source: str) -> None:
        """Lấy giá trị một ô từ tem hoặc từ DB."""
        with self.lock:
            r = self.need(rid)
            v = (r["tem"] if source == "tem" else r["dbv"]).get(key)
            if v is None:
                return
            r["values"][key] = v
            r["flags"].pop(key, None)
            db = r["dbv"].get(key, "")
            same = M.same_value(key, r["tem"].get(key, ""), db, self.config["dateFormat"])
            if source == "tem":
                r["edited"][key] = True  # coi như người dùng nhập: không bị ticket ghi đè
                state = "same" if same else "mismatch" if db else ""
            else:
                r["edited"].pop(key, None)
                state = "same" if same else "fixed" if r["tem"].get(key) else "ticket"
            if state:
                r["src"][key] = state
            else:
                r["src"].pop(key, None)
            self.emit_row(r)

    def fill(self, key: str, value: str, mode: str, set_default: bool) -> int:
        if key not in FIXED_KEYS:
            raise ValueError("Cột không hợp lệ")
        n = 0
        with self.lock:
            if mode in ("empty", "all"):
                for r in self.rows:
                    if mode == "empty" and r["values"].get(key):
                        continue
                    r["values"][key] = value
                    r["edited"][key] = True
                    r["flags"].pop(key, None)
                    n += 1
            if set_default and value:
                self.state["colDefaults"][key] = value
            else:
                self.state["colDefaults"].pop(key, None)
            self.save()
            self.hub.send(self.snapshot())
        return n

    def set_cols(self, cols: list[dict], fill_id: str | None) -> None:
        clean = []
        for c in cols[:50]:
            cid = str(c.get("id") or "")
            if not re.fullmatch(r"c[A-Za-z0-9]{1,40}", cid):
                continue
            clean.append({"id": cid, "label": str(c.get("label") or "")[:60], "def": str(c.get("def") or "")[:200]})
        with self.lock:
            ids = {c["id"] for c in clean}
            for r in self.rows:
                for k in [k for k in r["values"] if k.startswith("c") and k not in FIXED_KEYS and k not in ids]:
                    del r["values"][k]
                if fill_id:
                    col = next((c for c in clean if c["id"] == fill_id), None)
                    if col and col["def"] and not r["values"].get(fill_id):
                        r["values"][fill_id] = col["def"]
            self.state["cols"] = clean
            self.save()
            self.hub.send(self.snapshot())

    def delete_row(self, rid: str) -> None:
        with self.lock:
            r = self.need(rid)
            if (r.get("tg") or {}).get("status") == "running":
                raise ValueError("Dòng đang được nhập lên Telegram — chờ xong rồi xóa.")
            self.rows.remove(r)
            self.save()
            self.hub.send({"type": "delete", "id": rid})
        for img in [r.get("image"), *(i for f, _ in PHOTO_KINDS.values() for i in r.get(f, []))]:
            if img:
                (IMAGES / f"{img}.jpg").unlink(missing_ok=True)

    def clear(self) -> None:
        with self.lock:
            if any((r.get("tg") or {}).get("status") == "running" for r in self.rows):
                raise ValueError("Đang nhập một dòng lên Telegram — chờ xong rồi xóa bảng.")
            self.state["rows"] = []
            self.save()
            self.hub.send(self.snapshot())
        for p in IMAGES.glob("*.jpg"):
            p.unlink(missing_ok=True)

    # ---------- Ảnh ----------
    def add_image(self, kind: str, rid: str | None, jpeg: bytes, run_ocr: bool, by: str) -> dict:
        img = uid("i")
        atomic_write(IMAGES / f"{img}.jpg", jpeg)
        old = None
        with self.lock:
            r = self.find(rid) if rid else None
            if rid and not r:
                (IMAGES / f"{img}.jpg").unlink(missing_ok=True)
                raise KeyError("Không tìm thấy dòng (có thể đã bị xóa trên thiết bị khác).")
            if kind in PHOTO_KINDS:
                if not r:
                    raise KeyError("Thiếu dòng cho ảnh.")
                r.setdefault(PHOTO_KINDS[kind][0], []).append(img)
            else:
                if not r:
                    r = self.new_row(by=by)
                    self.rows.append(r)
                old = r.get("image")
                r["image"] = img
                r["upright"] = False  # ảnh tem mới: chưa biết chiều
                if run_ocr:
                    r["status"] = "queued"
                    r["error"] = ""
                elif r["status"] != "done":
                    r["status"] = "manual"
            self.emit_row(r)
        if old:
            (IMAGES / f"{old}.jpg").unlink(missing_ok=True)
        if kind == "label" and run_ocr:
            worker.push(r["id"])
        return r

    def rotate_label(self, rid: str, deg: int) -> None:
        """Xoay ảnh tem (độ, ngược chiều kim đồng hồ) và lưu thành ảnh mới (tên ảnh không đổi nội dung -> cache web).
        Ảnh đã xoay đứng: lần đọc sau không xoay thêm theo chiều đã học (rotPref)."""
        if deg % 360 not in (90, 180, 270):
            raise ValueError("Góc xoay không hợp lệ.")
        if not ocr.can_rotate():
            raise ValueError("Cần thư viện Pillow để xoay ảnh: pip install pillow")
        with self.lock:
            r = self.need(rid)
            old = r.get("image")
            if not old:
                raise ValueError("Dòng này chưa có ảnh tem.")
            new = uid("i")
            atomic_write(IMAGES / f"{new}.jpg", ocr.rotate_jpeg((IMAGES / f"{old}.jpg").read_bytes(), deg % 360))
            r["image"] = new
            r["upright"] = True
            # Đang chờ/đang đọc ảnh cũ: đọc lại theo chiều mới. Lần đọc đang chạy thấy ảnh đã đổi sẽ dừng, bỏ kết quả.
            reread = r["status"] in ("queued", "processing")
            if reread:
                r["status"], r["error"], r["warn"] = "queued", "", "Đã xoay ảnh — sẽ đọc lại theo chiều mới."
            self.emit_row(r)
        (IMAGES / f"{old}.jpg").unlink(missing_ok=True)  # lần đọc đang chạy đã nạp ảnh vào bộ nhớ từ trước
        if reread:
            worker.push(rid, front=True)

    def delete_photo(self, rid: str, img: str) -> None:
        with self.lock:
            r = self.need(rid)
            for field, _ in PHOTO_KINDS.values():
                if img in r.get(field, []):
                    r[field].remove(img)
                    (IMAGES / f"{img}.jpg").unlink(missing_ok=True)
            self.emit_row(r)

    def retry(self, rid: str, reset_edits: bool, mode: str = "") -> None:
        with self.lock:
            r = self.need(rid)
            if not r.get("image"):
                return
            if mode in ("quick", "full", "codes"):
                r["readModeNext"] = mode
            if reset_edits:
                for k in M.COMPARE_KEYS:
                    r["edited"].pop(k, None)
            if (r.get("match") or {}).get("how") != "manual":
                r["match"] = None
            r["checked"] = False
            r["status"] = "queued"
            r["error"] = ""
            self.emit_row(r)
        worker.push(rid)

    # ---------- Nhập lên bot Telegram ----------
    def tg_job(self, r: dict) -> TG.RepairJob:
        img = lambda i: str(IMAGES / f"{i}.jpg")  # noqa: E731
        return TG.RepairJob(
            values=dict(r["values"]),
            photos_before=[img(r["image"])] if r.get("image") else [],  # ảnh tem = ảnh TRƯỚC KHI TEST
            photos_after=[img(i) for i in r.get("photos", [])],
            photos_verify=[img(i) for i in r.get("vphotos", [])],
            date_format=self.config["dateFormat"],
        )

    def pcba_job(self, r: dict) -> PB.Job:
        img = lambda i: str(IMAGES / f"{i}.jpg")  # noqa: E731
        qr = r.get("qr") or {}
        return PB.Job(
            values=dict(r["values"]),
            photos_before=[img(r["image"])] if r.get("image") else [],  # ảnh tem = ảnh TRƯỚC KHI KIỂM TRA
            photos_after=[img(i) for i in r.get("photos", [])],
            photos_verify=[img(i) for i in r.get("vphotos", [])],
            qr_raw=qr.get("raw"), sn_source="QR" if qr.get("source") == "QR" else "MANUAL",
            date_format=self.config["dateFormat"],
        )

    def use_pcba(self) -> bool:
        return self.config.get("importTarget", "pcba") == "pcba"

    def import_check(self, r: dict) -> tuple[list[str], list[dict]]:
        if self.use_pcba():
            job = self.pcba_job(r)
            return PB.validate(job), PB.preview(job)
        job = self.tg_job(r)
        return TG.validate(job), TG.preview(job)

    def tg_preview(self, rid: str) -> dict:
        with self.lock:
            r = self.need(rid)
            errors, steps = self.import_check(r)
            tg = dict(r.get("tg") or {})
        return {"errors": errors, "steps": steps, "tg": tg, "target": "pcba" if self.use_pcba() else "telegram"}

    def tg_enqueue(self, rid: str, force: bool) -> None:
        with self.lock:
            r = self.need(rid)
            tg = r.get("tg") or {}
            if tg.get("status") in TG_ACTIVE:
                raise ValueError("Dòng này đang chờ/đang nhập.")
            if tg.get("status") in ("done", "uncertain") and not force:
                raise ValueError("Dòng này đã nhập (hoặc có thể đã tạo). Nhập lại sẽ tạo thêm một phiếu mới.")
            errs, _ = self.import_check(r)
            if errs:
                raise ValueError(" ".join(errs))
            hist = list(tg.get("history") or [])
            if tg.get("repairId"):
                hist.append({k: tg.get(k) for k in ("repairId", "deviceCode", "at")})
            # Lần trước lỗi: giữ đuôi nhật ký lần đó để còn biết vì sao (trước đây lần nhập lại xoá mất).
            prev = (tg.get("log") or [])[-15:] if tg.get("status") in ("error", "uncertain") else []
            log = [*prev, "──── nhập lại ────"] if prev else []
            # PCBA: lần trước lỗi giữa chừng -> giữ tiến độ (id phiếu, bước đã xong) để làm tiếp, không tạo phiếu mới.
            prog = tg.get("pcba") if tg.get("status") in ("error", "") and (tg.get("pcba") or {}).get("id") else None
            r["tg"] = {"status": "queued", "at": int(time.time() * 1000), "log": log, "history": hist,
                       **({"pcba": prog} if prog else {})}
            self.emit_row(r)
        tgworker.push(rid)

    def tg_dequeue(self, rid: str) -> None:
        with self.lock:
            r = self.need(rid)
            if (r.get("tg") or {}).get("status") != "queued":
                raise ValueError("Chỉ bỏ được dòng đang chờ (dòng đang chạy phải chờ bot xong).")
            r["tg"] = {**r["tg"], "status": "", "error": ""}
            self.emit_row(r)
        tgworker.remove(rid)

    def tg_update(self, rid: str, log_line: str = "", **fields) -> None:
        with self.lock:
            r = self.find(rid)
            if not r:
                return
            tg = r.setdefault("tg", {}) or {}
            r["tg"] = tg
            tg.update(fields)
            if log_line:
                tg["log"] = (tg.get("log") or [])[-90:] + [time.strftime("%H:%M:%S ") + log_line]
            self.emit_row(r)

    # ---------- Đọc tem bằng AI (gọi từ Worker) ----------
    def _plausible(self, res: dict, fmt: str) -> int:
        """Điểm tin cậy của một lần đọc: điểm ticket cao nhất trong DB; không có DB thì xét dạng mã."""
        if not res["is_label"]:
            return 0
        mapped = ocr.map_to_columns(res["fields"], fmt)
        vals = {k: f["value"] for k, f in mapped.items()}
        if self.index.ok and self.index.count:
            chars = ocr.map_chars(res["fields"])
            ranked = M.rank(vals, self.index.search_for(vals, chars, fmt=fmt), fmt, 1, chars)
            return ranked[0]["score"] if ranked else 0
        ok = bool(re.fullmatch(r"[A-Z0-9]{10,13}", M._alnum(vals["ticket_id"]))) + bool(re.fullmatch(r"\d{9,14}", M._alnum(vals["sn"])))
        return 50 * ok

    def _read_label(self, cfg: dict, jpeg: bytes, rid: str) -> tuple[dict, int]:
        """Đọc tem. Model chạy trên máy đọc kém khi ảnh bị xoay: đọc theo chiều đã thành công lần trước,
        nếu kết quả không khớp DB thì xoay 90° và đọc lại (tối đa 3 chiều). Trả (kết quả, góc xoay)."""
        if cfg.get("provider") != "ollama" or not ocr.can_rotate():
            return ocr.extract(cfg, jpeg), 0
        pref = int(cfg.get("rotPref") or 0)
        order = [pref, (pref + 90) % 360, (pref + 270) % 360]
        # Tự nhận chiều tem (không gọi AI, ~50 ms): chắc chắn thì đọc chiều đó trước -> tránh lần đọc sai chiều (~70 s).
        det = ocr.detect_orientation(jpeg) if not cfg.get("_upright") else {"deg": 0, "sure": True, "why": "ảnh đã xoay đứng"}
        if det.get("sure"):
            order = [det["deg"]] + [x for x in (pref, (pref + 90) % 360, (pref + 270) % 360, (pref + 180) % 360) if x != det["deg"]][:2]
        with self.lock:
            r = self.find(rid)
            if r:
                r["orient"] = {k: det.get(k) for k in ("deg", "sure", "why", "off")}
        best = None
        for rot in order:
            res = ocr.extract(cfg, ocr.rotate_jpeg(jpeg, rot))
            pts = self._plausible(res, cfg["dateFormat"])
            if not best or pts > best[0]:
                best = (pts, res, rot)
            if pts >= 50:
                break
            with self.lock:
                r = self.find(rid)
                if not r or r["status"] != "processing" or (cfg.get("_img") and r.get("image") != cfg["_img"]):
                    break  # dòng bị xoá / chuyển nhập tay / ảnh đã được xoay hoặc chụp lại: thôi thử các chiều còn lại
                r["warn"] = f"Chưa khớp DB ở chiều {rot}°, đang xoay ảnh đọc lại…"
                self.emit_row(r)
        pts, res, rot = best
        if pts >= 50 and rot != pref and not det.get("sure"):
            self.config["rotPref"] = rot  # lần sau thử chiều này trước (chỉ học khi không tự nhận được chiều)
            self.save_config()
        return res, rot

    def _quick_ok(self, res: dict, fmt: str) -> bool:
        """Đọc nhanh chỉ được tin khi SN khớp DB (đúng hoặc lệch 1 ký tự) và ra đúng MỘT ticket.
        Chỉ trạm + ngày thì không đủ: đo thực tế, SN + ngày đọc lệch đã khớp nhầm sang ticket khác cùng trạm."""
        vals = {k: f["value"] for k, f in ocr.map_to_columns(res["fields"], fmt).items()}
        chars = ocr.map_chars(res["fields"])
        d = M.decide(vals, self.index.search_for(vals, chars, fmt=fmt), fmt, chars)
        return d["status"] == "matched" and d["best"]["parts"]["sn"] >= 30

    def _read(self, cfg: dict, jpeg: bytes, rid: str) -> tuple[dict, int, str]:
        """Đọc nhanh (chỉ Mã trạm/Model/SN/Ngày, ticket lấy từ DB); nếu SN + ngày + trạm không khớp ticket
        nào trong DB thì đọc lại đầy đủ cả Ticket + mô tả. Trả (kết quả, góc xoay, chế độ đã dùng)."""
        if not ocr.is_quick(cfg):  # full, hoặc codes (đã đọc Ticket nên không cần đọc lại đầy đủ)
            return (*self._read_label(cfg, jpeg, rid), "codes" if cfg.get("readMode") == "codes" else "full")
        res, rot = self._read_label(cfg, jpeg, rid)
        if not res["is_label"] or not (self.index.ok and self.index.count) or self._quick_ok(res, cfg["dateFormat"]):
            return res, rot, "quick"
        with self.lock:
            r = self.find(rid)
            if not r:
                return res, rot, "quick"
            r["warn"] = "Đọc nhanh: SN chưa khớp chắc chắn một ticket trong DB — đang đọc đầy đủ cả Ticket…"
            self.emit_row(r)
        res, rot = self._read_label({**cfg, "readMode": "full"}, jpeg, rid)  # góc xoay luôn tính từ ảnh gốc
        return res, rot, "quick+full"

    def run_ocr(self, rid: str) -> None:
        with self.lock:
            r = self.find(rid)
            if not r or not r.get("image") or r["status"] not in ("queued", "processing"):
                return
            img = r["image"]
            r["status"] = "processing"
            self.emit_row(r)
            cfg = dict(self.config)
            if r.get("readModeNext"):  # "Đọc đầy đủ" cho riêng dòng này
                cfg["readMode"] = r.pop("readModeNext")
            if r.get("upright"):  # ảnh đã xoay đứng (tay hoặc lần đọc trước): thử chiều 0° trước, không xoay thêm
                cfg["rotPref"] = 0
                cfg["_upright"] = True
            cfg["_img"] = img  # để dừng sớm nếu ảnh bị xoay / chụp lại trong lúc đọc
        t0 = time.time()
        rot = 0
        mode = "full"
        try:
            jpeg = (IMAGES / f"{img}.jpg").read_bytes()
            res, rot, mode = self._read(cfg, jpeg, rid)
        except ocr.ExtractError as err:
            with self.lock:
                r = self.find(rid)
                if not r or r.get("image") != img or r["status"] != "processing":
                    return  # ảnh đã xoay / chụp lại / chuyển nhập tay trong lúc đọc
                if err.fatal:
                    r["status"] = "queued"  # chờ sửa cài đặt rồi tự chạy tiếp
                    worker.pause(str(err))
                    worker.push(rid, front=True)
                else:
                    r["status"], r["error"] = "error", str(err)
                self.emit_row(r)
            return
        except Exception as err:  # noqa: BLE001 - báo lỗi lên dòng
            with self.lock:
                r = self.find(rid)
                if r and r.get("image") == img and r["status"] == "processing":
                    r["status"], r["error"] = "error", f"Lỗi: {err}"
                    self.emit_row(r)
            return

        mapped = ocr.map_to_columns(res["fields"], cfg["dateFormat"])
        new_img = None
        if rot:  # lưu ảnh đã xoay thẳng để xem và để bước xác nhận dùng
            new_img = uid("i")
            atomic_write(IMAGES / f"{new_img}.jpg", ocr.rotate_jpeg(jpeg, rot))
        with self.lock:
            r = self.find(rid)
            if not r or r.get("image") != img or r["status"] != "processing":
                if new_img:
                    (IMAGES / f"{new_img}.jpg").unlink(missing_ok=True)
                return  # bị xóa / chụp lại / chuyển nhập tay trong lúc đọc
            if new_img:
                r["image"] = new_img
                r["upright"] = True  # đã lưu ảnh xoay đứng: đọc lại không xoay thêm
                (IMAGES / f"{img}.jpg").unlink(missing_ok=True)
            self._revert(r, keep_manual=True)
            r["tem"] = {k: f["value"] for k, f in mapped.items()}
            r["tem"]["model"] = res["fields"]["model"]["value"]
            r["chars"] = ocr.map_chars(res["fields"])
            r["ms"] = int((time.time() - t0) * 1000)
            r["readMode"] = mode
            r.pop("confirm", None)
            r["flags"] = {}
            for k, f in mapped.items():
                if r["edited"].get(k) or k in NO_AUTOFILL:
                    continue  # không ghi đè ô người dùng đã sửa tay; mô tả lỗi: chỉ giữ trong r["tem"]
                r["values"][k] = f["value"]
                if f["uncertain"]:
                    r["flags"][k] = f["note"] or "cần xác nhận"
            if res["is_label"]:
                r["status"], r["error"], r["warn"] = "done", "", res["issue"]
            else:
                r["status"], r["error"] = "error", res["issue"] or "Không đọc được tem trong ảnh."
            self.emit_row(r)
        if res["is_label"]:
            self.match(rid)


class Worker:
    """Hàng đợi đọc ảnh, chạy song song theo cài đặt `parallel`."""
    MAX = 5

    def __init__(self):
        self.q: list[str] = []
        self.cv = threading.Condition()
        self.paused = ""
        self.running = 0

    def start(self) -> None:
        for _ in range(self.MAX):
            threading.Thread(target=self._loop, daemon=True).start()

    def push(self, rid: str, front: bool = False) -> None:
        with self.cv:
            if rid in self.q:
                self.q.remove(rid)
            self.q.insert(0, rid) if front else self.q.append(rid)
            self.cv.notify_all()
        self.status()

    def pause(self, msg: str) -> None:
        self.paused = msg
        self.status()

    def resume(self) -> None:
        with self.cv:
            self.paused = ""
            self.cv.notify_all()
        self.status()

    def status(self) -> None:
        hub.send({"type": "worker", "paused": self.paused, "queue": len(self.q), "running": self.running,
                  "ready": ocr.ready(store.config)})

    def _loop(self) -> None:
        while True:
            with self.cv:
                while not self.q or self.paused or ocr.ready(store.config) or self.running >= int(store.config["parallel"]):
                    self.cv.wait(5)
                rid = self.q.pop(0)
                self.running += 1
            try:
                store.run_ocr(rid)
            finally:
                with self.cv:
                    self.running -= 1
                    self.cv.notify_all()
                self.status()


class TelegramWorker:
    """Nhập từng dòng lên bot Telegram, lần lượt (một phiên bot tại một thời điểm).
    Playwright chạy trong đúng một luồng này. Nhật ký: data/telegram_log.jsonl."""

    def __init__(self):
        self.q: list[str] = []
        self.cv = threading.Condition()
        self.current = ""
        self.stop_gen = 0  # tăng mỗi lần dừng khẩn cấp: việc lấy ra trước đó sẽ dừng
        self.lock = threading.Lock()  # một nơi điều khiển Telegram Web tại một thời điểm
        self.driver_factory = self.open_driver

    @staticmethod
    def open_driver(cfg: dict, log) -> TG.PlaywrightDriver:
        """Tự mở Edge (profile riêng) + tab Telegram nếu chưa có, rồi kết nối."""
        TG.ensure_telegram(cfg["tgCdp"], log=log)
        return TG.PlaywrightDriver(cfg["tgCdp"])

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def push(self, rid: str) -> None:
        with self.cv:
            if rid not in self.q:
                self.q.append(rid)
            self.cv.notify_all()
        self.status()

    def remove(self, rid: str) -> None:
        with self.cv:
            if rid in self.q:
                self.q.remove(rid)
        self.status()

    def stop(self) -> dict:
        """Dừng khẩn cấp: bỏ toàn bộ hàng đợi, dòng đang chạy dừng ở lần kiểm tra kế tiếp (≤ ~1 giây,
        lâu hơn nếu đang tải ảnh lên)."""
        with self.cv:
            self.stop_gen += 1
            dropped, self.q = list(self.q), []
            running = self.current
        for rid in dropped:
            store.tg_update(rid, "Bỏ khỏi hàng đợi (dừng khẩn cấp)", status="", error="")
        if running:
            store.tg_update(running, "⛔ Yêu cầu dừng khẩn cấp…", stopping=True)
        self.journal({"event": "emergency_stop", "running": running, "dropped": dropped})
        self.status()
        return {"running": running, "dropped": len(dropped)}

    def status(self) -> None:
        hub.send({"type": "tg", "current": self.current, "queue": list(self.q)})

    def journal(self, event: dict) -> None:
        try:
            with open(DATA / "telegram_log.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), **event}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _loop(self) -> None:
        while True:
            with self.cv:
                while not self.q:
                    self.cv.wait()
                rid = self.q.pop(0)
                self.current = rid
                gen = self.stop_gen
            self.status()
            try:
                self.run_one(rid, lambda: self.stop_gen != gen)
            finally:
                self.current = ""
                self.status()

    def run_one(self, rid: str, should_stop=lambda: False) -> None:
        if store.use_pcba():
            return self.run_pcba(rid, should_stop)
        with store.lock:
            r = store.find(rid)
            if not r or (r.get("tg") or {}).get("status") != "queued":
                return
            job = store.tg_job(r)
            cfg = dict(store.config)
            row_no = store.rows.index(r) + 1
        store.tg_update(rid, "Bắt đầu nhập lên bot", status="running", error="", step="", n=0, stopping=False,
                        total=len(TG.plan_steps(job)), started=int(time.time() * 1000))
        self.journal({"row": rid, "no": row_no, "event": "start", "values": job.values})
        driver = None
        held = False
        try:
            if should_stop():
                raise TG.Stopped("⛔ Đã dừng khẩn cấp trước khi gửi gì lên bot.")
            held = self.lock.acquire(timeout=60)  # chờ lần kiểm tra đăng nhập (nếu có) xong
            if not held:
                raise TG.BotError("Telegram Web đang bận (kiểm tra đăng nhập quá lâu) — thử lại.")
            store.tg_update(rid, step="Mở Telegram Web")
            driver = self.driver_factory(cfg, lambda s: store.tg_update(rid, s))
            runner = TG.Runner(driver, job, timeout=float(cfg.get("tgTimeout") or 45), stop=should_stop,
                               progress=lambda e: store.tg_update(rid, step=e["label"], n=e["n"], total=e["total"], detail=e["detail"]),
                               log=lambda s: store.tg_update(rid, s))
            res = runner.run()
        except TG.BotError as e:
            status = "uncertain" if e.uncertain else "error"
            store.tg_update(rid, f"LỖI: {e}", status=status, error=str(e), stopping=False, at=int(time.time() * 1000))
            with store.lock:
                tail = list(((store.find(rid) or {}).get("tg") or {}).get("log") or [])[-8:]
            self.journal({"row": rid, "no": row_no, "event": "stopped" if isinstance(e, TG.Stopped) else status,
                          "uncertain": e.uncertain, "error": str(e), "log": tail})
            return
        except Exception as e:  # noqa: BLE001
            store.tg_update(rid, f"LỖI: {e}", status="error", error=f"Lỗi: {e}", at=int(time.time() * 1000))
            self.journal({"row": rid, "no": row_no, "event": "error", "error": str(e)})
            return
        finally:
            if driver and hasattr(driver, "close"):
                driver.close()
            if held:
                self.lock.release()
        code = res["device_code"] or f"không có mã ({res['test_tool_result'] or 'bot trả về -'})"
        store.tg_update(rid, f"✅ Repair #{res['repair_id']} · mã thiết bị {code}", status="done",
                        repairId=res["repair_id"], deviceCode=res["device_code"], boardCode=res["board"],
                        result=res["test_tool_result"],
                        messageId=res["message_id"], botText=res["text"], at=int(time.time() * 1000))
        self.journal({"row": rid, "no": row_no, "event": "done", **{k: res[k] for k in ("repair_id", "device_code", "board", "message_id")}})


    def run_pcba(self, rid: str, should_stop=lambda: False) -> None:
        with store.lock:
            r = store.find(rid)
            if not r or (r.get("tg") or {}).get("status") != "queued":
                return
            job = store.pcba_job(r)
            prog = json.loads(json.dumps((r.get("tg") or {}).get("pcba") or {}))
            row_no = store.rows.index(r) + 1
        steps = [k for k, _ in PB.plan_steps(job)]
        resume = bool(prog.get("id"))
        store.tg_update(rid, f"Tiếp tục phiếu {prog.get('ticketNo')}" if resume else "Bắt đầu nhập lên hệ thống PCBA",
                        status="running", error="", step="", n=0, stopping=False, total=len(steps),
                        started=int(time.time() * 1000), pcba=prog)
        self.journal({"row": rid, "no": row_no, "event": "start", "target": "pcba", "resume": resume, "values": job.values})

        def step(key: str, label: str) -> None:
            n = steps.index(key) + 1 if key in steps else 0
            store.tg_update(rid, label, step=label, n=n, total=len(steps))

        def save(p: dict) -> None:
            store.tg_update(rid, pcba=json.loads(json.dumps(p)), deviceCode=p.get("ticketNo", ""))

        try:
            res = PB.submit(pcba.client(), job, prog, save=save, step=step, stop=should_stop)
        except (PB.PcbaError, CL.LoginError) as e:
            store.tg_update(rid, f"LỖI: {e}", status="error", error=str(e), stopping=False, at=int(time.time() * 1000),
                            pcba=prog)
            self.journal({"row": rid, "no": row_no, "event": "error", "target": "pcba", "error": str(e),
                          "ticketNo": prog.get("ticketNo")})
            return
        except Exception as e:  # noqa: BLE001
            store.tg_update(rid, f"LỖI: {e}", status="error", error=f"Lỗi: {e}", at=int(time.time() * 1000), pcba=prog)
            self.journal({"row": rid, "no": row_no, "event": "error", "target": "pcba", "error": str(e)})
            return
        note = f" · {res['note']}" if res.get("note") else ""
        store.tg_update(rid, f"✅ Phiếu {res.get('ticketNo')} · trạng thái {res.get('status') or '?'}{note}", status="done",
                        repairId=str(res.get("id") or ""), deviceCode=res.get("ticketNo") or "", result=job.result,
                        pcba=res, at=int(time.time() * 1000))
        self.journal({"row": rid, "no": row_no, "event": "done", "target": "pcba", "id": res.get("id"),
                      "ticketNo": res.get("ticketNo"), "status": res.get("status")})


class PcbaSession:
    """Một phiên đăng nhập hệ thống PCBA dùng chung (cookie); tạo lại khi đổi tài khoản."""

    def __init__(self):
        self.lock = threading.Lock()
        self._c: PB.Client | None = None

    def reset(self) -> None:
        with self.lock:
            self._c = None

    def client(self) -> PB.Client:
        with self.lock:
            if not self._c:
                c = store.config
                self._c = PB.Client(c.get("pcbaUser", ""), CL.unprotect(c.get("pcbaPass", "")),
                                    c.get("pcbaBase") or PB.BASE)
            return self._c


# ---------- HTTP ----------

def lan_urls(port: int) -> list[str]:
    ips: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # không gửi gói nào, chỉ để biết IP ra mạng
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return [f"http://{ip}:{port}" for ip in ips]


def public_config(c: dict) -> dict:
    out = {k: v for k, v in c.items() if k not in SECRET_KEYS}
    for key in SECRET_KEYS:
        v = c.get(key) or ""
        out[key + "Hint"] = ("••••" + v[-4:]) if v else ""
    out["cctsPassHint"] = "••••••" if c.get("cctsPass") else ""  # bản mã hoá: không hé lộ ký tự nào
    out["pcbaPassHint"] = "••••••" if c.get("pcbaPass") else ""
    return out


def device_label(ua: str, name: str) -> str:
    if name:
        return name[:40]
    if re.search(r"iPhone|Android.*Mobile|Mobile", ua):
        return "📱 Điện thoại"
    if re.search(r"iPad|Android|Tablet", ua):
        return "📱 Máy tính bảng"
    return "💻 Máy tính"


class Handler(SimpleHTTPRequestHandler):
    port = 8000
    lan = True

    def log_message(self, fmt, *args):
        if "/api/" in getattr(self, "path", "") and "/api/events" not in self.path and "/api/img/" not in self.path:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        elif self.path.startswith(("/js/", "/css/", "/index.html")) or self.path == "/":
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _json(self, code: int, data) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self, limit: int = 20 << 20) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("Dữ liệu quá lớn")
        return self.rfile.read(n) if n else b""

    # --- GET ---
    def do_GET(self):
        url = urlparse(self.path)
        p = posixpath.normpath(unquote(url.path)) + ("/" if url.path.endswith("/") and url.path != "/" else "")
        if not p.startswith("/api/"):
            if p != "/" and not p.startswith(STATIC):
                return self.send_error(404)  # không lộ data/, file .py, config
            return super().do_GET()
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if p == "/api/events":
                return self._events(q)
            if p.startswith("/api/img/"):
                return self._image(p.rsplit("/", 1)[-1])
            if p == "/api/info":
                index.ensure()
                return self._json(200, {"ok": True, "urls": lan_urls(self.port) if self.lan else [],
                                        "local": f"http://localhost:{self.port}", "lan": self.lan,
                                        "db": {"ok": index.ok and index.count > 0, "count": index.count, "name": Path(index.db_path).name, "newest": DS.db_info(index.db_path).get("newest", "")}})
            if p == "/api/config":
                return self._json(200, public_config(store.config))
            m = re.fullmatch(r"/api/rows/([\w-]+)/candidates", p)
            if m:
                return self._json(200, {"ok": True, "items": store.candidates(m[1])})
            m = re.fullmatch(r"/api/rows/([\w-]+)/tgpreview", p)
            if m:
                return self._json(200, {"ok": True, **store.tg_preview(m[1])})
            if p == "/api/dbsync":
                return self._json(200, {"ok": True, **dbsync.snapshot(), "db": DS.db_info(index.db_path)})
            if p == "/api/tg/status":
                if store.use_pcba():  # hệ thống PCBA: không cần Edge / Telegram Web
                    return self._json(200, {"ok": True, "target": "pcba", "current": tgworker.current,
                                            "queue": list(tgworker.q)})
                return self._json(200, {"ok": True, "target": "telegram", **TG.cdp_status(store.config["tgCdp"]),
                                        "edgePath": TG.find_edge(), "playwright": _has_playwright(),
                                        "current": tgworker.current, "queue": list(tgworker.q)})
            if p == "/api/photos.zip":
                return self._zip()
            return self._json(404, {"ok": False, "error": "not found"})
        except KeyError as e:
            return self._json(404, {"ok": False, "error": e.args[0]})
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"ok": False, "error": str(e)})

    def _events(self, q: dict) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        cid, events = hub.add(device_label(self.headers.get("User-Agent", ""), q.get("name", "")))
        try:
            self.wfile.write(f"data: {json.dumps(store.snapshot(), ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            worker.status()
            tgworker.status()
            while True:
                try:
                    msg = events.get(timeout=15)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            hub.remove(cid)

    def _image(self, img: str) -> None:
        f = IMAGES / f"{img}.jpg"
        if not ID_RE.fullmatch(img) or not f.exists():
            return self.send_error(404)
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable")  # tên ảnh không bao giờ đổi nội dung
        self.end_headers()
        self.wfile.write(data)

    def _zip(self) -> None:
        buf = io.BytesIO()
        with store.lock:
            rows = [dict(r) for r in store.rows]
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            for i, r in enumerate(rows, 1):
                name = re.sub(r"[^\w.-]+", "_", r["values"].get("ticket_id") or r["values"].get("sn") or "")
                folder = f"{i:03d}_{name}".rstrip("_")
                files = [("tem", r["image"])] if r.get("image") else []
                for field, name in PHOTO_KINDS.values():
                    files += [(f"{name}_{j}", img) for j, img in enumerate(r.get(field, []), 1)]
                for label, img in files:
                    f = IMAGES / f"{img}.jpg"
                    if f.exists():
                        z.write(f, f"{folder}/{label}.jpg")
        data = buf.getvalue()
        self.send_response(200)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Content-Disposition", f'attachment; filename="anh_hang_loi_{time.strftime("%Y-%m-%d")}.zip"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # --- POST ---
    def do_POST(self):
        url = urlparse(self.path)
        p = url.path
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if p == "/api/upload":
                kind = q.get("kind", "label")
                if kind != "label" and kind not in PHOTO_KINDS:
                    raise ValueError("kind không hợp lệ")
                data = self._body()
                if not data.startswith(b"\xff\xd8"):
                    raise ValueError("Chỉ nhận ảnh JPEG")
                r = store.add_image(kind, q.get("row") or None, data, q.get("ocr", "1") != "0",
                                    device_label(self.headers.get("User-Agent", ""), ""))
                return self._json(200, {"ok": True, "id": r["id"]})

            body = json.loads(self._body(1 << 20) or b"{}")
            if p == "/api/rows":
                with store.lock:
                    r = store.new_row()
                    keys = store.allowed_keys()
                    for k, v in (body.get("values") or {}).items():
                        if k in keys:
                            r["values"][k] = str(v)[:2000]
                            r["edited"][k] = True
                    r["checked"] = bool(body.get("checked"))
                    store.rows.append(r)
                    store.emit_row(r)
                return self._json(200, {"ok": True, "id": r["id"]})
            m = re.fullmatch(r"/api/rows/([\w-]+)/(\w+)", p)
            if m:
                rid, act = m[1], m[2]
                if act == "patch":
                    store.patch(rid, body)
                elif act == "delete":
                    store.delete_row(rid)
                elif act == "retry":
                    if worker.paused:
                        worker.resume()
                    store.retry(rid, bool(body.get("resetEdits")), str(body.get("mode") or ""))
                elif act == "match":
                    if body.get("pk") == "":
                        store.choose(rid, "", bool(body.get("clear")))  # bỏ khớp: nhanh, làm ngay để web thấy liền
                    else:
                        threading.Thread(target=store.choose, args=(rid, body.get("pk")), daemon=True).start()
                elif act == "use":
                    store.use_value(rid, str(body.get("key")), str(body.get("source")))
                elif act == "photodel":
                    store.delete_photo(rid, str(body.get("img")))
                elif act == "rotate":
                    store.rotate_label(rid, int(body.get("deg") or 0))
                elif act == "tg":
                    store.tg_enqueue(rid, bool(body.get("force")))
                elif act == "tgcancel":
                    store.tg_dequeue(rid)
                elif act == "qr":
                    store.set_qr(rid, str(body.get("raw") or ""))
                elif act == "sncheck":
                    threading.Thread(target=store.sn_check, args=(rid,), daemon=True).start()
                else:
                    raise KeyError("Thao tác không hợp lệ")
                return self._json(200, {"ok": True})
            if p == "/api/rows/batch":
                return self._json(200, {"ok": True, "n": store.patch_many(body.get("changes") or [])})
            if p == "/api/fill":
                n = store.fill(str(body.get("key")), str(body.get("value") or "")[:500], str(body.get("mode")), bool(body.get("setDefault")))
                return self._json(200, {"ok": True, "n": n})
            if p == "/api/cols":
                store.set_cols(body.get("cols") or [], body.get("fillId"))
                return self._json(200, {"ok": True})
            if p == "/api/clear":
                store.clear()
                return self._json(200, {"ok": True})
            if p == "/api/rematch":
                ids = [r["id"] for r in store.rows]
                threading.Thread(target=lambda: [store.match(i) for i in ids], daemon=True).start()
                return self._json(200, {"ok": True})
            if p == "/api/retry-errors":
                worker.resume()
                for r in [r for r in store.rows if r["status"] == "error" and r.get("image")]:
                    store.retry(r["id"], False)
                return self._json(200, {"ok": True})
            if p == "/api/dbsync/start":
                c = store.config
                login = ccts_login if c.get("cctsUser") and c.get("cctsPass") else None
                dbsync.start(str(body.get("mode") or "new"), c.get("cctsSso", ""), c.get("cctsToken", ""), index.db_path,
                             c.get("cctsToolDir") or DS.DEFAULT_TOOL_DIR, int(c.get("cctsPageSize") or 100),
                             float(c.get("cctsDelay") if c.get("cctsDelay") is not None else 3), login=login)
                return self._json(200, {"ok": True, "login": bool(login)})
            if p == "/api/pcba/login":
                pcba.reset()
                try:
                    me = pcba.client().login()
                except (PB.PcbaError, CL.LoginError) as e:
                    return self._json(200, {"ok": False, "error": str(e)})
                user = (me or {}).get("user") or me or {}
                return self._json(200, {"ok": True, "name": user.get("displayName") or user.get("username") or ""})
            if p == "/api/dbsync/login":
                try:
                    ccts_login()
                except CL.LoginError as e:
                    return self._json(200, {"ok": False, "error": str(e)})
                return self._json(200, {"ok": True, "config": public_config(store.config)})
            if p == "/api/dbsync/stop":
                return self._json(200, {"ok": True, "stopped": dbsync.stop()})
            if p == "/api/tg/stop":
                return self._json(200, {"ok": True, **tgworker.stop()})
            if p in ("/api/tg/launch", "/api/tg/ensure"):
                return self._json(200, {"ok": True, **ensure_telegram()})
            if p == "/api/config":
                return self._save_config(body)
            if p == "/api/config/test":
                cfg = {**store.config, **{k: v for k, v in body.items() if k in DEFAULT_CONFIG and v not in (None, "")}}
                try:
                    models = ocr.list_models(cfg)
                    return self._json(200, {"ok": True, "models": models, "suggest": ocr.pick_default(models)})
                except ocr.ExtractError as e:
                    return self._json(200, {"ok": False, "error": str(e)})
            return self._json(404, {"ok": False, "error": "not found"})
        except KeyError as e:
            return self._json(404, {"ok": False, "error": e.args[0] if e.args else "not found"})
        except ValueError as e:
            return self._json(400, {"ok": False, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._json(500, {"ok": False, "error": str(e)})

    def _save_config(self, body: dict) -> None:
        c = store.config
        old_fmt = c["dateFormat"]
        if body.get("provider") in ("gemini", "ollama"):
            c["provider"] = body["provider"]
        if "geminiKey" in body:  # chỉ gửi khi người dùng gõ key mới / xóa key
            c["geminiKey"] = str(body["geminiKey"] or "").strip()
        for k in ("geminiModel", "geminiConfirmModel", "ollamaUrl", "ollamaModel"):
            if body.get(k):
                c[k] = str(body[k]).strip()
        if body.get("parallel"):
            c["parallel"] = max(1, min(Worker.MAX, int(body["parallel"])))
        if body.get("dateFormat") in M.DATE_FORMATS:
            c["dateFormat"] = body["dateFormat"]
        if body.get("speed") in ("fast", "careful"):
            c["speed"] = body["speed"]
        if body.get("readMode") in ("full", "quick", "codes"):
            c["readMode"] = body["readMode"]
        if str(body.get("aiMaxSide", "")) in ("0", "1024", "1280", "1568"):
            c["aiMaxSide"] = int(body["aiMaxSide"])
        for k in ("cctsSso", "cctsToken"):  # chỉ gửi khi người dùng gõ giá trị mới / xoá
            if k in body:
                c[k] = str(body[k] or "").strip()
        if "pcbaUser" in body:
            c["pcbaUser"] = str(body["pcbaUser"] or "").strip()
            pcba.reset()
        if "pcbaPass" in body:
            c["pcbaPass"] = CL.protect(str(body["pcbaPass"] or ""))
            pcba.reset()
        if body.get("importTarget") in ("pcba", "telegram"):
            c["importTarget"] = body["importTarget"]
        if "cctsUser" in body:
            c["cctsUser"] = str(body["cctsUser"] or "").strip()
        if "cctsPass" in body:  # chỉ gửi khi người dùng gõ mật khẩu mới / xoá
            c["cctsPass"] = CL.protect(str(body["cctsPass"] or ""))
        if body.get("cctsToolDir"):
            c["cctsToolDir"] = str(body["cctsToolDir"]).strip()
        if body.get("cctsPageSize"):
            c["cctsPageSize"] = max(10, min(1000, int(body["cctsPageSize"])))
        if body.get("cctsDelay") not in (None, ""):
            c["cctsDelay"] = max(0.0, min(60.0, float(body["cctsDelay"])))
        if "aiConfirm" in body:
            c["aiConfirm"] = bool(body["aiConfirm"])
        if re.fullmatch(r"http://(127\.0\.0\.1|localhost):\d{2,5}/?", str(body.get("tgCdp") or "")):
            c["tgCdp"] = body["tgCdp"].rstrip("/")
        if body.get("tgTimeout"):
            c["tgTimeout"] = max(10, min(300, int(body["tgTimeout"])))
        store.save_config()
        if c["dateFormat"] != old_fmt:
            hub.send(store.snapshot())
        worker.resume()
        return self._json(200, {"ok": True, "config": public_config(c)})


def _has_playwright() -> bool:
    import importlib.util
    return importlib.util.find_spec("playwright") is not None


def ensure_telegram() -> dict:
    """Tự mở Edge (profile riêng cho bot, cổng debug — không đụng Edge dùng hằng ngày) + tab Telegram Web,
    rồi xem đã đăng nhập chưa. state: ready | login | loading | busy (đang nhập một dòng: chắc chắn sẵn sàng)."""
    cdp = store.config["tgCdp"]
    try:
        st = TG.ensure_telegram(cdp)
    except TG.BotError as e:
        return {"ready": False, "state": "error", "message": str(e), **TG.cdp_status(cdp)}
    if not tgworker.lock.acquire(timeout=0.2):
        return {**st, "ready": True, "state": "busy", "message": ""}
    try:
        state = TG.check_login(cdp, wait=25 if st.get("launched") else 15)
    except TG.BotError as e:
        return {**st, "ready": False, "state": "error", "message": str(e)}
    finally:
        tgworker.lock.release()
    msg = {"ready": "", "login": TG.LOGIN_MSG, "loading": "Telegram Web chưa tải xong — chờ thêm vài giây rồi bấm Kiểm tra lại."}[state]
    return {**st, "ready": state == "ready", "state": state, "message": msg}


# ---------- Khởi động ----------

hub = Hub()
index: M.TicketIndex
store: Store
worker = Worker()
tgworker = TelegramWorker()
pcba = PcbaSession()


def _after_dbsync(status: str) -> None:
    """Đồng bộ xong: nạp lại chỉ mục DB ngay, rồi đối chiếu lại các dòng chưa khớp ticket (trừ dòng nhập tay)."""
    if status not in ("completed", "stopped_incremental", "stopped_limit", "interrupted"):
        return
    index.checked_at = 0
    index.ensure()
    with store.lock:
        ids = [r["id"] for r in store.rows if r["status"] == "done" and (r.get("match") or {}).get("status") != "matched"
               and (r.get("match") or {}).get("how") != "manual"]
    for rid in ids:
        store.match(rid)
    hub.send({"type": "dbsync_done", "rematched": len(ids), "db": DS.db_info(index.db_path)})


def ccts_login() -> tuple[str, str]:
    """Đăng nhập bằng tài khoản đã lưu, lưu lại cookie/token mới (để Kiểm tra token / chạy tay vẫn dùng được)."""
    c = store.config
    sso, token = CL.login(c.get("cctsUser", ""), CL.unprotect(c.get("cctsPass", "")))
    c["cctsSso"], c["cctsToken"] = sso, token
    store.save_config()
    return sso, token


dbsync = DS.DbSync(emit=lambda e: hub.send(e), on_done=_after_dbsync)


_data_lock = None  # giữ file khoá suốt thời gian server chạy


def lock_data_dir(data: Path) -> None:
    """Chỉ MỘT server được dùng một thư mục data/. Hai server cùng chạy sẽ ghi đè state.json của nhau
    (đã xảy ra 21/10: xoay ảnh xong thì server kia ghi lại tên ảnh cũ -> 3 ảnh tem bị mất).
    Khoá tự nhả khi tiến trình kết thúc, kể cả bị tắt đột ngột."""
    global _data_lock
    data.mkdir(parents=True, exist_ok=True)
    f = open(data / ".server.lock", "a+")
    try:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        print(f"\n  ✖ Đã có một server khác đang chạy với thư mục dữ liệu {data}.\n"
              f"    Hai server cùng lúc sẽ ghi đè dữ liệu của nhau. Tắt cửa sổ server kia (Ctrl+C) rồi chạy lại.\n", flush=True)
        sys.exit(1)
    _data_lock = f


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """Không cho tiến trình khác chiếm chung cổng (Windows mặc định cho phép khi bật SO_REUSEADDR)."""
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> None:
    global index, store, DATA, IMAGES
    for stream in (sys.stdout, sys.stderr):  # console Windows mặc định không in được tiếng Việt
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="Web OCR tem hàng lỗi — dữ liệu dùng chung máy tính + điện thoại")
    ap.add_argument("--db", default=os.environ.get("CCTS_DB", DEFAULT_DB), help="đường dẫn ccts_tickets.db")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--data", help="thư mục lưu bảng + ảnh (mặc định ./data)")
    ap.add_argument("--local", action="store_true", help="chỉ cho máy này truy cập (không mở cho mạng LAN)")
    ap.add_argument("--lan", action="store_true", help=argparse.SUPPRESS)  # tương thích lệnh cũ (mặc định đã mở LAN)
    args = ap.parse_args()

    if args.data:
        DATA = Path(args.data).resolve()
        IMAGES = DATA / "images"
    lock_data_dir(DATA)  # trước khi đọc state.json: không cho server thứ hai chạy cùng thư mục
    index = M.TicketIndex(args.db)
    if index.ok:
        index.ensure()
    else:
        print(f"⚠ Không tìm thấy DB: {args.db} — vẫn chạy nhưng không đối chiếu ticket (dùng --db).", flush=True)
    store = Store(hub, index)
    # Ảnh đang chờ/đang đọc khi tắt server -> đọc tiếp.
    for r in store.rows:
        if r["status"] in ("queued", "processing"):
            r["status"] = "queued"
            worker.q.append(r["id"])
        tg = r.get("tg") or {}
        if tg.get("status") == "queued":  # không tự chạy lại: người dùng bấm nhập lại
            tg.update(status="", error="")
        elif tg.get("status") == "running":
            tg.update(status="uncertain", error="Server tắt khi đang nhập — kiểm tra chat bot trước khi nhập lại.")
    worker.start()
    tgworker.start()

    Handler.port = args.port
    Handler.lan = not args.local
    host = "127.0.0.1" if args.local else "0.0.0.0"
    try:
        server = ExclusiveHTTPServer((host, args.port), partial(Handler, directory=str(ROOT)))
    except OSError as e:
        print(f"\n  ✖ Cổng {args.port} đang được chương trình khác dùng ({e}). Tắt server kia hoặc dùng --port khác.\n", flush=True)
        sys.exit(1)
    server.daemon_threads = True
    print(f"\n  Máy tính:   http://localhost:{args.port}", flush=True)
    if not args.local:
        for u in lan_urls(args.port):
            print(f"  Điện thoại: {u}   (cùng Wi-Fi; trong web bấm 📱 để quét QR)", flush=True)
        print("  Lưu ý: mọi thiết bị trong mạng LAN mở được web này. Dùng --local để chỉ máy này.\n", flush=True)
    if ocr.ready(store.config):
        print(f"  AI: {ocr.ready(store.config)} Mở web → ⚙️ Cài đặt.\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
