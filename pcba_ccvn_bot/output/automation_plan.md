# Kế hoạch tool Python tạo Repair từ Excel cho bot PCBA-CCVN

## 1. Kiến trúc

```
pcba_ccvn_bot/
  config.yaml              # cấu hình (không chứa secret)
  .env                     # TG_API_ID, TG_API_HASH  (KHÔNG commit, KHÔNG export)
  data/boards.json         # từ tools/extract_boards.py
  data/workflow.json       # máy trạng thái (output/workflow.json)
  src/
    excel_loader.py        # đọc + validate Excel -> list[RepairRecord]
    models.py              # dataclass RepairRecord
    ledger.py              # SQLite chống trùng (record_key -> repair_id, status)
    driver_base.py         # interface: send_text, send_photo, click, wait_reply
    driver_telethon.py     # Phương án C (khuyến nghị)
    driver_playwright.py   # Phương án A/B (dự phòng, Edge CDP 9222)
    flow.py                # chạy từng record theo workflow, phát hiện menu lạ
    cli.py                 # extract | validate | dry-run | submit
  logs/run_YYYYMMDD.log
```

Phần trích xuất dữ liệu (`tools/extract_boards.py`, chỉ đọc) **tách riêng** với phần submit (`cli.py submit`).

## 2. Mẫu Excel (sheet `repairs`)

| Cột | Bắt buộc | Ánh xạ bước bot | Ghi chú |
|---|---|---|---|
| `record_key` | ✅ | — | Khoá chống trùng do bạn đặt (vd số phiếu nội bộ) |
| `board_id` | ✅ | Bước 1 | Phải có trong `boards.json` |
| `device_action` | | Bước 2 | `new` (mặc định). `search` chưa hỗ trợ vì nhánh chưa khảo sát |
| `contractor` | | Nhà thầu | `EC/ES/ITS/HTE/SE HN/SE HCM` hoặc trống → `🚫 Không có trong danh sách / Bỏ trống` |
| `photos_before_test` | ✅ | Ảnh trước test | 1–2 đường dẫn file, cách nhau bằng `;` |
| `test_tool_result` | ✅ | Bước 3 | `PASS` / `FAIL` (chưa hỗ trợ `Discard`) |
| `failure_cause` | nếu FAIL | | text |
| `disposition` | nếu FAIL | | text |
| `after_repair_result` | nếu FAIL | | `PASS` (nhánh FAIL chưa khảo sát) |
| `photos_after_repair` | | | 0–3 đường dẫn |
| `charging_station_test_status` | nếu FAIL | | `Pending` / `OK` |
| `ticket_id` | | | trống → `🚫 Không có ticket` |
| `sn`, `date_onsite`, `original_phenomenon`, `station_code` | | | trống → `🚫 Bỏ qua`. `date_onsite` ghi ra theo `M/D/YYYY` |
| `failure_verification_photo` | | Bước cuối | trống → `🚫 Bỏ qua` |
| `status`, `repair_id`, `device_code`, `error` | | — | tool tự ghi kết quả vào file kết quả |

Validate trước khi chạy: board tồn tại, enum hợp lệ, file ảnh tồn tại, ngày parse được, đủ trường theo nhánh PASS/FAIL. Lỗi nào cũng chặn cả lô ở bước `validate`.

## 3. Driver khuyến nghị: Telethon (Phương án C)

- `pip install telethon openpyxl pyyaml python-dotenv`
- Lấy `api_id`/`api_hash` tại my.telegram.org. **Bạn tự đăng nhập**, tool không lưu mật khẩu.
- Lần đầu chạy, Telethon hỏi số điện thoại và mã OTP trên console và **bạn tự nhập**. File `*.session` là bí mật ngang mật khẩu: để ngoài thư mục output, thêm vào `.gitignore`.

```python
# driver_telethon.py (rút gọn)
from telethon import TelegramClient

class TelethonDriver:
    def __init__(self, api_id, api_hash, session_path, bot="@pcba_ccvn_bot", timeout=30):
        self.client = TelegramClient(session_path, api_id, api_hash)
        self.bot, self.timeout = bot, timeout

    async def send_text(self, text):
        return await self.client.send_message(self.bot, text)

    async def send_photo(self, path):
        return await self.client.send_file(self.bot, path)

    async def last_bot_message(self):
        msgs = await self.client.get_messages(self.bot, limit=1)
        return msgs[0]

    async def wait_reply(self, after_id, expect_substr=None):
        """Chờ tin mới của bot (id > after_id) hoặc tin cũ được bot EDIT."""
        # dùng events.NewMessage / events.MessageEdited + asyncio.wait_for(timeout)
        ...

    async def click(self, msg, label):
        # msg.buttons: list[list[MessageButton]]; .data = callback_data (bytes)
        for row in msg.buttons or []:
            for b in row:
                if b.text.strip() == label:
                    return await b.click()
        raise UnexpectedMenu(f"Không thấy nút '{label}' trong msg {msg.id}: "
                             f"{[[b.text for b in r] for r in msg.buttons or []]}")
```

Telethon có `button.data`, nên **lấy được `callback_data` thật** để bổ sung vào `boards.json` (lệnh `cli.py extract --via telethon`, chỉ đọc tin nhắn Board gần nhất, không bấm).

## 4. Máy trạng thái (`flow.py`)

Mỗi bước có dạng: *chờ prompt khớp `bot_text` → thực hiện hành động → bước kế tiếp*, dựa trên `workflow.json`.

```python
STEPS = [
  ("S0_START",  "Quản lý Repair - chọn 1 thao tác", lambda r: click("➕ Thêm mới")),
  ("S1_BOARD",  "Bước 1: Chọn Board",               lambda r: click(board_label(r.board_id))),
  ("S2_DEVICE", "Board này đã có",                  lambda r: click("➕ Thêm mới")),
  ("S3_CONTR",  "Chọn nhà thầu",                    lambda r: click(r.contractor or "🚫 Không có trong danh sách / Bỏ trống")),
  ("S4_PHOTO",  "Gửi ảnh TRƯỚC KHI TEST",           lambda r: send_photos_then("✅ Xong / Bỏ qua", r.photos_before_test, max_=2)),
  ("S5_TEST",   "Bước 3: Kết quả test",             lambda r: click("✅ PASS" if r.pass_ else "❌ FAIL")),
  # nhánh FAIL: S6..S9 ; sau đó S10..S14 ; S15 = BƯỚC CUỐI
]
```

Quy tắc an toàn:

1. **Phát hiện menu lạ:** nếu prompt nhận được không khớp bước mong đợi (hoặc nhãn nút cần bấm không có), hãy bấm `❎ Hủy` hoặc gửi `/cancel`, ghi `status=UNEXPECTED_MENU` cùng nguyên văn tin nhắn và keyboard vào log, rồi **dừng cả lô**.
2. **Timeout:** bot không trả lời trong N giây (mặc định 30) thì `status=TIMEOUT` và dừng lô. Lưu ý: lúc kiểm tra, bot đã có lúc không phản hồi.
3. **Không retry mù:** chỉ retry các thao tác *trước* bước S15. Nếu lỗi xảy ra ở/sau S15 thì đánh dấu `NEEDS_MANUAL_CHECK`, không chạy lại.
4. **Bước cuối = tạo record ngay**, bot không có bước xác nhận. Vì vậy tool phải tự có **cổng xác nhận** *trước* S15:

```
[record_key=RP-001] Board 12030000025 | FAIL | cause=... | ticket=None | SN=... 
Sắp tạo Repair THẬT trên production. Gõ YES để tạo, SKIP để huỷ record này:
```

   Chế độ `--yes-all` chỉ được bật khi có cờ `--i-understand-production` và đã chạy `dry-run` thành công trên cùng file.
5. **Parse kết quả:** regex `✅ Đã tạo Repair #(\d+)`, `Thiết bị \(code\): (\S+)`, rồi ghi vào ledger và Excel kết quả.

## 5. Chống tạo trùng (`ledger.py`)

- SQLite `ledger.db`: `record_key PRIMARY KEY, payload_hash, status, repair_id, device_code, started_at, finished_at`.
- Trước khi chạy: nếu `record_key` đã có `status=CREATED`, bỏ qua; nếu `IN_PROGRESS` hoặc `NEEDS_MANUAL_CHECK`, **dừng và yêu cầu kiểm tra tay** (có thể đã tạo nhưng mất phản hồi).
- Ghi `IN_PROGRESS` *trước* S15, ghi `CREATED` ngay khi parse được `#repair_id`.
- Có thể đối chiếu thêm với tính năng `🔍 Tra cứu theo Code (Admin)` sau khi khảo sát nhánh này.

## 6. Logging và xử lý lỗi

- `logging` ra file `logs/run_*.log` và console; mỗi dòng có `record_key`, `step`, `msg_id`.
- Log nguyên văn prompt/keyboard của bot. **Không** log `api_hash`, nội dung file session hay số điện thoại.
- Exception riêng: `UnexpectedMenu`, `BotTimeout`, `ValidationError`, `SubmissionUncertain`.
- Kết thúc mỗi lần chạy: xuất `results_YYYYMMDD.xlsx` gồm `record_key, status, repair_id, device_code, error`.

## 7. CLI

```
python -m src.cli extract  --via telethon            # chỉ đọc, cập nhật boards.json (+callback_data)
python -m src.cli validate --excel repairs.xlsx      # không kết nối Telegram
python -m src.cli dry-run  --excel repairs.xlsx      # in kịch bản từng bước, KHÔNG gửi gì
python -m src.cli submit   --excel repairs.xlsx      # hỏi YES trước mỗi record
```

## 8. Dự phòng: driver Playwright (Phương án A/B)

- Khởi chạy Edge **profile riêng**: `msedge.exe --user-data-dir=%LOCALAPPDATA%\PCBA-CCVN-EdgeAutomation --remote-debugging-port=9222`. Không dùng được với profile Default trên Edge ≥136.
- `connect_over_cdp`, mở chat bằng click `a.chatlist-chat[data-peer-id="8656506505"]`. Đóng overlay "tip" (`Escape` hoặc nút X) nếu có.
- Bấm nút: `page.locator('.bubble[data-mid="{mid}"] .reply-markup-button', has_text=label)`. Luôn khoanh theo **mid của tin bot mới nhất**, tránh bấm nhầm keyboard cũ (các tin cũ vẫn giữ nút).
- Gửi text: `.input-message-input[contenteditable="true"]` → type → Enter. Xoá ô nhập trước khi gõ.
- Gửi ảnh: `page.set_input_files('input[type=file]', path)` rồi bấm Send trong popup. **Cần khảo sát thêm.**
- Không lấy được `callback_data`, nên chỉ khớp theo nhãn.

## 9. Việc cần làm trước khi code submit

1. Xác nhận bot hoạt động lại (đang không phản hồi `/help`, `/repair`).
2. Khảo sát (có người giám sát) các nhánh chưa thấy: `🔍 Tìm thiết bị`, `🗑 Discard`, `after_repair_result=FAIL`, ticket tìm thấy qua API, gửi ảnh ở bước Failure verification.
3. Hỏi admin bot xem có **môi trường test** hoặc cách xoá record test không. Nếu không có, record đầu tiên chạy thật cần kiểm tra tay.
4. Chạy `extract --via telethon` để có `callback_data` thật.
