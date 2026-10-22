"""Test tự đăng nhập CNPowercore (máy chủ giả lập, không gọi web thật) + mã hoá mật khẩu lưu + DbSync có bước đăng nhập."""
import base64
import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ccts_login as CL  # noqa: E402
import dbsync as DS  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUB = base64.b64encode(KEY.public_key().public_bytes(serialization.Encoding.DER,
                                                    serialization.PublicFormat.SubjectPublicKeyInfo)).decode()


class FakeResp:
    def __init__(self, data, status=200):
        self.data, self.status_code, self.content = data, status, b"x"

    def json(self):
        return self.data


class FakeSession:
    """Giả lập cloud.cnpowercore.com:8091: kiểm tra mật khẩu = RSA(md5(mật khẩu))."""
    password = "Mk@12345"

    def __init__(self):
        self.headers, self.cookies, self.sent = {}, {}, None

    def get(self, url, timeout=None):
        assert url.endswith("/authen/index/getPublicKey")
        return FakeResp({"success": "true", "data": PUB[:40] + "power" + PUB[40:]})

    def post(self, url, json=None, timeout=None):
        assert url.endswith("/authen/login/validate")
        self.sent = json
        got = KEY.decrypt(base64.b64decode(json["password"]), padding.PKCS1v15()).decode()
        if json["account"] != "kythuat01" or got != hashlib.md5(self.password.encode()).hexdigest():
            return FakeResp({"success": "false", "message": "Account or password error"})
        self.cookies["ssoticket"] = "SSO-123"
        return FakeResp({"success": "true", "data": {"token": "TK-456", "isFirst": False}})


class TestLogin(unittest.TestCase):
    def test_login_ok_and_password_never_sent_plain(self):
        s = FakeSession()
        with mock.patch("requests.Session", lambda: s):
            self.assertEqual(CL.login(" kythuat01 ", "Mk@12345"), ("SSO-123", "TK-456"))
        self.assertNotIn("Mk@12345", str(s.sent))

    def test_wrong_password_message(self):
        with mock.patch("requests.Session", FakeSession):
            with self.assertRaisesRegex(CL.LoginError, "Account or password error"):
                CL.login("kythuat01", "sai")

    def test_missing_account(self):
        with self.assertRaises(CL.LoginError):
            CL.login("", "x")

    def test_protect_roundtrip_not_plain(self):
        stored = CL.protect("Mk@12345")
        self.assertNotIn("Mk@12345", stored)
        self.assertEqual(CL.unprotect(stored), "Mk@12345")
        self.assertEqual(CL.protect(""), "")


class TestDbSyncLogin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "main.py").write_text(
            "import os\nprint('sso', os.environ['CCTS_SSOTICKET'] == 'SSO-NEW')\n"
            "print('KẾT QUẢ ĐỒNG BỘ - trạng thái: completed')\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_sync(self, login):
        done = []
        d = DS.DbSync(lambda e: None, done.append)
        d.start("new", "", "", "x.db", self.tmp.name, 100, 0, login=login)
        for _ in range(100):
            if done:
                break
            time.sleep(0.05)
        return done, d.snapshot()["lines"]

    def test_login_then_sync_with_new_tokens(self):
        done, lines = self.run_sync(lambda: ("SSO-NEW", "TK"))
        self.assertEqual(done, ["completed"])
        self.assertIn("sso True", lines)

    def test_login_failure_stops_before_sync(self):
        def bad():
            raise CL.LoginError("Đăng nhập thất bại: Account or password error")
        done, lines = self.run_sync(bad)
        self.assertEqual(done, ["auth_error"])
        self.assertFalse(any(x.startswith("sso") for x in lines))

    def test_no_login_no_tokens_rejected(self):
        with self.assertRaises(ValueError):
            DS.DbSync(lambda e: None).start("new", "", "", "x.db", self.tmp.name, 100, 0)


if __name__ == "__main__":
    unittest.main()
