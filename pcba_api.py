"""Nhập bo mạch lên hệ thống PCBA Team (https://pcba.pcba-api.workers.dev/intake) — thay cho bot Telegram.

Gọi đúng các API mà trang /intake gọi (đọc từ mã JS công khai của trang, assets/IntakeView-*.js):
  POST /api/auth/login {username, password}                     -> cookie phiên
  GET  /api/board-models, /api/contractors                      -> danh mục (id, code, name)
  GET  /api/boards/by-sn/{SN}                                    -> {board, openTicket, history} ("Check SN")
  POST /api/tickets {boardModelId, sn, snSource, qrRaw, snMissingReason, contractorId, dateReceive,
                     ccvnTicketId, stationCode, dateOnsite, originalPhenomenon, detailedRemarks}  -> {id, ticketNo, ticket}
  POST /api/tickets/{id}/attachments {category, mimeType, fullSize, thumbSize, width, height, originalName}
       -> {attachment, upload:{fullUrl, thumbUrl, contentType}}; PUT 2 ảnh; POST /api/attachments/{id}/confirm
  POST /api/tickets/{id}/transitions {version, event: initial_test, result: PASS|FAIL|DISCARD, reason}
  PATCH /api/tickets/{id} {version, failureCause, dispositionType, dispositionNote}          (FAIL)
  POST /api/tickets/{id}/transitions {version, event: retest, result, chargeTestResult}      (FAIL -> sửa xong)
  POST /api/tickets/{id}/transitions {version, event: station_test, result}                  (retest PASS)
Mỗi bước tạo dữ liệu gửi kèm Idempotency-Key cố định của dòng: nhập lại sau lỗi không tạo phiếu trùng, và
các bước đã xong (lưu trong `progress`) được bỏ qua — giống nút Lưu trên web khi lưu dở.
"""

from __future__ import annotations

import io
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import matching as M

BASE = "https://pcba.pcba-api.workers.dev"
SN_REASONS = {  # nhãn tiếng Việt trên web -> mã API
    "Không có tem": "NO_LABEL",
    "Tem hỏng": "LABEL_DAMAGED",
    "Tem không đọc được": "UNREADABLE",
    "Lý do khác": "OTHER",
}
RESULTS = {"PASS": "PASS", "FAIL": "FAIL", "DISCARD": "DISCARD"}
STATION = {"OK": "OK", "FAIL": "FAIL", "NOT USED": "NOT_USED", "NOT TESTED": "NOT_TESTED"}  # Pending: để trống
# Loại xử lý (trang web bỏ DISCARD khỏi danh sách này: Discard đi theo kết quả kiểm tra ban đầu).
DISPOSITIONS = {"SỬA CHỮA": "REPAIR", "REPAIR": "REPAIR", "KHÔNG SỬA CHỮA": "NO_REPAIR", "NO REPAIR": "NO_REPAIR",
                "NO_REPAIR": "NO_REPAIR"}
# Hành động cho từng vị trí đã xử lý (U12, C46…). Chữ viết tắt hay gõ: "thay", "tháo", "sửa".
ACTIONS = (("THAY THẾ", "REPLACE"), ("THÁO BỎ", "REMOVE"), ("SỬA CHỮA", "REPAIR"), ("REPLACE", "REPLACE"),
           ("REMOVE", "REMOVE"), ("REPAIR", "REPAIR"), ("THAY", "REPLACE"), ("THÁO", "REMOVE"), ("SỬA", "REPAIR"))
ACTION_LABEL = {"REPAIR": "Sửa chữa", "REPLACE": "Thay thế", "REMOVE": "Tháo bỏ"}


def parse_components(text: str) -> list[dict]:
    """"U12, C46 Thay thế; R5 Tháo bỏ; Q3" -> [{ref, action, note}]: mỗi đoạn (ngăn bởi ; hoặc xuống dòng) là các vị trí
    + một hành động (không ghi = Sửa chữa). Vị trí tách như trang web: dấu cách / phẩy / chấm phẩy, viết hoa, bỏ trùng."""
    out: list[dict] = []
    for seg in re.split(r"[;\n]+", text or ""):
        s = " " + seg.upper() + " "
        action = "REPAIR"
        for word, code in ACTIONS:
            m = re.search(rf"(?<![\w]){re.escape(word)}(?![\w])", s)
            if m:
                action = code
                s = s[:m.start()] + " " + s[m.end():]
                break
        s = re.sub(r"[()\[\]:]", " ", s)
        for ref in re.split(r"[\s,;]+", s):
            ref = ref.strip()
            if ref and not any(c["ref"] == ref and c["action"] == action for c in out):
                out.append({"ref": ref, "action": action, "note": None})
    return out


def format_components(items: list[dict]) -> str:
    groups: dict[str, list[str]] = {}
    for c in items:
        groups.setdefault(c["action"], []).append(c["ref"])
    return "; ".join(f"{', '.join(refs)} {ACTION_LABEL.get(a, a)}" for a, refs in groups.items())


PHOTO_LIMITS = {"BEFORE_TEST": 2, "AFTER_REPAIR": 3, "FAILURE_VERIFICATION": 3}
PHOTO_DEFAULT = {"fullMaxEdge": 2560, "fullQuality": 0.85, "thumbMaxEdge": 400, "thumbQuality": 0.7}


class PcbaError(Exception):
    def __init__(self, msg: str, status: int = 0, code: str = ""):
        super().__init__(msg)
        self.status, self.code = status, code


class Stopped(PcbaError):
    pass


def norm_sn(raw: str | None) -> str | None:
    """Như hàm chuẩn hoá SN của web: bỏ ký tự điều khiển, gộp khoảng trắng, viết hoa; >100 ký tự = lỗi."""
    s = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", raw or "")).strip().upper()
    if not s:
        return None
    if len(s) > 100:
        raise PcbaError("SN dài quá 100 ký tự.")
    return s


def today_vn() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=7)).strftime("%Y-%m-%d")


def code_of(label: str) -> str:
    return (label or "").split(" - ", 1)[0].strip()


def _key(s: str) -> str:
    return re.sub(r"[\s_\-.]+", "", (s or "").upper())


@dataclass
class Job:
    values: dict
    photos_before: list[str] = field(default_factory=list)
    photos_after: list[str] = field(default_factory=list)
    photos_verify: list[str] = field(default_factory=list)
    qr_raw: str | None = None
    sn_source: str = "MANUAL"  # QR khi SN lấy từ quét QR
    date_format: str = "mdy"

    def v(self, key: str) -> str:
        return str(self.values.get(key) or "").strip()

    @property
    def result(self) -> str:
        return RESULTS.get(self.v("test_tool_result").upper(), "")

    @property
    def board_sn(self) -> str | None:
        return norm_sn(self.v("board_sn"))

    @property
    def sn_reason(self) -> str | None:
        r = self.v("sn_missing_reason")
        return SN_REASONS.get(r) or (r if r in SN_REASONS.values() else None)

    @property
    def retest(self) -> str:
        return {"PASS": "PASS", "FAIL": "FAIL"}.get(self.v("after_repair_result").upper(), "")

    @property
    def station(self) -> str:
        return STATION.get(self.v("charging_station_test_status").upper().replace("_", " "), "")

    @property
    def discard_reason(self) -> str:
        return self.v("disposition") or self.v("failure_cause")

    @property
    def disposition_type(self) -> str:
        return DISPOSITIONS.get(self.v("disposition_type").upper(), "REPAIR")  # trống = Sửa chữa (mặc định của web)

    @property
    def components(self) -> list[dict]:
        return parse_components(self.v("components"))

    @property
    def has_repair(self) -> bool:
        """Có thông tin sửa chữa để gửi (như điều kiện trên web: nguyên nhân, ghi chú hoặc vị trí)."""
        return bool(self.v("failure_cause") or self.v("disposition") or self.components)


def validate(job: Job) -> list[str]:
    err = []
    if not job.v("board"):
        err.append("Chưa chọn Board.")
    try:
        sn = job.board_sn
    except PcbaError as e:
        sn, _ = None, err.append(str(e))
    if not sn and not job.sn_reason:
        err.append("Cần Check SN (quét QR / nhập SN bo mạch) hoặc chọn Lý do không có SN.")
    if not job.result:
        err.append("test_tool_result phải là PASS, FAIL hoặc Discard.")
    if not job.photos_before:
        err.append("Cần ít nhất 1 ảnh TRƯỚC KHI TEST (ảnh tem của dòng).")
    if job.result == "DISCARD" and not job.discard_reason:
        err.append("Discard cần lý do (ghi vào cột disposition hoặc failure_cause).")
    if job.result == "FAIL" and job.retest == "PASS" and not job.v("failure_cause"):
        err.append("Kiểm tra lại PASS sẽ đóng phiếu: cần nhập failure_cause.")
    if job.v("date_onsite") and not M.parse_day(job.v("date_onsite"), job.date_format):
        err.append(f"Date onsite '{job.v('date_onsite')}' không đọc được.")
    for p in job.photos_before + job.photos_after + job.photos_verify:
        if not Path(p).is_file():
            err.append(f"Thiếu file ảnh {Path(p).name}.")
    return err


def plan_steps(job: Job) -> list[tuple[str, str]]:
    s = [("login", "Đăng nhập"), ("lookup", "Check SN"), ("create", "Tạo phiếu"),
         ("photo_before", "Ảnh trước kiểm tra"), ("photo_verify", "Ảnh xác minh lỗi"), ("initial", "Kiểm tra ban đầu")]
    if job.result == "FAIL":
        s += [("repair", "Nguyên nhân + xử lý"), ("photo_after", "Ảnh sau sửa")]
        if job.retest:
            s.append(("retest", "Kiểm tra lại"))
        if job.retest == "PASS" and job.station:
            s.append(("station", "Kiểm tra tại trạm"))
    return s


def preview(job: Job, cat: "Catalog | None" = None) -> list[dict]:
    n = lambda xs, mx: f"{min(len(xs), mx)} ảnh" if xs else "—"  # noqa: E731
    vals = {
        "login": "tài khoản đã lưu", "lookup": job.v("board_sn") or f"Không có SN — {job.v('sn_missing_reason')}",
        "create": " · ".join(x for x in (job.v("board"), job.v("contractor"), job.v("ticket_id")) if x),
        "photo_before": n(job.photos_before, 2), "photo_verify": n(job.photos_verify, 3),
        "initial": job.result + (f" — {job.discard_reason}" if job.result == "DISCARD" else ""),
        "repair": " / ".join(x for x in (job.v("failure_cause"), job.v("disposition_type") or "Sửa chữa",
                                         job.v("disposition"), format_components(job.components)) if x) or "—",
        "photo_after": n(job.photos_after, 3), "retest": job.retest, "station": job.station,
    }
    return [{"key": k, "label": label, "value": vals.get(k, "")} for k, label in plan_steps(job)]


# ---------- HTTP ----------
class Client:
    def __init__(self, username: str, password: str, base: str = BASE, timeout: float = 30, session=None):
        import requests

        self.base, self.timeout = base.rstrip("/"), timeout
        self.username, self.password = (username or "").strip(), password or ""
        self.s = session or requests.Session()
        # Hệ thống chặn yêu cầu không có nguồn gửi (HTTP 403 "nguồn gửi không hợp lệ"): gửi Origin/Referer của
        # chính trang /intake như trình duyệt vẫn tự gửi khi đăng nhập trên web.
        self.s.headers.update({"Accept": "application/json", "Accept-Language": "vi", "Origin": self.base,
                               "Referer": f"{self.base}/intake"})
        self.logged_in = False

    def login(self) -> dict:
        if not self.username or not self.password:
            raise PcbaError("Chưa nhập tài khoản / mật khẩu hệ thống PCBA (⚙️ → Nhập PCBA).")
        res = self._raw("POST", "/auth/login", {"username": self.username, "password": self.password})
        self.logged_in = True
        return res

    def _raw(self, method: str, path: str, body=None, idem: str | None = None):
        import requests

        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if idem:
            headers["Idempotency-Key"] = idem
        try:
            r = self.s.request(method, f"{self.base}/api{path}", json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise PcbaError(f"Không kết nối được hệ thống PCBA: {type(e).__name__}") from None
        try:
            data = r.json()
        except ValueError:
            data = None
        if not r.ok:
            err = (data or {}).get("error") if isinstance(data, dict) else None
            if isinstance(err, dict):
                raise PcbaError(f"{err.get('message') or err.get('code')} (HTTP {r.status_code})", r.status_code,
                                str(err.get("code") or ""))
            raise PcbaError(f"Lỗi HTTP {r.status_code} từ hệ thống PCBA.", r.status_code, "HTTP_ERROR")
        return data

    def call(self, method: str, path: str, body=None, idem: str | None = None):
        if not self.logged_in:
            self.login()
        try:
            return self._raw(method, path, body, idem)
        except PcbaError as e:
            if e.status != 401:
                raise
            self.logged_in = False  # phiên hết hạn: đăng nhập lại một lần
            self.login()
            return self._raw(method, path, body, idem)

    def put_blob(self, url: str, data: bytes, content_type: str) -> None:
        import requests

        try:
            r = requests.put(url, data=data, headers={"Content-Type": content_type}, timeout=max(60, self.timeout))
        except requests.RequestException as e:
            raise PcbaError(f"Tải ảnh lên thất bại: {type(e).__name__}") from None
        if not r.ok:
            raise PcbaError(f"Tải ảnh lên thất bại (HTTP {r.status_code}).", r.status_code, "STORAGE_ERROR")


class Catalog:
    def __init__(self, models: list[dict], contractors: list[dict]):
        self.models, self.contractors = models, contractors

    @classmethod
    def load(cls, c: Client) -> "Catalog":
        return cls(c.call("GET", "/board-models").get("items") or [], c.call("GET", "/contractors").get("items") or [])

    def model_id(self, board_label: str) -> int:
        code = code_of(board_label)
        for m in self.models:
            if _key(str(m.get("code"))) == _key(code):
                return m["id"]
        raise PcbaError(f"Board '{code}' không có trong danh mục Board model của hệ thống PCBA.")

    def contractor_id(self, name: str) -> int | None:
        if not name:
            return None
        for c in self.contractors:
            if _key(str(c.get("name") or c.get("code"))) == _key(name) or _key(str(c.get("code") or "")) == _key(name):
                return c["id"]
        raise PcbaError(f"Nhà thầu '{name}' không có trong danh mục của hệ thống PCBA.")


def lookup(c: Client, sn: str) -> dict:
    """Check SN: bo mạch đã có trên hệ thống chưa, có phiếu đang mở không."""
    from urllib.parse import quote

    res = c.call("GET", f"/boards/by-sn/{quote(sn, safe='')}") or {}
    board, open_t = res.get("board"), res.get("openTicket")
    return {
        "sn": sn,
        "found": bool(board),
        "boardModelId": (board or {}).get("boardModelId"),
        "boardModelCode": (board or {}).get("boardModelCode") or "",
        "boardModelName": (board or {}).get("boardModelName") or "",
        "openTicketNo": (open_t or {}).get("ticketNo") or "",
        "openTicketId": (open_t or {}).get("id") or "",
        "history": len(res.get("history") or []),
    }


def jpeg_pair(path: str, settings: dict) -> dict:
    """Ảnh đầy đủ + ảnh nhỏ JPEG như web tạo (cạnh dài tối đa theo /settings/photo)."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")

        def enc(edge: float, q: float) -> tuple[bytes, int, int]:
            x = im.copy()
            if edge and max(x.size) > edge:
                x.thumbnail((int(edge), int(edge)))
            b = io.BytesIO()
            x.save(b, "JPEG", quality=int(round(q * 100)))
            return b.getvalue(), x.width, x.height

        full, w, h = enc(settings.get("fullMaxEdge") or 2560, settings.get("fullQuality") or 0.85)
        thumb, _, _ = enc(settings.get("thumbMaxEdge") or 400, settings.get("thumbQuality") or 0.7)
    return {"full": full, "thumb": thumb, "width": w, "height": h, "name": Path(path).name[:255]}


def upload_photo(c: Client, ticket_id, category: str, path: str, idem: str, settings: dict) -> dict:
    p = jpeg_pair(path, settings)
    res = c.call("POST", f"/tickets/{ticket_id}/attachments", {
        "category": category, "mimeType": "image/jpeg", "fullSize": len(p["full"]), "thumbSize": len(p["thumb"]),
        "width": p["width"], "height": p["height"], "originalName": p["name"]}, idem)
    att = res.get("attachment") or {}
    up = res.get("upload")
    if up:
        c.put_blob(up["fullUrl"], p["full"], up.get("contentType") or "image/jpeg")
        c.put_blob(up["thumbUrl"], p["thumb"], up.get("contentType") or "image/jpeg")
        att = (c.call("POST", f"/attachments/{att['id']}/confirm") or {}).get("attachment") or att
    return att


def submit(c: Client, job: Job, progress: dict, save: Callable[[dict], None] | None = None,
           step: Callable[[str, str], None] | None = None, stop: Callable[[], bool] = lambda: False) -> dict:
    """Chạy toàn bộ luồng nhập. `progress` (lưu trên dòng) nhớ id phiếu, version, bước đã xong và khóa
    Idempotency: gọi lại sau lỗi sẽ tiếp tục, không tạo phiếu thứ hai."""
    save = save or (lambda p: None)
    step = step or (lambda k, s: None)
    done: list[str] = progress.setdefault("done", [])
    keys: dict = progress.setdefault("keys", {})
    key = lambda name: keys.setdefault(name, str(uuid.uuid4()))  # noqa: E731

    def mark(name: str, **kw) -> None:
        progress.update(kw)
        if name not in done:
            done.append(name)
        save(progress)

    def check() -> None:
        if stop():
            raise Stopped("⛔ Đã dừng khẩn cấp." + (f" Phiếu {progress['ticketNo']} đã tạo — bấm nhập lại để làm tiếp."
                                                  if progress.get("ticketNo") else " Chưa tạo phiếu nào."))

    step("login", "Đăng nhập hệ thống PCBA")
    if not c.logged_in:
        c.login()
    check()
    cat = Catalog.load(c)
    sn = job.board_sn
    model_id = cat.model_id(job.v("board"))
    if "create" not in done:
        if sn:
            step("lookup", f"Check SN {sn}")
            info = lookup(c, sn)
            if info["openTicketNo"]:
                raise PcbaError(f"SN {sn} đang có phiếu mở {info['openTicketNo']} — cập nhật trên phiếu đó, không tạo mới.")
            if info["boardModelId"]:
                model_id = info["boardModelId"]  # bo đã có trên hệ thống: dùng đúng model đã đăng ký
        check()
        step("create", "Tạo phiếu")
        d = M.parse_day(job.v("date_onsite"), job.date_format)
        remarks = f"SN trụ sạc: {job.v('sn')}" if job.v("sn") else None
        res = c.call("POST", "/tickets", {
            "boardModelId": model_id,
            "sn": sn,
            "snSource": job.sn_source if sn else "MANUAL",
            "qrRaw": (job.qr_raw or "")[:500] or None if sn and job.sn_source == "QR" else None,
            "snMissingReason": None if sn else job.sn_reason,
            "contractorId": cat.contractor_id(job.v("contractor")),
            "dateReceive": today_vn(),
            "ccvnTicketId": job.v("ticket_id") or None,
            "stationCode": job.v("station_code") or None,
            "dateOnsite": d.isoformat() if d else None,
            "originalPhenomenon": job.v("original_phenomenon") or None,
            "detailedRemarks": remarks,
        }, key("create"))
        mark("create", id=res["id"], ticketNo=res.get("ticketNo") or "",
             version=((res.get("ticket") or {}).get("version") or 1), status=(res.get("ticket") or {}).get("status") or "")
    tid = progress["id"]
    settings = dict(PHOTO_DEFAULT)
    try:
        settings.update((c.call("GET", "/settings/photo") or {}).get("photo") or {})
    except PcbaError:
        pass

    def photos(name: str, category: str, paths: list[str]) -> None:
        for i, pth in enumerate(paths[:PHOTO_LIMITS[category]]):
            tag = f"{name}{i}"
            if tag in done:
                continue
            check()
            step(name, f"Ảnh {category} {i + 1}/{min(len(paths), PHOTO_LIMITS[category])}")
            upload_photo(c, tid, category, pth, key(tag), settings)
            mark(tag)

    def transition(name: str, body: dict) -> None:
        if name in done:
            return
        check()
        res = c.call("POST", f"/tickets/{tid}/transitions", {"version": progress["version"], **body}, key(name))
        t = res.get("ticket") or {}
        mark(name, version=t.get("version", progress["version"]), status=t.get("status", progress.get("status", "")))

    photos("photo_before", "BEFORE_TEST", job.photos_before)
    photos("photo_verify", "FAILURE_VERIFICATION", job.photos_verify)
    step("initial", f"Kiểm tra ban đầu: {job.result}")
    transition("initial", {"event": "initial_test", "result": job.result,
                           **({"reason": job.discard_reason} if job.result == "DISCARD" else {})})
    if job.result == "FAIL":
        if "repair" not in done and job.has_repair:
            check()
            step("repair", "Nguyên nhân lỗi + xử lý")
            res = c.call("PATCH", f"/tickets/{tid}", {
                "version": progress["version"], "failureCause": job.v("failure_cause"),
                "dispositionType": job.disposition_type, "dispositionNote": job.v("disposition")})
            t = res.get("ticket") or {}
            mark("repair", version=t.get("version", progress["version"]), status=t.get("status", progress.get("status", "")))
        if "components" not in done and job.components:
            check()
            step("repair", f"Vị trí đã xử lý: {format_components(job.components)}")
            res = c.call("PUT", f"/tickets/{tid}/components", {"version": progress["version"], "items": job.components})
            t = res.get("ticket") or {}
            mark("components", version=t.get("version", progress["version"]),
                 status=t.get("status", progress.get("status", "")))
        photos("photo_after", "AFTER_REPAIR", job.photos_after)
        if job.retest and progress.get("status") in ("REPAIRING", ""):
            step("retest", f"Kiểm tra lại: {job.retest}")
            transition("retest", {"event": "retest", "result": job.retest, "chargeTestResult": None})
        if job.retest == "PASS" and job.station and progress.get("status") == "CLOSED":
            step("station", f"Kiểm tra tại trạm: {job.station}")
            try:
                transition("station", {"event": "station_test", "result": job.station})
            except PcbaError as e:
                if e.status != 403:
                    raise
                progress["note"] = "Tài khoản không có quyền kiểm tra tại trạm — phiếu ở trạng thái chờ kiểm tra tại trạm."
                save(progress)
    return progress
