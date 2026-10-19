"""Đọc tem HÀNG LỖI bằng AI có vision, chạy phía server (key không nằm trên điện thoại).

Nhà cung cấp: Google Gemini (REST, có gói miễn phí) hoặc Ollama (model chạy trên máy).
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from datetime import date

from matching import fmt_day, tem_day, tem_day_fixed

# 7 trường trên tem.
FIELDS = (
    ("ma_tram", "Mã Trạm"),
    ("model", "Model Trụ Sạc"),
    ("serial", "S/N Trụ Sạc"),
    ("ma_loi", "Mã lỗi"),
    ("ticket", "Ticket"),
    ("ngay_thay_the", "Ngày Thay Thế"),
    ("mo_ta_loi", "Mô Tả Lỗi"),
)

# Các trường là mã: AI trả thêm danh sách ký tự có thể ở từng vị trí để so từng ký tự với DB.
CODE_FIELDS = ("ma_tram", "serial", "ticket")

_FIELD_SCHEMA = {
    "type": "object",
    "required": ["value", "uncertain", "note"],
    "properties": {"value": {"type": "string"}, "uncertain": {"type": "boolean"}, "note": {"type": "string"}},
}
_CODE_SCHEMA = {
    "type": "object",
    "required": ["value", "chars", "uncertain", "note"],
    "properties": {
        "value": {"type": "string"},
        "chars": {"type": "array", "items": {"type": "string"}},
        "uncertain": {"type": "boolean"},
        "note": {"type": "string"},
    },
}
SCHEMA = {
    "type": "object",
    "required": ["is_broken_item_label", "image_issue", "fields"],
    "properties": {
        "is_broken_item_label": {"type": "boolean"},
        "image_issue": {"type": "string"},
        "fields": {
            "type": "object",
            "required": [k for k, _ in FIELDS],
            "properties": {k: _CODE_SCHEMA if k in CODE_FIELDS else _FIELD_SCHEMA for k, _ in FIELDS},
        },
    },
}

SYSTEM_PROMPT = """Bạn là công cụ OCR cho tem giấy "HÀNG LỖI / BROKEN ITEM" dán trên linh kiện/bo mạch trụ sạc xe điện.
Tem là một bảng 2 cột: cột trái là nhãn in sẵn, cột phải là giá trị viết tay (thường bút bi). Các hàng theo thứ tự và khóa JSON tương ứng:
1. Mã Trạm -> ma_tram (dạng C.VLO10201, có dấu chấm sau chữ cái đầu)
2. Model Trụ Sạc -> model
3. S/N Trụ Sạc -> serial (thường 12 chữ số)
4. Mã lỗi -> ma_loi
5. Ticket -> ticket (thường 12 ký tự chữ hoa + số, ví dụ 8MHEMQO4Z3EO)
6. Ngày Thay Thế -> ngay_thay_the
7. Mô Tả Lỗi -> mo_ta_loi

Quy tắc:
- Ghép giá trị với nhãn theo HÀNG của bảng. Chữ viết tay có thể lệch dòng hoặc tràn ra ngoài ô; ảnh có thể nghiêng, xoay.
- Chép đúng từng ký tự của mã, số serial, ticket: giữ nguyên số 0 ở đầu, chữ hoa/thường, dấu chấm/gạch bên trong mã. Không "sửa" mã cho hợp lý.
- Bỏ dấu chấm thừa ở cuối giá trị (người viết hay chấm kết thúc).
- Bỏ qua: sticker nhỏ dán đè lên tem, số hoặc ký hiệu ghi bên ngoài các ô của bảng, linh kiện bo mạch phía sau.
- Mã lỗi và Mô Tả Lỗi thường là tiếng Việt viết tay: ghi bằng tiếng Việt có dấu đúng chính tả khi đọc rõ ý (ví dụ "ngat sac" -> "ngắt sạc").
- Ngày Thay Thế: trả về dạng DD/MM/YYYY nếu đọc rõ ngày, tháng, năm; nếu không thì giữ như đọc được.
- uncertain = true nếu có bất kỳ ký tự nào không chắc chắn (dễ nhầm: 0/O/D/Q, 1/I/l/7, 2/Z, 5/S, 6/G/b, 8/B, U/V, M/N, 4/A), bị che, mờ hoặc bị cắt. Ghi ngắn trong note ký tự nào đáng ngờ và các khả năng.
- Không đoán bừa: ký tự không đọc được thì thay bằng "?" trong value và đặt uncertain = true.
- Với ma_tram, serial, ticket: đọc TỪNG KÝ TỰ một. Trả thêm "chars": mảng có đúng số phần tử bằng số ký tự của value, theo thứ tự.
  Mỗi phần tử là chuỗi các ký tự có thể ở vị trí đó, ký tự khả năng cao nhất đứng đầu.
  Ký tự chắc chắn: chỉ 1 ký tự (vd. "8"). Phân vân: liệt kê mọi khả năng (vd. "0OQ", "2Z", "1I7"). Không đọc được: "?".
  Ví dụ value "8MHEMQ0423EQ" -> chars ["8","M","H","E","M","QO","0O","4","2Z","3","E","QO"]. Dấu chấm/gạch cũng là một phần tử (vd. ".").
- Ô để trống: value = "", uncertain = false, note = "trống".
- note = "" khi chắc chắn.
- Nếu ảnh không phải tem này hoặc không đọc được: is_broken_item_label = false, mọi value = "", mô tả lý do trong image_issue.
- image_issue = "" nếu ảnh ổn; nếu không, mô tả ngắn vấn đề (mờ, lóa, bị cắt, nghiêng...).
Chỉ trả về JSON đúng schema, không thêm chữ nào khác."""

USER_TEXT = "Trích xuất 7 trường của tem trong ảnh này."

# ---------- Chế độ đọc một phần tem ----------
# Mã lỗi + Mô Tả Lỗi (đoạn chữ dài) và Ticket (12 ký tự đọc từng ký tự) chiếm phần lớn thời gian AI sinh chữ.
#   quick: Mã trạm, Model, S/N, Ngày        -> Ticket lấy từ DB theo SN + ngày + trạm
#   codes: Ticket, S/N, Mã trạm, Ngày       -> bỏ Model, Mã lỗi, Mô tả lỗi (mô tả lỗi để trống, không dùng)
READ_MODES = {
    "quick": ("ma_tram", "model", "serial", "ngay_thay_the"),
    "codes": ("ticket", "serial", "ma_tram", "ngay_thay_the"),
}
_ROW_HINT = {
    "ma_tram": "Mã Trạm -> ma_tram (dạng C.VLO10201)",
    "model": "Model Trụ Sạc -> model",
    "serial": "S/N Trụ Sạc -> serial (thường 12 chữ số)",
    "ticket": "Ticket -> ticket (thường 12 ký tự chữ hoa + số)",
    "ngay_thay_the": "Ngày Thay Thế -> ngay_thay_the (trả về DD/MM/YYYY nếu đọc rõ)",
}
_LABEL = dict(FIELDS)


def read_fields(cfg: dict) -> tuple[str, ...] | None:
    """Các ô AI cần đọc; None = đủ 7 ô."""
    return READ_MODES.get(cfg.get("readMode") or "")


def is_quick(cfg: dict) -> bool:
    return cfg.get("readMode") == "quick"


def subset_schema(keys: tuple[str, ...]) -> dict:
    props = SCHEMA["properties"]["fields"]["properties"]
    return {**SCHEMA, "properties": {**SCHEMA["properties"], "fields": {
        "type": "object", "required": list(keys), "properties": {k: props[k] for k in keys}}}}


def subset_prompt(keys: tuple[str, ...]) -> tuple[str, str]:
    skip = ", ".join(_LABEL[k] for k, _ in FIELDS if k not in keys)
    codes = [k for k in keys if k in CODE_FIELDS]
    rows = "\n".join(f"- {_ROW_HINT[k]}" for k in keys)
    system = f"""Bạn là công cụ OCR cho tem giấy "HÀNG LỖI / BROKEN ITEM" dán trên bo mạch trụ sạc xe điện.
Tem là bảng 2 cột: nhãn in sẵn bên trái, giá trị VIẾT TAY bên phải. Chỉ đọc {len(keys)} hàng sau (BỎ QUA {skip}):
{rows}
Quy tắc: ghép giá trị theo HÀNG của bảng; chép đúng từng ký tự, không sửa cho hợp lý; giữ số 0 ở đầu; bỏ dấu chấm thừa ở cuối.
Với {", ".join(codes)}: đọc TỪNG KÝ TỰ, trả thêm "chars" — mỗi vị trí một chuỗi các ký tự có thể, khả năng cao nhất trước (vd. "0O", "2Z"); ký tự chắc chắn chỉ 1 ký tự; không đọc được: "?".
uncertain = true nếu có ký tự không chắc; note ngắn gọn ("" khi chắc chắn). Ô trống: value = "".
Ảnh không phải tem này: is_broken_item_label = false. image_issue = "" nếu ảnh ổn.
Chỉ trả về JSON đúng schema."""
    return system, "Đọc " + ", ".join(_LABEL[k] for k in keys) + " trên tem."


QUICK_FIELDS = READ_MODES["quick"]
QUICK_SCHEMA = subset_schema(QUICK_FIELDS)


class ExtractError(Exception):
    def __init__(self, message: str, *, fatal: bool = False, retryable: bool = False, retry_after: float = 0):
        super().__init__(message)
        self.fatal = fatal  # lỗi key/cấu hình: dừng cả hàng đợi
        self.retryable = retryable  # lỗi tạm thời: tự thử lại
        self.retry_after = retry_after


def with_retry(fn, attempts: int = 4):
    for i in range(1, attempts + 1):
        try:
            return fn()
        except ExtractError as err:
            if not err.retryable or i >= attempts:
                raise
            time.sleep(min(60, err.retry_after or 2 * 2 ** (i - 1)))


def parse_json_text(text: str) -> dict:
    t = (text or "").strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(t[a : b + 1])
        except ValueError:
            pass
    raise ExtractError("AI trả về dữ liệu không đúng định dạng JSON. Bấm đọc lại.")


def normalize(data) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("fields"), dict):
        raise ExtractError("Kết quả AI thiếu trường dữ liệu. Bấm đọc lại.")
    fields = {}
    for key, _ in FIELDS:
        f = data["fields"].get(key) or {}
        raw = f if isinstance(f, str) else f.get("value")  # model nhỏ đôi khi trả thẳng chuỗi
        value = "" if raw is None else str(raw).strip()
        note = f.get("note", "") if isinstance(f, dict) else ""
        chars = f.get("chars") if isinstance(f, dict) else None
        fields[key] = {
            "value": value,
            "uncertain": bool(isinstance(f, dict) and f.get("uncertain")) or "?" in value,
            "note": note.strip() if isinstance(note, str) else "",
            "chars": [str(c) for c in chars] if isinstance(chars, list) else None,
        }
    return {
        "is_label": data.get("is_broken_item_label") is not False,
        "issue": str(data.get("image_issue") or "").strip(),
        "fields": fields,
    }


# ---------- HTTP ----------

def _http(url: str, body: dict | None = None, headers: dict | None = None, timeout: float = 120):
    req = urllib.request.Request(url, method="POST" if body is not None else "GET", headers=dict(headers or {}))
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, data, timeout=timeout) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        return e.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ConnectionError(str(getattr(e, "reason", e))) from e


def _short(m: str) -> str:
    m = m or ""
    return m if len(m) <= 200 else m[:200] + "…"


# ---------- Gemini ----------

GEMINI = "https://generativelanguage.googleapis.com/v1beta"
_schema_rejected = False


def _gemini_schema(s: dict) -> dict:
    out = {"type": s["type"].upper()}
    if "properties" in s:
        out["properties"] = {k: _gemini_schema(v) for k, v in s["properties"].items()}
        out["propertyOrdering"] = list(s["properties"])
    if "items" in s:
        out["items"] = _gemini_schema(s["items"])
    if "required" in s:
        out["required"] = s["required"]
    return out


def _gemini_call(path: str, key: str, body: dict | None = None) -> dict:
    try:
        code, data = _http(GEMINI + path, body, {"x-goog-api-key": key})
    except ConnectionError:
        raise ExtractError("Máy tính không kết nối được Gemini (mất mạng?).", retryable=True)
    if 200 <= code < 300:
        return data
    err = data.get("error") or {}
    msg = err.get("message") or f"HTTP {code}"
    reason = json.dumps(err.get("details") or "")
    if code == 400 and re.search(r"API_KEY_INVALID|API key not valid", msg + reason, re.I):
        raise ExtractError("Gemini API key không hợp lệ.", fatal=True)
    if code == 403:
        raise ExtractError(f"Gemini từ chối truy cập: {_short(msg)}", fatal=True)
    if code == 404:
        raise ExtractError("Không có model Gemini này. Mở Cài đặt, bấm Kiểm tra để chọn model khác.", fatal=True)
    if code == 429:
        m = re.search(r'retryDelay":\s*"(\d+)', reason)
        daily = re.search(r"per.?day|PerDay", msg + reason, re.I)
        if daily:
            raise ExtractError("Đã hết hạn mức miễn phí trong ngày của Gemini. Thử lại ngày mai hoặc đổi model.", fatal=True)
        raise ExtractError("Gemini giới hạn tốc độ (gói miễn phí). Đang chờ rồi thử lại…", retryable=True,
                           retry_after=int(m[1]) + 0.5 if m else 15)
    if code >= 500:
        raise ExtractError("Máy chủ Gemini quá tải. Đọc lại sau ít phút.", retryable=True)
    e = ExtractError(f"Lỗi Gemini {code}: {_short(msg)}")
    e.status, e.raw = code, msg  # type: ignore[attr-defined]
    raise e


# Chế độ "suy nghĩ" của Gemini: tắt để đọc nhanh (DB kiểm tra lại kết quả). Model không cho tắt -> thử mức thấp,
# rồi để mặc định. Ghi nhớ cách dùng được cho từng model.
_THINKING_TRIES = ({"thinkingLevel": "low"}, {"thinkingBudget": 0}, None)
_thinking_ok: dict[str, int] = {}


def gemini_generate(cfg: dict, jpeg: bytes, system: str, user: str, schema: dict) -> dict:
    global _schema_rejected
    model = cfg["geminiModel"]
    gen = {"temperature": 0, "responseMimeType": "application/json"}
    if not _schema_rejected:
        gen["responseSchema"] = _gemini_schema(schema)
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}},
            {"text": user},
        ]}],
        "generationConfig": gen,
    }
    path = f"/models/{urllib.request.quote(model)}:generateContent"
    fast = cfg.get("speed", "fast") == "fast"
    tries = list(range(_thinking_ok.get(model, 0), len(_THINKING_TRIES))) if fast else [len(_THINKING_TRIES) - 1]
    data = None
    for i in tries:
        think = _THINKING_TRIES[i]
        if think:
            gen["thinkingConfig"] = think
        else:
            gen.pop("thinkingConfig", None)
        try:
            data = _gemini_call(path, cfg["geminiKey"], body)
            if fast:
                _thinking_ok[model] = i
            break
        except ExtractError as e:
            raw = getattr(e, "raw", "")
            if getattr(e, "status", 0) != 400:
                raise
            if think and not re.search("schema", raw, re.I):
                continue  # model không nhận kiểu cấu hình này (lỗi 400) -> thử kiểu tiếp theo
            if re.search("schema", raw, re.I) and not _schema_rejected:
                # Model không hỗ trợ schema: chỉ yêu cầu JSON, prompt đã mô tả cấu trúc.
                _schema_rejected = True
                gen.pop("responseSchema", None)
                data = _gemini_call(path, cfg["geminiKey"], body)
                break
            raise
    if (data.get("promptFeedback") or {}).get("blockReason"):
        raise ExtractError(f"Gemini chặn ảnh này ({data['promptFeedback']['blockReason']}). Chụp lại hoặc nhập tay.")
    cands = data.get("candidates") or []
    if not cands:
        raise ExtractError("Gemini không trả kết quả. Bấm đọc lại.", retryable=True)
    cand = cands[0]
    parts = (cand.get("content") or {}).get("parts") or []
    if cand.get("finishReason") == "MAX_TOKENS":
        raise ExtractError("Phản hồi Gemini bị cắt ngang. Bấm đọc lại.")
    if cand.get("finishReason") not in (None, "STOP") and not parts:
        raise ExtractError(f"Gemini dừng với lý do {cand['finishReason']}. Chụp lại hoặc nhập tay.")
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))  # bỏ phần "suy nghĩ"
    return parse_json_text(text)


def gemini_extract(cfg: dict, jpeg: bytes) -> dict:
    keys = read_fields(cfg)
    if keys:
        system, user = subset_prompt(keys)
        return gemini_generate(cfg, jpeg, system, user, subset_schema(keys))
    return gemini_generate(cfg, jpeg, SYSTEM_PROMPT, USER_TEXT, SCHEMA)


def gemini_models(cfg: dict) -> list[str]:
    data = _gemini_call("/models?pageSize=1000", cfg["geminiKey"])
    names = [m["name"].removeprefix("models/") for m in data.get("models", [])
             if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    return [n for n in names if n.startswith("gemini") and not re.search(r"image|tts|audio|live|embedding|robotics|computer", n, re.I)]


def pick_default(models: list[str]) -> str:
    """Flash (không phải Lite) có số phiên bản cao nhất."""
    def ver(n: str) -> float:
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return float(m[1]) if m else 0
    flash = sorted((n for n in models if re.fullmatch(r"gemini-[\d.]+-flash", n)), key=ver, reverse=True)
    return (flash or [n for n in models if "flash" in n] or models or [""])[0]


# ---------- Ollama ----------

def _ollama(cfg: dict, path: str, body: dict | None = None) -> dict:
    base = (cfg.get("ollamaUrl") or "http://localhost:11434").rstrip("/")
    try:
        code, data = _http(base + path, body, timeout=600)
    except ConnectionError:
        raise ExtractError(f"Không kết nối được Ollama tại {base}. Ollama đã chạy chưa?", fatal=True)
    if 200 <= code < 300:
        return data
    msg = data.get("error") or f"HTTP {code}"
    if code == 404:
        raise ExtractError(f"Chưa có model này trong Ollama. Chạy: ollama pull {cfg.get('ollamaModel')}", fatal=True)
    if re.search(r"allocate|out of memory|requires more system memory", msg, re.I):
        raise ExtractError("Model quá lớn so với RAM của máy. Dùng model nhỏ hơn, ví dụ qwen3-vl:4b.", fatal=True)
    raise ExtractError(f"Lỗi Ollama {code}: {_short(msg)}", retryable=code >= 500)


def ollama_generate(cfg: dict, jpeg: bytes, system: str, user: str, schema: dict, max_tokens: int = 1200) -> dict:
    data = _ollama(cfg, "/api/chat", {
        "model": cfg["ollamaModel"],
        "stream": False,
        "format": schema,
        "think": False,
        "keep_alive": "30m",  # giữ model trong RAM giữa các ảnh (nạp lại mất vài chục giây)
        "options": {"temperature": 0, "num_ctx": 8192, "num_predict": max_tokens},  # chặn model lặp chữ không dừng
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user, "images": [base64.b64encode(jpeg).decode()]},
        ],
    })
    return parse_json_text((data.get("message") or {}).get("content", ""))


# Model nhỏ chạy trên máy (CPU): prompt ngắn, không có mã ví dụ thật (model nhỏ hay chép lại ví dụ),
# trả JSON một dòng; ký tự phân vân ghi trong ngoặc vuông -> ít chữ phải sinh ra, nhanh hơn nhiều.
LOCAL_PROMPT = """Đọc tem giấy "HÀNG LỖI / BROKEN ITEM". Tem là bảng 2 cột: nhãn in sẵn bên trái, giá trị VIẾT TAY bên phải.
Các hàng: Mã trạm, Model, SN, Mã lỗi, Ticket, Date (Ngày thay thế), Mô tả lỗi.
Chép đúng nét chữ viết tay, từng ký tự một. KHÔNG tự bịa, KHÔNG sửa cho hợp lý.
Ký tự phân vân: ghi các khả năng trong ngoặc vuông, khả năng cao nhất trước, vd. A[0O]B[2Z]7. Không đọc được: ?
Ô trống: "". Ngày ghi dạng ngày/tháng/năm. Ảnh không có tem này: label=false.
Trả về JSON trên MỘT dòng:
{"label":true,"ma_tram":"...","model":"...","sn":"...","ma_loi":"...","ticket":"...","date":"...","mo_ta":"..."}"""
_LOCAL_KEYS = {"ma_tram": "ma_tram", "model": "model", "sn": "serial", "ma_loi": "ma_loi", "ticket": "ticket",
               "date": "ngay_thay_the", "mo_ta": "mo_ta_loi"}
LOCAL_SCHEMA = {"type": "object", "required": ["label", *_LOCAL_KEYS],
                "properties": {"label": {"type": "boolean"}, **{k: {"type": "string"} for k in _LOCAL_KEYS}}}


def parse_brackets(s: str) -> tuple[str, list[str] | None]:
    """"A[0O]B[2Z]7" -> ("A0B27", ["A", "0O", "B", "2Z", "7"]). Không có ngoặc -> chars None."""
    s = (s or "").strip().rstrip(".")
    if "[" not in s:
        return s, None
    value, chars, i = [], [], 0
    while i < len(s):
        if s[i] == "[" and "]" in s[i:]:
            j = s.index("]", i)
            alts = s[i + 1 : j].replace("/", "").replace(",", "").replace(" ", "") or "?"
            value.append(alts[0])
            chars.append(alts)
            i = j + 1
        else:
            value.append(s[i])
            chars.append(s[i])
            i += 1
    return "".join(value), chars


_LOCAL_NAME = {"ma_tram": "Mã trạm", "model": "Model", "serial": "SN", "ticket": "Ticket", "ngay_thay_the": "Date (Ngày thay thế)",
               "ma_loi": "Mã lỗi", "mo_ta_loi": "Mô tả lỗi"}
_LOCAL_JSON = {v: k for k, v in _LOCAL_KEYS.items()}  # serial -> "sn", ngay_thay_the -> "date"…


def local_subset(keys: tuple[str, ...]) -> tuple[str, dict, dict]:
    """Prompt ngắn cho model chạy trên máy, chỉ các ô `keys`. Trả (prompt, schema, {khóa JSON: trường})."""
    jkeys = {_LOCAL_JSON[k]: k for k in keys}
    skip = ", ".join(_LOCAL_NAME[k] for k, _ in FIELDS if k not in keys)
    sample = ",".join(f'"{j}":"..."' for j in jkeys)
    prompt = f"""Đọc tem giấy "HÀNG LỖI / BROKEN ITEM". Tem là bảng 2 cột: nhãn in sẵn bên trái, giá trị VIẾT TAY bên phải.
CHỈ đọc {len(keys)} hàng: {", ".join(_LOCAL_NAME[k] for k in keys)}. Bỏ qua {skip}.
Chép đúng nét chữ viết tay, từng ký tự một. KHÔNG tự bịa, KHÔNG sửa cho hợp lý.
Ký tự phân vân: ghi các khả năng trong ngoặc vuông, khả năng cao nhất trước, vd. A[0O]B[2Z]7. Không đọc được: ?
Ô trống: "". Ngày ghi dạng ngày/tháng/năm. Ảnh không có tem này: label=false.
Trả về JSON trên MỘT dòng:
{{"label":true,{sample}}}"""
    schema = {"type": "object", "required": ["label", *jkeys],
              "properties": {"label": {"type": "boolean"}, **{j: {"type": "string"} for j in jkeys}}}
    return prompt, schema, jkeys


def ollama_extract(cfg: dict, jpeg: bytes) -> dict:
    sub = read_fields(cfg)
    if sub:
        prompt, schema, keys = local_subset(sub)
        data = ollama_generate(cfg, jpeg, prompt, "Đọc tem này.", schema, max_tokens=60 + 45 * len(sub))
    else:
        data = ollama_generate(cfg, jpeg, LOCAL_PROMPT, "Đọc tem này.", LOCAL_SCHEMA, max_tokens=400)
        keys = _LOCAL_KEYS
    fields = {}
    for k, key in keys.items():
        value, chars = parse_brackets(str(data.get(k) or ""))
        fields[key] = {"value": value, "uncertain": bool(chars and any(len(c) > 1 for c in chars)) or "?" in value,
                       "note": "" if not chars else "phân vân: " + " ".join(f"{c[0]}→{'/'.join(c)}" for c in chars if len(c) > 1),
                       "chars": chars or (list(value) if key in CODE_FIELDS and value else None)}
    return {"is_broken_item_label": data.get("label") is not False, "image_issue": "", "fields": fields}


def rotate_jpeg(jpeg: bytes, deg: int) -> bytes:
    """Xoay ảnh (độ, ngược chiều kim đồng hồ). Cần Pillow; không có thì trả ảnh gốc."""
    if not deg:
        return jpeg
    try:
        import io
        from PIL import Image
    except ImportError:
        return jpeg
    im = Image.open(io.BytesIO(jpeg)).rotate(deg, expand=True)
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


def can_rotate() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def ollama_models(cfg: dict) -> list[str]:
    return [m["name"] for m in _ollama(cfg, "/api/tags").get("models", [])]


# ---------- Xác nhận lần 2: so ảnh với các ticket ứng viên ----------

CONFIRM_PROMPT = """Bạn so sánh tem giấy "HÀNG LỖI / BROKEN ITEM" viết tay với danh sách ticket lấy từ cơ sở dữ liệu.
Tem có các hàng: Mã Trạm, Model Trụ Sạc, S/N Trụ Sạc, Mã lỗi, Ticket, Ngày Thay Thế (ngày/tháng/năm), Mô Tả Lỗi.
Nhìn kỹ TỪNG KÝ TỰ viết tay của Ticket, S/N, Mã Trạm và ngày, so với từng ticket ứng viên.
Chữ viết tay hay nhầm: 0/O/Q/D, 1/I/7, 2/Z, 5/S, 6/G, 8/B, U/V, M/N, 4/A.
Chọn ticket mà các ký tự trên tem phù hợp (chỉ khác ở chỗ nét viết dễ nhầm). Nếu ký tự rõ ràng khác ở vị trí nào thì ticket đó KHÔNG khớp.
Không ticket nào khớp thì pk = "".
confidence: "high" khi Ticket hoặc S/N khớp từng ký tự; "medium" khi khớp nhưng còn 1-2 ký tự khó đọc; "low" khi chỉ đoán.
Chỉ trả JSON đúng schema."""

CONFIRM_SCHEMA = {
    "type": "object",
    "required": ["pk", "confidence", "reason"],
    "properties": {"pk": {"type": "string"}, "confidence": {"type": "string"}, "reason": {"type": "string"}},
}


def confirm(cfg: dict, jpeg: bytes, cands: list[dict]) -> dict:
    """cands: [{pk, id, sn, station, date}] -> {pk, confidence, reason}"""
    lines = "\n".join(f'- pk={c["pk"]} | Ticket {c["id"]} | S/N {c["sn"]} | Mã Trạm {c["station"]} | ngày onsite {c["date"] or "?"}'
                       for c in cands)
    user = f"Ticket ứng viên:\n{lines}\n\nTicket nào khớp với tem trong ảnh?"
    fn = ollama_generate if cfg.get("provider") == "ollama" else gemini_generate
    model = {"geminiModel": cfg.get("geminiConfirmModel") or cfg["geminiModel"]}  # có thể dùng model mạnh hơn model đọc
    kw = {"max_tokens": 200} if fn is ollama_generate else {}
    data = with_retry(lambda: fn({**cfg, **model, "speed": "fast"}, jpeg, CONFIRM_PROMPT, user, CONFIRM_SCHEMA, **kw))
    pk = str(data.get("pk") or "").strip()
    return {"pk": pk if any(c["pk"] == pk for c in cands) else "",
            "confidence": str(data.get("confidence") or "low").lower(), "reason": str(data.get("reason") or "")[:300]}


# ---------- Chung ----------

def ready(cfg: dict) -> str:
    """"" nếu cấu hình đủ để chạy, ngược lại là lý do."""
    if cfg.get("provider") == "ollama":
        return "" if cfg.get("ollamaModel") else "Chưa chọn model Ollama."
    if not cfg.get("geminiKey"):
        return "Chưa nhập Gemini API key."
    return "" if cfg.get("geminiModel") else "Chưa chọn model Gemini."


def shrink_jpeg(jpeg: bytes, max_side: int) -> bytes:
    """Thu nhỏ ảnh gửi AI (ảnh lưu trong bảng giữ nguyên). Model chạy trên CPU xử lý ảnh chiếm phần lớn thời gian:
    đo trên máy này (qwen3-vl:4b, tem thật) 1568px ~106-120s, 1024px ~69s, cùng kết quả khớp DB; 800px đọc hỏng SN."""
    if not max_side:
        return jpeg
    try:
        import io
        from PIL import Image
    except ImportError:
        return jpeg
    im = Image.open(io.BytesIO(jpeg))
    s = max_side / max(im.size)
    if s >= 1:
        return jpeg
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    out = io.BytesIO()
    im.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


def extract(cfg: dict, jpeg: bytes) -> dict:
    fn = ollama_extract if cfg.get("provider") == "ollama" else gemini_extract
    jpeg = shrink_jpeg(jpeg, int(cfg.get("aiMaxSide") or 0))
    return normalize(with_retry(lambda: fn(cfg, jpeg)))


def list_models(cfg: dict) -> list[str]:
    return ollama_models(cfg) if cfg.get("provider") == "ollama" else gemini_models(cfg)


# ---------- Tem -> cột bảng ----------

def _join_phenomenon(code: str, desc: str) -> str:
    a, b = code.strip().rstrip("."), desc.strip().rstrip(".")
    norm = lambda s: " ".join(s.lower().replace(".", " ").split())  # noqa: E731
    if not a:
        return b
    if not b:
        return a
    if norm(a) == norm(b) or norm(a) in norm(b):
        return b
    if norm(b) in norm(a):
        return a
    return f"{a} - {b}"


CHAR_COLUMNS = {"ticket_id": "ticket", "sn": "serial", "station_code": "ma_tram"}


def map_chars(fields: dict) -> dict:
    """{cột: danh sách ký tự có thể ở từng vị trí} cho các cột mã (bỏ nếu AI trả lệch độ dài)."""
    from matching import char_sets
    out = {}
    for col, k in CHAR_COLUMNS.items():
        f = fields.get(k) or {}
        sets = char_sets(f.get("chars"), f.get("value", ""))
        if sets:
            out[col] = sets
    return out


def map_to_columns(fields: dict, fmt: str, ref: date | None = None) -> dict:
    """7 trường của tem -> {cột: {value, uncertain, note}}. ref: ngày chụp (mặc định hôm nay) để sửa năm đọc sai."""
    def get(k):
        return fields.get(k) or {"value": "", "uncertain": False, "note": ""}

    def notes(*xs):
        return "; ".join(x["note"] for x in xs if x["note"] and x["note"] != "trống")

    d = get("ngay_thay_the")
    day: date | None = tem_day_fixed(d["value"], ref)
    if day and day != tem_day(d["value"]):  # năm đọc sai / thiếu năm -> đã sửa: báo để người dùng xem lại
        d = {**d, "uncertain": True, "note": "; ".join(x for x in (d["note"], f"đã sửa năm (AI đọc \"{d['value']}\")") if x and x != "trống")}
    code, desc = get("ma_loi"), get("mo_ta_loi")
    return {
        "ticket_id": get("ticket"),
        "sn": get("serial"),
        "date_onsite": {**d, "value": fmt_day(day, fmt)} if day else {
            "value": d["value"], "uncertain": d["uncertain"] or bool(d["value"]),
            "note": f"Không hiểu được ngày: {d['note'] or d['value']}" if d["value"] else d["note"],
        },
        "original_phenomenon": {
            "value": _join_phenomenon(code["value"], desc["value"]),
            "uncertain": code["uncertain"] or desc["uncertain"],
            "note": notes(code, desc),
        },
        "station_code": get("ma_tram"),
    }
