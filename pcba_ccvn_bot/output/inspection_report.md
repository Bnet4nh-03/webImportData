# Báo cáo kiểm tra bot PCBA-CCVN

Ngày kiểm tra: 2026-10-14, khoảng 23:10–23:30 (giờ máy)

## 1. Tab và giao diện đã kiểm tra

| Mục | Giá trị |
|---|---|
| Edge chính của bạn (PID 11584, `--profile-directory=Default`) | **Không** có `--remote-debugging-port`, nên không gắn vào được. Edge 155 cũng **chặn** cổng debug trên profile mặc định (chính sách bảo mật Chromium ≥136). Tôi **không đóng** và **không can thiệp** cửa sổ này. |
| Phiên được kiểm tra | Cửa sổ Edge riêng, `--user-data-dir=%LOCALAPPDATA%\PCBA-CCVN-EdgeAutomation`, `--remote-debugging-port=9222` |
| Trạng thái đăng nhập | Profile tạo lúc 23:14:31. Khi đọc trang lần đầu thì đã đăng nhập (tài khoản "Viet Anh"). Tôi không nhập mã hay mật khẩu nào. |
| Giao diện | Telegram Web K, `https://web.telegram.org/k/#@pcba_ccvn_bot` |
| Bot | Tên hiển thị **PCBA-CCVN**, username `@pcba_ccvn_bot`, peer id `8656506505` |
| Công cụ | Playwright Python `connect_over_cdp("http://127.0.0.1:9222")` + `page.evaluate()` đọc DOM |

## 2. Phương pháp trích xuất

1. Đọc DOM chứ không dùng OCR. Mỗi tin nhắn là `div.bubble[data-mid][data-timestamp]`, nội dung nằm ở `.message`.
2. Inline keyboard là `div.reply-markup > div.reply-markup-row > button.rp.reply-markup-button`, nhãn nút nằm trong `span.reply-markup-button-text`. Emoji được render bằng `<img class="emoji" alt="✅">`, nên script thay `img` bằng `alt` để giữ đúng nhãn.
3. Cuộn ngược lịch sử 12 lần, thu được **225 tin nhắn (mid 10409 → 10634)**, gồm 6 lần tạo Repair hoàn chỉnh (#952 → #957) và 1 lần huỷ.
4. Thử live một lần: gửi `/repair` (msg 10634), nhưng **bot không phản hồi** sau hơn 20 giây. Tin `/help` lúc 22:56 (msg 10632) cũng không có phản hồi. Tôi **không bấm nút nào** và không gửi gì thêm.

## 3. Kết quả

| Hạng mục | Kết quả |
|---|---|
| Board (Bước 1) | **28 board** + nút `❎ Hủy`, mỗi hàng 1 nút |
| Phân trang | **Không có** (không có nút Next/Prev/số trang) |
| Độ ổn định | Danh sách **giống hệt** ở 8 tin nhắn: 10413, 10441, 10472, 10503, 10536, 10564, 10593, 10611 |
| Định dạng nhãn | `"{board_id} - {board_name}"`. 25 board có ID dạng số 11 chữ số, 3 board dạng `BRD_OCPP_THREE_xxxxxx` |
| Menu lồng | Menu `/repair` (4 lựa chọn) → Board → thiết bị (`Tìm`/`Thêm mới`) → nhà thầu (6 + Bỏ trống) → ảnh trước test → kết quả test (PASS/FAIL/Discard) → nhánh FAIL (failure_cause, disposition, after_repair_result, ảnh sau sửa, trạng thái trạm) → ticket → SN → Date onsite → Original phenomenon → Station code → ảnh xác minh → **tạo record** |

Chi tiết từng bước xem trong `workflow.json`.

## 4. Phân loại dữ liệu

**Quan sát trực tiếp trong giao diện (DOM):**
- Nhãn của 28 board, `board_id`, `board_name` (tách tại dấu `" - "` đầu tiên), vị trí hàng/cột, `message_id`.
- Tất cả prompt của bot và nhãn nút ở các bước, danh sách nhà thầu, định dạng ngày `M/D/YYYY`.
- Bot tạo record **ngay sau bước ảnh Failure verification**, không có bước "Xác nhận".

**Metadata có được qua kênh hợp lệ:** chỉ có `data-mid` (message id), `data-timestamp` và `data-peer-id` mà Telegram Web đưa ra trong DOM.

**Vẫn chưa biết (để `null`):**
- `callback_data`: **không có** trong DOM. Nút là `<button>` chỉ có `class`, không có `data-*` hay `aria-label`. Telegram Web K giữ callback data trong state JS nội bộ, và tôi không lấy từ đó.
- Các nhánh chưa từng xuất hiện: `🔍 Tìm thiết bị`, `🗑 Discard`, `after_repair_result = FAIL`, ticket tìm **thấy** qua API, gửi ảnh ở bước Failure verification, và 3 lựa chọn Admin trong menu `/repair`.
- Lựa chọn nào đã được bấm ở các lần trước: click inline button không sinh tin nhắn, chỉ suy ra được từ phản hồi kế tiếp của bot.

**Không kiểm tra được:**
- Trạng thái bot hiện tại: bot không trả lời `/help` và `/repair` nên không xác minh live được danh sách board mới nhất. Dữ liệu mới nhất lấy từ msg 10611 (2026-10-14 ~20:50).
- Ảnh đã gửi: không mở hay tải về.

## 5. Hướng tự động hoá đề xuất

| Phương án | Khả thi | Ghi chú |
|---|---|---|
| **C. Telegram client API (Telethon, tài khoản người dùng)** | ✅ **Khuyến nghị chính** | MTProto trả về `reply_markup` kèm `KeyboardButtonCallback.data`, nên **có callback_data hợp lệ**. Có `message.click(text=...)`, gửi ảnh trực tiếp từ file, chờ phản hồi theo message id. Cần `api_id`/`api_hash` lấy từ my.telegram.org, đăng nhập 1 lần bằng mã OTP do chính bạn nhập. Không cần trình duyệt. |
| **A+B. Playwright + CDP + selector DOM** | ✅ Đã chứng minh chạy được | Phải dùng profile Edge **riêng** có `--remote-debugging-port`; không gắn được vào profile Default. Selector ổn định: `.bubble[data-mid]`, `.reply-markup-button` lọc theo nhãn, `.input-message-input[contenteditable=true]`. Nhược điểm: phụ thuộc giao diện Telegram Web K (class có hash như `_item_5iddr_1` dễ đổi), upload ảnh phức tạp hơn, overlay "tip" có thể chặn thao tác. |
| Bot token / backend | ❌ Không phù hợp | Bot token chỉ để vận hành bot, không cho xem mã nguồn hay dữ liệu backend; client không thể dùng nó thay vai người dùng. |

## 6. Lưu ý

- Danh sách chat có **3 tin từ "Deleted Account"** (2–3/10) với nội dung kiểu "tài khoản đã bị đánh dấu khả nghi, sẽ bị buộc đăng xuất…". Đây là dấu hiệu **lừa đảo (phishing)** điển hình. Tôi không mở các chat này. Đừng bấm link hay làm theo hướng dẫn trong đó.
- Cửa sổ Edge tự động hoá có tin `/repair` (msg 10634) **đang chờ**. Nếu bot hoạt động lại, nó sẽ hiện menu; chỉ cần bấm `❎ Hủy` hoặc bỏ qua, không phát sinh record.
