"""Tự đăng nhập console.cnpowercore.com để lấy cookie `ssoticket` + API `token` (thay cho F12 chép tay).

Làm đúng như trang đăng nhập (component/Login.js + js/api.js của web):
1. GET  {API}/authen/index/getPublicKey          -> khóa công khai RSA (base64, có chèn chữ "power" cần bỏ đi)
2. mật khẩu = RSA_PKCS1v15( md5_hex(mật khẩu) )   -> base64 (giống JSEncrypt)
3. POST {API}/authen/login/validate {account, password} -> data.token; server đặt cookie ssoticket (cloud.cnpowercore.com)

Mật khẩu lưu trong data/config.json đã mã hoá bằng Windows DPAPI (chỉ tài khoản Windows này trên máy này giải mã được);
không bao giờ in ra log hay gửi về trình duyệt.
"""

from __future__ import annotations

import base64
import hashlib
import sys

API = "https://cloud.cnpowercore.com:8091"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=utf-8",
    "Origin": "https://console.cnpowercore.com",
    "Referer": "https://console.cnpowercore.com/",
    "Accept-Language": "en-US",
}
PREFIX = "dpapi:"


class LoginError(Exception):
    pass


# ---------- Mã hoá mật khẩu khi lưu (Windows DPAPI) ----------
def _dpapi(data: bytes, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    src, out = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), Blob()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out)):  # 0x1: không hiện hộp thoại
        raise LoginError("Không giải mã được mật khẩu đã lưu (đổi tài khoản Windows / máy khác?) — nhập lại mật khẩu.")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protect(password: str) -> str:
    if not password:
        return ""
    if sys.platform != "win32":
        return "plain:" + base64.b64encode(password.encode()).decode()
    return PREFIX + base64.b64encode(_dpapi(password.encode("utf-8"), True)).decode()


def unprotect(stored: str) -> str:
    if not stored:
        return ""
    if stored.startswith("plain:"):
        return base64.b64decode(stored[6:]).decode()
    if stored.startswith(PREFIX):
        return _dpapi(base64.b64decode(stored[len(PREFIX):]), False).decode("utf-8")
    return stored


# ---------- Đăng nhập ----------
def encrypt_password(password: str, public_key_b64: str) -> str:
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    key = load_der_public_key(base64.b64decode(public_key_b64))
    digest = hashlib.md5(password.strip().encode("utf-8")).hexdigest()
    return base64.b64encode(key.encrypt(digest.encode(), padding.PKCS1v15())).decode()


def login(account: str, password: str, timeout: float = 30) -> tuple[str, str]:
    """-> (ssoticket, token). Lỗi -> LoginError với lời nhắn dễ hiểu (không chứa mật khẩu)."""
    import requests

    account = (account or "").strip()
    if not account or not password:
        raise LoginError("Chưa nhập tài khoản / mật khẩu CNPowercore.")
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        r = s.get(f"{API}/authen/index/getPublicKey", timeout=timeout)
        key = str((r.json() or {}).get("data") or "").replace("power", "")
        if not key:
            raise LoginError(f"Không lấy được khóa đăng nhập (HTTP {r.status_code}).")
        body = {"account": account, "password": encrypt_password(password, key)}
        r = s.post(f"{API}/authen/login/validate", json=body, timeout=timeout)
        res = r.json() if r.content else {}
    except requests.RequestException as e:
        raise LoginError(f"Không kết nối được máy chủ CNPowercore: {type(e).__name__}") from None
    except ValueError:
        raise LoginError(f"Máy chủ trả về dữ liệu lạ (HTTP {r.status_code}).") from None
    if str(res.get("success")) == "false" or not isinstance(res.get("data"), dict):
        raise LoginError(f"Đăng nhập thất bại: {res.get('message') or f'HTTP {r.status_code}'}")
    data = res["data"]
    if data.get("isFirst"):
        raise LoginError("Tài khoản đăng nhập lần đầu: hãy vào console.cnpowercore.com đổi mật khẩu trước.")
    token = str(data.get("token") or "").strip()
    sso = s.cookies.get("ssoticket") or str(data.get("ssoticket") or data.get("ssoTicket") or "").strip()
    if not token:
        raise LoginError("Đăng nhập được nhưng không thấy token trong phản hồi.")
    if not sso:
        names = ", ".join(sorted({c.name for c in s.cookies})) or "không có"
        raise LoginError(f"Đăng nhập được nhưng không thấy cookie ssoticket (cookie nhận được: {names}).")
    return sso, token
