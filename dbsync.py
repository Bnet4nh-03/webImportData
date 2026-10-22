"""Cập nhật ccts_tickets.db từ web CNPowercore bằng tool có sẵn (D:\\00_WORKSPACE\\01_Python\\04_Takedata_CCVN).

Server chạy `python main.py --db <DB> sync …` của tool đó như một tiến trình con, truyền cookie `ssoticket` + API token
qua biến môi trường CCTS_SSOTICKET / CCTS_API_TOKEN (tool ưu tiên biến môi trường hơn credentials.json), nên không
cần sửa hay ghi file của tool. Token không bao giờ được in ra log / gửi về trình duyệt.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

DEFAULT_TOOL_DIR = r"D:\00_WORKSPACE\01_Python\04_Takedata_CCVN"
MODES = {
    "new": ("Lấy ticket mới", ["sync", "--new"]),         # hằng ngày: chỉ ticket mới ở đầu danh sách
    "auto": ("Tiếp tục đồng bộ", ["sync"]),               # tiếp tục từ checkpoint lần trước
    "full": ("Quét lại toàn bộ", ["sync", "--full"]),     # quét từ trang 1 (rất lâu)
    "probe": ("Kiểm tra token", ["probe"]),               # 4 request, không ghi DB
}
RESULT_RE = re.compile(r"KẾT QUẢ ĐỒNG BỘ - trạng thái: (\w+)(?: \((.*)\))?")
PAGE_RE = re.compile(r"Trang (\d[\d,]*)/(\d[\d,]*) \| nhận (\d+) \| mới (\d+) \| cập nhật (\d+) \| DB ([\d,]+)")
STATUS_TEXT = {
    "completed": "Xong", "stopped_limit": "Dừng theo giới hạn", "auth_error": "Token / cookie hết hạn hoặc sai",
    "error": "Lỗi", "interrupted": "Đã dừng", "stopped_incremental": "Xong (đã hết ticket mới)",
}


def db_info(db_path: str) -> dict:
    """Số ticket + ticket mới nhất trong DB (đọc nhanh nhờ index create_time_norm)."""
    p = Path(db_path)
    if not p.exists():
        return {"ok": False}
    try:
        with sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5) as con:
            newest = con.execute("SELECT MAX(create_time_norm) FROM tickets").fetchone()[0]
            count = con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}
    wal = p.with_name(p.name + "-wal")
    mtime = max(p.stat().st_mtime, wal.stat().st_mtime if wal.exists() else 0)
    return {"ok": True, "count": count, "newest": newest or "", "mtime": int(mtime * 1000)}


class DbSync:
    """Một lần đồng bộ tại một thời điểm; đẩy tiến độ qua `emit(dict)`."""

    def __init__(self, emit: Callable[[dict], None], on_done: Callable[[str], None] | None = None):
        self.emit = emit
        self.on_done = on_done or (lambda status: None)
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.state: dict = {"running": False, "mode": "", "lines": [], "status": "", "message": "", "progress": None}
        self._lines: deque[str] = deque(maxlen=60)

    def snapshot(self) -> dict:
        with self.lock:
            return {**self.state, "lines": list(self._lines)}

    def _push(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)
        self.emit({"type": "dbsync", **self.snapshot()})

    def start(self, mode: str, sso: str, token: str, db_path: str, tool_dir: str, page_size: int, delay: float,
              login: Callable[[], tuple[str, str]] | None = None) -> None:
        """login: hàm tự đăng nhập -> (ssoticket, token), chạy ngầm trước khi tải (thay cho sso/token chép tay)."""
        if mode not in MODES:
            raise ValueError("Chế độ đồng bộ không hợp lệ.")
        if not login and (not sso or not token):
            raise ValueError("Chưa nhập tài khoản CNPowercore hoặc cookie ssoticket + API token (⚙️ → Đồng bộ DB).")
        tool = Path(tool_dir or DEFAULT_TOOL_DIR)
        if not (tool / "main.py").exists():
            raise ValueError(f"Không thấy tool lấy dữ liệu (main.py) trong {tool}.")
        with self.lock:
            if self.state.get("running"):
                raise ValueError("Đang đồng bộ — chờ xong hoặc bấm Dừng.")
            label, args = MODES[mode]
            cmd = [sys.executable, "-u", "main.py", "--db", str(db_path), *args, "--page-size", str(int(page_size))]
            if mode != "probe":
                cmd += ["--delay", str(float(delay))]
            self._lines.clear()
            self.proc = None
            self.state.update(running=True, mode=mode, label=label, status="", message="", progress=None,
                              started=int(time.time() * 1000), ended=None, stopping=False)
        self._line(f"▶ {label} (pageSize {page_size}{'' if mode == 'probe' else f', nghỉ {delay:g}s giữa các trang'})")
        threading.Thread(target=self._run, args=(cmd, tool, sso, token, login), daemon=True).start()

    def _run(self, cmd: list[str], tool: Path, sso: str, token: str, login) -> None:
        if login:
            self._line("🔑 Tự đăng nhập CNPowercore…")
            try:
                sso, token = login()
            except Exception as e:  # noqa: BLE001 - LoginError hoặc lỗi giải mã mật khẩu
                self._line(f"✖ {e}")
                return self._finish("auth_error", str(e), None)
            self._line("✔ Đăng nhập xong — đã lấy cookie ssoticket + token mới.")
        env = {**os.environ, "CCTS_SSOTICKET": sso, "CCTS_API_TOKEN": token, "PYTHONIOENCODING": "utf-8",
               "PYTHONUNBUFFERED": "1"}
        with self.lock:
            if self.state.get("stopping"):
                stopped = True
            else:
                stopped = False
                self.proc = subprocess.Popen(cmd, cwd=str(tool), env=env, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                             encoding="utf-8", errors="replace",
                                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if stopped:
            return self._finish("interrupted", "", None)
        self._read()

    def stop(self) -> bool:
        with self.lock:
            if not self.state.get("running"):
                return False
            self.state["stopping"] = True
            p = self.proc
        if p and p.poll() is None:
            p.terminate()  # trang đã ghi vẫn giữ (tool commit từng trang + checkpoint); lần sau "Tiếp tục" chạy tiếp
        self._line("⏹ Đã yêu cầu dừng.")
        return True

    def _line(self, text: str) -> None:
        text = re.sub(r"^\d{4}-\d\d-\d\d (\d\d:\d\d:\d\d),\d+ \[\w+\] ", r"\1 ", text.rstrip())
        if not text:
            return
        with self.lock:
            self._lines.append(text)
        m = PAGE_RE.search(text)
        if m:
            n = lambda s: int(s.replace(",", ""))  # noqa: E731
            self._push(progress={"page": n(m[1]), "pages": n(m[2]), "new": n(m[4]), "db": n(m[6])})
        else:
            self.emit({"type": "dbsync", **self.snapshot()})

    def _read(self) -> None:
        p = self.proc
        status, message = "", ""
        for raw in p.stdout:
            self._line(raw)
            m = RESULT_RE.search(raw)
            if m:
                status, message = m[1], m[2] or ""
            if "LỖI XÁC THỰC" in raw or "AuthError" in raw:
                status = status or "auth_error"
        code = p.wait()
        if not status:
            status = "interrupted" if self.state.get("stopping") else "completed" if code == 0 else "error"
        self._finish(status, message, code)

    def _finish(self, status: str, message: str, code: int | None) -> None:
        tail = "" if code is None else f" (mã thoát {code})"
        self._line(f"■ {STATUS_TEXT.get(status, status)}{f' — {message}' if message else ''}{tail}")
        self._push(running=False, status=status, message=message, ended=int(time.time() * 1000))
        try:
            self.on_done(status)
        except Exception:  # noqa: BLE001
            pass
