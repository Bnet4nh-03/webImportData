"""Nhập một dòng của bảng lên bot Telegram PCBA-CCVN ("Thêm Repair mới") và lấy mã thiết bị bot trả về.

Cách chạy: Edge mở Telegram Web bằng profile riêng có cổng debug (xem `edge_command`), server điều khiển
tab đó qua Playwright (CDP). Quy trình được dựng theo lịch sử chat thật (76 lần tạo Repair #833..#957):

    /repair → ➕ Thêm mới → Board → (➕ Thêm mới thiết bị) → nhà thầu → ảnh TRƯỚC KHI TEST → PASS/FAIL
      FAIL: failure_cause → disposition → after_repair_result → ảnh SAU KHI SỬA → trạng thái trạm
    → ticket → SN → Date onsite → Original phenomenon → Station code → ảnh xác minh lỗi → "✅ Đã tạo Repair #N"

Bot KHÔNG có bước xác nhận: hành động ở bước ảnh xác minh lỗi tạo record ngay. Vì vậy người dùng xác nhận
trên web trước khi chạy, và lỗi xảy ra sau bước đó được đánh dấu "cần kiểm tra tay" thay vì chạy lại.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import matching as M

BOT_PEER_ID = "8656506505"  # @pcba_ccvn_bot
DEFAULT_CDP = "http://127.0.0.1:9222"
PROFILE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "PCBA-CCVN-EdgeAutomation"

CANCEL = "❎ Hủy"
DONE_PHOTOS = "✅ Xong / Bỏ qua"
SKIP = "🚫 Bỏ qua"
NO_TICKET = "🚫 Không có ticket"
NO_CONTRACTOR = "🚫 Không có trong danh sách / Bỏ trống"
NEW_DEVICE = "➕ Thêm mới"
RESULT_BTN = {"PASS": "✅ PASS", "FAIL": "❌ FAIL", "DISCARD": "🗑 Discard"}
STATION_BTN = {"PENDING": "⏳ Pending", "OK": "✅ OK"}
CONTRACTORS = ("EC", "ES", "ITS", "HTE", "SE HN", "SE HCM")
MAX_TEXT = 3500


class BotError(Exception):
    """Lỗi khi nhập. `uncertain`: đã qua bước cuối, có thể bot đã tạo record."""

    def __init__(self, msg: str, uncertain: bool = False):
        super().__init__(msg)
        self.uncertain = uncertain


class Stopped(BotError):
    """Người dùng bấm dừng khẩn cấp."""


# ---------- Dữ liệu một dòng ----------

@dataclass
class RepairJob:
    values: dict
    photos_before: list[str] = field(default_factory=list)  # ảnh TRƯỚC KHI TEST (bắt buộc 1–2)
    photos_after: list[str] = field(default_factory=list)   # ảnh SAU KHI SỬA (0–3, chỉ khi FAIL)
    photos_verify: list[str] = field(default_factory=list)  # ảnh xác minh lỗi (0–1)
    date_format: str = "mdy"

    def v(self, key: str) -> str:
        return str(self.values.get(key) or "").strip()

    @property
    def result(self) -> str:
        return self.v("test_tool_result").upper()


def board_code(label: str) -> str:
    return label.split(" - ", 1)[0].strip()


def bot_date(s: str, fmt: str) -> str:
    """Bot nhận ngày dạng M/D/YYYY (vd 8/17/2026)."""
    d = M.parse_day(s, fmt)
    return f"{d.month}/{d.day}/{d.year}" if d else ""


def bot_text(s: str) -> str:
    """Một dòng (Enter trên Telegram Web là gửi), giới hạn độ dài."""
    s = re.sub(r"\s*[\r\n]+\s*", " / ", str(s or "").strip())
    return s[:MAX_TEXT]


def validate(job: RepairJob) -> list[str]:
    """Lỗi chặn import. Rỗng = đủ dữ liệu."""
    err = []
    if not job.v("board"):
        err.append("Chưa chọn Board.")
    c = job.v("contractor")
    if c and c not in CONTRACTORS:
        err.append(f"Nhà thầu '{c}' không có trong danh sách nút của bot ({', '.join(CONTRACTORS)}).")
    if not job.photos_before:
        err.append("Cần ít nhất 1 ảnh TRƯỚC KHI TEST (ảnh tem của dòng).")
    res = job.result
    if res not in RESULT_BTN:
        err.append("test_tool_result phải là PASS, FAIL hoặc Discard.")
    if res == "FAIL":
        for k in ("failure_cause", "disposition"):
            if not job.v(k):
                err.append(f"FAIL cần nhập {k}.")
        if job.v("after_repair_result").upper() != "PASS":
            err.append("FAIL cần after_repair_result = PASS (nhánh after_repair FAIL chưa khảo sát).")
        if job.v("charging_station_test_status").upper() not in STATION_BTN:
            err.append("FAIL cần charging_station_test_status = Pending hoặc OK.")
    if job.v("date_onsite") and not bot_date(job.v("date_onsite"), job.date_format):
        err.append(f"Date onsite '{job.v('date_onsite')}' không đọc được.")
    for p in job.photos_before + job.photos_after + job.photos_verify:
        if not Path(p).is_file():
            err.append(f"Thiếu file ảnh {Path(p).name}.")
    return err


def plan_steps(job: RepairJob) -> list[tuple[str, str]]:
    """Các bước dự kiến (để vẽ thanh tiến độ). PASS và Discard: bot bỏ qua 5 bước của FAIL, sang thẳng
    Ticket ID (lịch sử chat: 23 PASS + 6 Discard). Discard: bot tự ghi Disposition = Discard, mã thiết bị "-"."""
    s = [("menu", "Mở menu Repair"), ("board", "Chọn Board"), ("device", "Thiết bị"), ("contractor", "Nhà thầu"),
         ("before", "Ảnh trước khi test"), ("test", "Kết quả test")]
    if job.result == "FAIL":
        s += [("cause", "failure_cause"), ("disp", "disposition"), ("after", "after_repair_result"),
              ("after_photo", "Ảnh sau khi sửa"), ("station_status", "Trạng thái trạm")]
    s += [("ticket", "Ticket ID"), ("sn", "SN"), ("date", "Date onsite"), ("phen", "Original phenomenon"),
          ("station", "Station code"), ("verify", "Ảnh xác minh lỗi (tạo record)")]
    return s


def preview(job: RepairJob) -> list[dict]:
    """Từng bước sẽ bấm/gửi gì — hiện cho người dùng xác nhận trước khi chạy."""
    j = job
    n = lambda xs: f"{len(xs)} ảnh" if xs else ""  # noqa: E731
    out = {
        "menu": "/repair → ➕ Thêm mới", "board": j.v("board"), "device": NEW_DEVICE + " (mã thiết bị mới)",
        "contractor": j.v("contractor") or NO_CONTRACTOR, "before": n(j.photos_before[:2]),
        "test": RESULT_BTN.get(j.result, j.result), "cause": bot_text(j.v("failure_cause")),
        "disp": bot_text(j.v("disposition")), "after": RESULT_BTN.get(j.v("after_repair_result").upper(), ""),
        "after_photo": n(j.photos_after[:3]) or DONE_PHOTOS,
        "station_status": STATION_BTN.get(j.v("charging_station_test_status").upper(), ""),
        "ticket": bot_text(j.v("ticket_id")) or NO_TICKET, "sn": bot_text(j.v("sn")) or SKIP,
        "date": bot_date(j.v("date_onsite"), j.date_format) or SKIP,
        "phen": bot_text(j.v("original_phenomenon")) or SKIP, "station": bot_text(j.v("station_code")) or SKIP,
        "verify": n(j.photos_verify[:1]) or SKIP,
    }
    return [{"key": k, "label": label, "value": out.get(k, "")} for k, label in plan_steps(job)]


CREATED_RE = re.compile(r"Đã tạo Repair #(\d+)")


def parse_created(text: str) -> dict:
    g = lambda p: (m[1].strip() if (m := re.search(p, text)) else "")  # noqa: E731
    code = g(r"Thiết bị \(code\):\s*(\S+)")
    return {"repair_id": g(r"Đã tạo Repair #(\d+)"), "board": g(r"Board:\s*(\S+)"),
            "device_code": "" if code in ("-", "None") else code,  # Discard: bot không cấp mã thiết bị
            "test_tool_result": g(r"Test tool result:\s*(\S+)"), "ticket_id": g(r"Ticket ID:\s*(\S+)")}


# ---------- Máy trạng thái ----------

# (khóa bước, đoạn đầu tin nhắn của bot). Thứ tự quan trọng: kiểm tra từ trên xuống.
PROMPTS = (
    ("created", "✅ Đã tạo Repair"),
    ("expired", "⚠️ Phiên thêm Repair đã hết hạn"),
    ("admin", "⛔"),
    ("cancelled", "❎ Đã hủy"),
    ("menu", "🔧 Quản lý Repair"),
    ("board", "➕ Thêm Repair mới"),
    ("device", "Board này đã có"),
    ("contractor", "Chọn nhà thầu"),
    ("before", "📷 Gửi ảnh TRƯỚC KHI TEST"),
    ("photo_ack", "✅ Đã lưu ảnh"),
    ("test", "Bước 3: Kết quả test"),
    ("cause", "❌ FAIL - Nhập failure_cause"),
    ("disp", "Nhập disposition"),
    ("after", "Kết quả sau khi sửa"),
    ("after_photo", "📷 Gửi ảnh SAU KHI SỬA"),
    ("station_status", "Trạng thái kiểm tra vận hành tại trạm sạc"),
    ("ticket", "Nhập ticket ID"),
    ("sn", "Nhập SN"),
    ("date", "Nhập Date onsite"),
    ("phen", "Nhập Original phenomenon"),
    ("station", "Nhập Station code"),
    ("verify", "📷 Gửi ảnh Failure verification photo"),
)
# Tin thông báo, không cần làm gì.
INFO = ("✅ Sẽ tạo mã thiết bị mới", "ℹ️ Chưa có thiết bị nào", "✅ Đã bỏ trống nhà thầu", "✅ Đã chọn nhà thầu",
        "ℹ️ Đã bỏ qua ticket", "⚠️ Không tìm thấy thông tin ticket", "⚠️ Có lỗi với phiên upload ảnh")


def classify(text: str) -> str | None:
    t = text.lstrip()
    for key, prefix in PROMPTS:
        if t.startswith(prefix):
            return key
    if t.startswith(INFO):
        return "info"
    return None


def labels(msg: dict) -> list[str]:
    return [b for row in msg.get("kb") or [] for b in row]


class Runner:
    """Chạy một RepairJob trên một driver (PlaywrightDriver hoặc bot giả lập khi test)."""

    def __init__(self, driver, job: RepairJob, progress: Callable[[dict], None] | None = None,
                 log: Callable[[str], None] | None = None, timeout: float = 45, poll: float = 0.7,
                 stop: Callable[[], bool] | None = None):
        self.d, self.job = driver, job
        self.progress = progress or (lambda e: None)
        self.log = log or (lambda s: None)
        self.should_stop = stop or (lambda: False)
        self.timeout, self.poll = timeout, poll
        self.steps = plan_steps(job)
        self.done_steps: set[str] = set()
        self.final = False  # đã thao tác ở bước cuối → có thể đã tạo record
        self.pending: list[str] = []
        self.photo_kind = ""

    # --- tiện ích ---
    def check_stop(self) -> None:
        """Gọi trước mỗi thao tác trên bot: dừng trước bước cuối thì chưa có record nào được tạo."""
        if self.should_stop():
            if self.final:
                raise Stopped("⛔ Đã dừng khẩn cấp SAU bước cuối — có thể bot ĐÃ tạo Repair. Kiểm tra chat bot.", True)
            raise Stopped("⛔ Đã dừng khẩn cấp — đã hủy phiên trên bot, chưa tạo Repair.")

    def report(self, key: str, detail: str = "") -> None:
        keys = [k for k, _ in self.steps]
        n = keys.index(key) + 1 if key in keys else len(self.done_steps)
        name = dict(self.steps).get(key, key)
        self.progress({"step": key, "label": name, "n": n, "total": len(self.steps), "detail": detail})
        self.log(f"[{n}/{len(self.steps)}] {name}{': ' + detail if detail else ''}")

    def click(self, msg: dict, label: str) -> None:
        if label not in labels(msg):
            raise BotError(f"Không thấy nút '{label}' (bot đưa ra: {', '.join(labels(msg)) or 'không có nút'}).", self.final)
        self.d.click(msg["mid"], label)

    def send_photo(self) -> None:
        self.check_stop()
        path = self.pending.pop(0)
        self.log(f"Gửi ảnh {self.photo_kind}: {Path(path).name}")
        self.d.send_photo(path)

    def text_or_skip(self, msg: dict, value: str, skip: str) -> str:
        if value:
            self.d.send_text(value)
            return value
        self.click(msg, skip)
        return skip

    # --- xử lý từng bước ---
    def handle(self, key: str, msg: dict) -> str | None:
        j = self.job
        if key == "menu":
            if "menu" in self.done_steps:
                return None  # bot trả lời lệnh /repair cũ: bỏ qua
            self.click(msg, "➕ Thêm mới")
            return "➕ Thêm mới"
        if key == "board":
            want = j.v("board")
            code = board_code(want)
            label = next((b for b in labels(msg) if b == want), None) or next((b for b in labels(msg) if board_code(b) == code), None)
            if not label:
                raise BotError(f"Bot không có board '{want}' trong danh sách.")
            self.click(msg, label)
            return label
        if key == "device":
            self.click(msg, NEW_DEVICE)
            return NEW_DEVICE
        if key == "contractor":
            label = j.v("contractor") or NO_CONTRACTOR
            self.click(msg, label)
            return label
        if key == "before":
            self.pending, self.photo_kind = list(j.photos_before[:2]), "trước khi test"
            self.send_photo()
            return f"{len(self.pending) + 1} ảnh"
        if key == "test":
            label = RESULT_BTN[j.result]
            self.click(msg, label)
            return label
        if key in ("cause", "disp"):
            field_ = "failure_cause" if key == "cause" else "disposition"
            value = bot_text(j.v(field_))
            if not value:  # vd chọn PASS/Discard mà bot vẫn hỏi: không gửi chữ rỗng
                raise BotError(f"Bot hỏi {field_} nhưng ô {field_} đang trống (kết quả test: {j.result}).")
            self.d.send_text(value)
            return value
        if key == "after":
            label = RESULT_BTN[j.v("after_repair_result").upper()]
            self.click(msg, label)
            return label
        if key == "after_photo":
            self.pending, self.photo_kind = list(j.photos_after[:3]), "sau khi sửa"
            if self.pending:
                self.send_photo()
                return f"{len(self.pending) + 1} ảnh"
            self.click(msg, DONE_PHOTOS)
            return "không có ảnh"
        if key == "station_status":
            label = STATION_BTN[j.v("charging_station_test_status").upper()]
            self.click(msg, label)
            return label
        if key == "ticket":
            return self.text_or_skip(msg, bot_text(j.v("ticket_id")), NO_TICKET)
        if key == "sn":
            return self.text_or_skip(msg, bot_text(j.v("sn")), SKIP)
        if key == "date":
            return self.text_or_skip(msg, bot_date(j.v("date_onsite"), j.date_format), SKIP)
        if key == "phen":
            return self.text_or_skip(msg, bot_text(j.v("original_phenomenon")), SKIP)
        if key == "station":
            return self.text_or_skip(msg, bot_text(j.v("station_code")), SKIP)
        if key == "verify":
            self.final = True
            self.pending, self.photo_kind = list(j.photos_verify[:1]), "xác minh lỗi"
            if self.pending:
                self.send_photo()
                return "gửi ảnh"
            self.click(msg, SKIP)
            return SKIP
        raise BotError(f"Bước chưa hỗ trợ: {key}", self.final)

    def on_photo_ack(self, msg: dict) -> None:
        if self.pending:
            self.send_photo()
        elif DONE_PHOTOS in labels(msg):
            self.click(msg, DONE_PHOTOS)
        # không có nút "Xong": bot tự sang bước sau khi đủ ảnh

    def cancel(self) -> None:
        try:
            last = self.d.last_bot_message()
            if last and CANCEL in labels(last):
                self.d.click(last["mid"], CANCEL)
            else:
                self.d.send_text("/cancel")
            self.log("Đã gửi lệnh hủy phiên trên bot.")
        except Exception as e:  # noqa: BLE001
            self.log(f"Không hủy được phiên trên bot: {e}")

    # --- vòng chính ---
    def run(self) -> dict:
        errs = validate(self.job)
        if errs:
            raise BotError(" ".join(errs))
        self.check_stop()
        self.d.open_chat()
        cursor = self.d.last_mid()
        self.log(f"Bắt đầu, tin cuối trong chat: {cursor}")
        self.check_stop()
        self.d.send_text("/repair")
        self.report("menu", "/repair")
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                self.check_stop()
                msgs = [m for m in self.d.messages_after(cursor) if not m["out"]]
                if not msgs:
                    if time.monotonic() > deadline:
                        if self.final:
                            raise BotError("Bot không trả lời sau bước cuối — có thể ĐÃ tạo record. Kiểm tra chat trước khi nhập lại.", True)
                        raise BotError(f"Bot không phản hồi sau {int(self.timeout)} giây.")
                    time.sleep(self.poll)
                    continue
                for msg in msgs:
                    cursor = max(cursor, msg["mid"])
                    key = classify(msg["text"])
                    self.log(f"Bot #{msg['mid']}: {msg['text'].splitlines()[0][:80]}")
                    if key == "created":
                        res = parse_created(msg["text"])
                        res.update(message_id=msg["mid"], text=msg["text"])
                        self.report("verify", f"Repair #{res['repair_id']} · {res['device_code'] or 'không có mã thiết bị'}")
                        return res
                    if key in ("expired", "admin", "cancelled"):
                        raise BotError(msg["text"].splitlines()[0], self.final)
                    if key in (None, "info"):
                        if key is None and labels(msg):
                            raise BotError(f"Bot hiện menu lạ: '{msg['text'][:120]}'", self.final)
                        continue
                    self.check_stop()
                    if key == "photo_ack":
                        self.on_photo_ack(msg)
                        continue
                    if key in self.done_steps and key != "menu":
                        raise BotError(f"Bot hỏi lại bước '{dict(self.steps).get(key, key)}' — giá trị có thể không hợp lệ.", self.final)
                    detail = self.handle(key, msg)
                    if detail is None:
                        continue
                    self.done_steps.add(key)
                    self.report(key, detail)
                deadline = time.monotonic() + self.timeout
        except BotError as e:
            if not e.uncertain:
                self.cancel()
            raise
        except Exception as e:  # noqa: BLE001 - lỗi trình duyệt/Playwright
            if not self.final:
                self.cancel()
            raise BotError(f"Lỗi điều khiển Telegram Web: {e}", self.final) from e


# ---------- Driver: Telegram Web K trong Edge (CDP) ----------

JS_BUBBLES = r"""(after) => {
  const t = el => { if (!el) return ''; const c = el.cloneNode(true);
    c.querySelectorAll('img.emoji').forEach(i => i.replaceWith(i.alt));
    c.querySelectorAll('.time, .reply-markup').forEach(x => x.remove());
    return c.innerText.trim(); };
  return [...document.querySelectorAll('.bubble[data-mid]')].filter(b => +b.dataset.mid > after).map(b => ({
    mid: +b.dataset.mid, out: b.classList.contains('is-out'),
    text: t(b.querySelector('.message')),
    kb: [...b.querySelectorAll('.reply-markup-row')].map(r => [...r.querySelectorAll('.reply-markup-button')].map(x => t(x)))
  })).sort((a, b) => a.mid - b.mid);
}"""


def cdp_status(cdp: str = DEFAULT_CDP) -> dict:
    """Edge có mở cổng debug không, có tab Telegram không. Không cần Playwright."""
    try:
        with urllib.request.urlopen(cdp.rstrip("/") + "/json/list", timeout=2) as f:
            tabs = json.loads(f.read().decode("utf-8"))
    except OSError:
        return {"edge": False, "telegram": False}
    tg = [t for t in tabs if t.get("type") == "page" and "web.telegram.org" in t.get("url", "")]
    return {"edge": True, "telegram": bool(tg), "url": tg[0]["url"] if tg else ""}


TG_URL = f"https://web.telegram.org/k/#{BOT_PEER_ID}"
LOGIN_MSG = ("Telegram Web trong cửa sổ Edge của bot chưa đăng nhập: mở Telegram trên điện thoại → Cài đặt → Thiết bị → "
             "Liên kết thiết bị, quét mã QR trong cửa sổ Edge đó (chỉ cần một lần), rồi bấm lại.")


def open_telegram_tab(cdp: str = DEFAULT_CDP) -> bool:
    """Edge (cổng debug) đang mở nhưng không có tab Telegram: mở tab mới qua CDP."""
    try:
        req = urllib.request.Request(cdp.rstrip("/") + "/json/new?" + TG_URL, method="PUT")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except OSError:
        return False


def ensure_telegram(cdp: str = DEFAULT_CDP, wait: float = 25, log: Callable[[str], None] | None = None) -> dict:
    """Bảo đảm có Edge (profile riêng, cổng debug) với tab Telegram Web: tự mở Edge / tab nếu thiếu."""
    log = log or (lambda s: None)
    st = cdp_status(cdp)
    if st["telegram"]:
        return {**st, "launched": False}
    if st["edge"]:
        log("Edge đang mở nhưng chưa có tab Telegram — mở tab Telegram Web…")
        open_telegram_tab(cdp)
    else:
        edge = find_edge()
        if not edge:
            raise BotError("Không tìm thấy msedge.exe trên máy chạy server.")
        log("Chưa mở Edge cho bot — đang mở Edge (profile riêng) với Telegram Web…")
        import subprocess
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(edge_command(edge, cdp), close_fds=True)
    end = time.monotonic() + wait
    while time.monotonic() < end:
        time.sleep(0.5)
        st = cdp_status(cdp)
        if st["telegram"]:
            return {**st, "launched": True}
    raise BotError("Đã mở Edge nhưng không thấy tab Telegram Web sau %d giây." % wait)


LOGIN_JS = r"""() => {
  if (document.querySelector('a.chatlist-chat')) return 'ready';
  const t = document.body ? document.body.innerText : '';
  if (/Log in by QR|LOG IN BY PHONE|Quét mã QR|Sign in to Telegram|Your Phone|Log in to Telegram/i.test(t)) return 'login';
  return 'loading';
}"""


def page_state(page, wait: float = 20) -> str:
    """'ready' (đã đăng nhập, có danh sách chat) | 'login' (cần quét QR) | 'loading'."""
    end = time.monotonic() + wait
    state = "loading"
    while time.monotonic() < end:
        try:
            state = page.evaluate(LOGIN_JS)
        except Exception:  # noqa: BLE001 - trang đang tải lại
            state = "loading"
        if state != "loading":
            return state
        time.sleep(0.5)
    return state


def check_login(cdp: str = DEFAULT_CDP, wait: float = 20) -> str:
    """Kết nối tạm vào Edge để xem Telegram Web đã đăng nhập chưa."""
    d = PlaywrightDriver(cdp, check_login=False)
    try:
        return page_state(d.page, wait)
    finally:
        d.close()


def find_edge() -> str:
    cands = [shutil.which("msedge") or ""]
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            cands.append(str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as k:
            cands.insert(0, winreg.QueryValue(k, None))
    except OSError:
        pass
    return next((c for c in cands if c and Path(c).is_file()), "")


def edge_command(edge: str, cdp: str = DEFAULT_CDP) -> list[str]:
    port = cdp.rsplit(":", 1)[-1].strip("/")
    return [edge, f"--user-data-dir={PROFILE_DIR}", f"--remote-debugging-port={port}", "--no-first-run",
            "--new-window", TG_URL]


class PlaywrightDriver:
    """Điều khiển tab Telegram Web. Chỉ dùng trong MỘT luồng (Playwright sync)."""

    INPUT = '.input-message-input[contenteditable="true"]'

    def __init__(self, cdp: str = DEFAULT_CDP, check_login: bool = True):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise BotError("Chưa cài Playwright: chạy  pip install playwright") from e
        self._pw = sync_playwright().start()
        try:
            self.browser = self._pw.chromium.connect_over_cdp(cdp)
        except Exception as e:  # noqa: BLE001
            self._pw.stop()
            raise BotError("Không kết nối được Edge (cổng debug). Bấm 'Mở Telegram' trên web để mở Edge riêng cho bot.") from e
        self.page = next((p for c in self.browser.contexts for p in c.pages if "web.telegram.org" in p.url), None)
        if not self.page:
            self.close()
            raise BotError("Edge đang mở nhưng không có tab web.telegram.org.")
        if check_login:
            state = page_state(self.page, 30)
            if state != "ready":
                self.close()
                raise BotError(LOGIN_MSG if state == "login" else "Telegram Web chưa tải xong sau 30 giây — kiểm tra mạng rồi thử lại.")

    def close(self) -> None:
        try:
            self._pw.stop()  # chỉ ngắt kết nối, không đóng Edge
        except Exception:  # noqa: BLE001
            pass

    def open_chat(self) -> None:
        p = self.page
        if p.locator('.chat-info [data-peer-id="%s"], .topbar [data-peer-id="%s"]' % (BOT_PEER_ID, BOT_PEER_ID)).count() == 0 \
                or p.locator(".bubble[data-mid]").count() == 0:
            item = p.locator(f'a.chatlist-chat[data-peer-id="{BOT_PEER_ID}"]').first
            if item.count() == 0:
                p.goto(f"https://web.telegram.org/k/#{BOT_PEER_ID}")
                p.wait_for_selector(f'a.chatlist-chat[data-peer-id="{BOT_PEER_ID}"]', timeout=30000)
            item.click()
            p.wait_for_selector(".bubble[data-mid]", timeout=20000)
        self.to_bottom()

    def to_bottom(self) -> None:
        down = self.page.locator(".bubbles-go-down:not(.is-hidden)")
        if down.count() and down.first.is_visible():
            down.first.click()
            self.page.wait_for_timeout(800)
        self.page.evaluate("() => { const s = document.querySelector('#column-center .bubbles .scrollable'); if (s) s.scrollTop = s.scrollHeight; }")

    def messages_after(self, mid: int) -> list[dict]:
        self.to_bottom()
        return self.page.evaluate(JS_BUBBLES, mid)

    def last_mid(self) -> int:
        msgs = self.page.evaluate(JS_BUBBLES, 0)
        return max((m["mid"] for m in msgs), default=0)

    def last_bot_message(self) -> dict | None:
        msgs = [m for m in self.page.evaluate(JS_BUBBLES, 0) if not m["out"]]
        return msgs[-1] if msgs else None

    def click(self, mid: int, label: str) -> None:
        btns = self.page.locator(f'.bubble[data-mid="{mid}"] .reply-markup-button')
        for i in range(btns.count()):
            b = btns.nth(i)
            if b.evaluate("x => { const c = x.cloneNode(true); c.querySelectorAll('img.emoji').forEach(i => i.replaceWith(i.alt)); return c.innerText.trim(); }") == label:
                b.click()
                return
        raise BotError(f"Không bấm được nút '{label}' ở tin #{mid}.")

    def send_text(self, text: str) -> None:
        box = self.page.locator(self.INPUT).first
        box.click()
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Delete")
        self.page.keyboard.insert_text(text)
        self.page.keyboard.press("Enter")
        self.page.wait_for_timeout(400)
        if box.inner_text().strip():  # gợi ý lệnh "/..." có thể giữ lại chữ trong ô
            box.click()
            self.page.keyboard.press("Control+A")
            self.page.keyboard.press("Delete")

    def send_photo(self, path: str) -> None:
        """📎 → Photo or Video → chọn file → Send (gửi dạng ảnh nén như trên điện thoại)."""
        p = self.page
        p.locator("attach-menu-button.attach-file").first.click()
        p.wait_for_timeout(500)
        with p.expect_file_chooser(timeout=8000) as fc:
            p.locator(".btn-menu-item", has_text=re.compile(r"Photo|Ảnh")).first.click()
        fc.value.set_files(path)
        popup = p.locator(".popup-new-media").first
        popup.wait_for(state="visible", timeout=15000)
        send = popup.locator("button", has_text=re.compile(r"^\s*(Send|Gửi)\s*$")).first
        if send.count():
            send.click()
        else:
            p.keyboard.press("Enter")
        popup.wait_for(state="hidden", timeout=30000)
