# Tem Hàng Lỗi · CCVN

Chụp tem **HÀNG LỖI / BROKEN ITEM**, để AI (Gemini) đọc, đối chiếu với `ccts_tickets.db` để sửa chỗ đọc sai, rồi xuất Excel cho bot PCBA-CCVN.

**Máy tính và điện thoại dùng chung một bảng.** Server chạy trên máy tính giữ toàn bộ dữ liệu và ảnh. Chụp tem trên điện thoại thì máy tính hiện ngay, sửa trên máy tính thì điện thoại cũng thấy ngay, như hai màn hình của cùng một máy.

## Chạy

```bash
python server.py
```

- Máy tính: mở http://localhost:8000
- Điện thoại (cùng Wi-Fi): trong web bấm **📱** rồi quét mã QR, hoặc gõ địa chỉ `http://<IP-máy-tính>:8000` in ra khi khởi động.
- Lần đầu Windows hỏi tường lửa: chọn **cho phép Python trên mạng Private**.
- Lần đầu: bấm **⚙️** và dán Gemini API key (lấy miễn phí tại https://aistudio.google.com/apikey), bấm **Kiểm tra** rồi **Lưu**. Key lưu trên máy tính (`data/config.json`); điện thoại không cần nhập key.

Tùy chọn:
- `--local`: chỉ máy này truy cập (điện thoại không vào được).
- `--db "đường-dẫn\ccts_tickets.db"`: dùng DB ở chỗ khác (mặc định `D:\00_WORKSPACE\01_Python\04_Takedata_CCVN\ccts_tickets.db`).
- `--port 8080`: đổi cổng.

Khi khởi động, server nạp chỉ mục ticket/SN từ DB, mất khoảng 10 giây. Server tự nạp lại khi tool đồng bộ cập nhật DB. DB được mở **chỉ đọc**.

Không cần cài thêm thư viện; chỉ dùng thư viện chuẩn của Python 3.10 trở lên.

## Cấu trúc

```
server.py       Web + API + đồng bộ realtime (SSE) + hàng đợi đọc ảnh
ocr.py          Gọi Gemini / Ollama, prompt, chuyển 7 trường trên tem sang các cột
matching.py     Tra cứu ccts_tickets.db, chấm điểm, quyết định ticket khớp
telegram_bot.py Nhập một dòng lên bot PCBA-CCVN qua Telegram Web (Edge + Playwright), lấy mã thiết bị
tests/          Test quy trình bot bằng bot giả lập
data/           state.json (bảng), config.json (cài đặt + key), images/ (ảnh). Không chia sẻ thư mục này.
index.html, css/style.css
js/app.js       Giao diện: bảng (máy tính), thẻ (điện thoại), ngăn chi tiết đối chiếu
js/api.js       Gửi thao tác / nhận thay đổi từ server
js/columns.js   Danh sách cột, board, nhà thầu, gợi ý board theo model
js/image.js     Xoay / cắt / nén ảnh trước khi gửi
js/excel.js     Xuất .xlsx và sao chép dạng bảng
```

## Đối chiếu với ccts_tickets.db

- **Thứ tự ưu tiên**: Ticket ID (100 điểm) > **SN** (50) > mã trạm (30) > **ngày** (tối đa 20, yếu nhất). Ticket cùng SN được lấy
  làm ứng viên trước tiên. Ngày chỉ để chọn ticket gần ngày nhất trong các ticket cùng SN/trạm: đúng ngày ±1 = 20,
  lệch ≤3 ngày = 9, ≤7 = 6, ≤15 = 3, ≤30 = 1 (kỹ sư hay ghi ngày sớm/muộn hơn ngày onsite trên DB).
- **Index trong RAM** cho mã ticket / SN / mã trạm gần giống (chia chuỗi thành khúc, chỉ so các mã trùng khúc):
  tìm + chấm điểm ~10–20 ms/tem thay vì ~300 ms. Đo: `python tools/eval_matching.py --shift 7` (ngày lệch ±7).

| Cột | Trường trong DB | Cách xử lý |
|---|---|---|
| 🤖 Ticket ID | `ccts_ticket_id` | Lấy theo DB |
| 🤖 SN | `charge_box_id` | Lấy theo DB |
| 🤖 Date onsite | `onSiteCompleteTime` (raw_json) | Lấy ngày, định dạng chọn trong ⚙️ (mặc định M/D/YYYY như bot) |
| 🤖 Original phenomenon | `error_desc` | Bỏ dòng ngày giờ / mã / số điện thoại, giữ phần mô tả lỗi |
| 🤖 Station code | `station_code` | Lấy theo DB |
| Board | `charge_box_model` | Chỉ **gợi ý**: board hợp với model được xếp lên đầu danh sách. App không tự chọn board và không suy ra PASS/FAIL từ model. |
| Contractor | người phụ trách ticket | Chỉ điền khi đang trống (vd. `ES_Engineer` → ES) |
| Kết quả kiểm tra | nhập tay + ảnh | test_tool_result, after_repair_result (kèm ảnh), charging_station_test_status |

Cột Charger model đã bỏ khỏi bảng. Model vẫn được dùng để gợi ý Board.

Quy trình cho mỗi ảnh:
1. AI đọc 7 trường trên tem. Với Ticket, S/N và Mã Trạm, AI đọc **từng ký tự** và cho biết mỗi vị trí có thể là những ký tự nào,
   vd. `8MHEMQ0423EQ` → `8 M H E M [Q/O] [0/O] 4 [2/Z] 3 E [Q/O]`.
2. Server tìm trong DB các mã khớp mẫu ký tự đó. Ký tự AI chắc chắn phải khớp đúng; ở chỗ AI phân vân, DB chọn một trong các khả năng AI đưa ra.
   Ngoài ra vẫn chấp nhận ký tự dễ nhầm (0/O/Q/D, 1/I/L, 2/Z, 5/S, 8/B, 6/G) và lệch 1–2 ký tự. Nếu cả Ticket lẫn SN đều không ra, server tìm theo Mã Trạm + ngày.
3. Chấm điểm theo Ticket ID, SN, Station code, ngày onsite (lệch tối đa ±1 ngày).
   - **✓ DB**: chắc chắn một ticket. Các cột 🤖 được ghi theo DB; giá trị AI đọc từ tem vẫn được giữ để xem lại.
   - **?N**: N ticket gần giống (thường là tem chỉ có SN mà trụ có nhiều ticket). App chỉ điền những trường các ticket này giống nhau; bạn chọn ticket đúng trong ngăn chi tiết.
   - **–**: không tìm thấy.
4. Ô bạn đã gõ tay không bao giờ bị DB ghi đè. Nếu khác DB, ô chuyển đỏ.
5. Nếu vẫn chưa chắc (vd. tem chỉ có SN mà trụ có 11 ticket), server gửi ảnh tem kèm tối đa 10 ticket ứng viên cho AI so từng ký tự viết tay và chọn ticket đúng (**✓ AI**). Bước này chỉ chạy khi cần, có thể tắt trong ⚙️.

Trong ngăn chi tiết, cột **Tem (AI)** tô màu từng ký tự: **xanh** = khớp DB, **vàng** = AI phân vân và DB đã chọn (chữ nhỏ bên dưới là ký tự trong DB), **đỏ** = khác DB. Rê chuột lên ký tự để xem các khả năng AI đưa ra.

**Tốc độ** (đo với Gemini, tháng 10/2026): `gemini-flash-lite-latest` khoảng 2–4 giây/ảnh; `gemini-flash-latest` khoảng 7–9 giây/ảnh ở chế độ **Nhanh** và ~26 giây ở chế độ **Kỹ**.
Nên đọc tem bằng bản Lite và để bản Flash làm bước xác nhận. Lưu ý: gói miễn phí của `gemini-flash-latest` có hạn mức mỗi ngày khá thấp.

Màu ô: **vàng** = AI chưa chắc · **xanh lá** = đã sửa theo DB · **xanh dương** = điền từ DB · **xanh nhạt** = khớp DB · **đỏ** = khác DB.

**Ngăn chi tiết** (bấm số dòng, nút DB, hoặc chạm vào thẻ trên điện thoại) hiện từng trường theo 3 cột **Tem (AI) | DB | Đang dùng**, có nút **← Tem** / **← DB** để chọn nhanh giá trị. Ngăn này cũng có danh sách ticket gần giống kèm điểm, nút chọn ticket hoặc bỏ khớp, và phần nhập thông tin sửa chữa. Bấm **✓ Đã kiểm tra** để đánh dấu dòng và chuyển sang dòng tiếp theo.

Bảng gợi ý Board theo model nằm trong `BOARD_RULES` ở `js/columns.js`; cập nhật khi có model hoặc board mới.

## Chụp ảnh

- **Chụp tem / Chọn ảnh**: mỗi ảnh tạo một dòng mới và được đưa vào hàng đợi đọc.
- **📷 ở cột Ảnh tem**: chụp lại tem cho dòng đó. App đọc lại tem nhưng giữ các ô bạn đã sửa.
- **📷 ở cột after_repair_result**: thêm ảnh sau sửa (một dòng có thể có nhiều ảnh). Bấm vào ảnh nhỏ để xem hoặc xóa.
- **⋯ → Tải toàn bộ ảnh (.zip)**: mỗi dòng một thư mục `001_<TicketID>/tem.jpg, sau_sua_1.jpg…`.

## Bảng kiểu Excel (máy tính)

- **Chọn vùng**: kéo chuột qua các ô, hoặc bấm một ô rồi Shift+click ô khác (Shift+mũi tên để nới vùng).
- **Ctrl+C / Ctrl+X / Ctrl+V**: chép / cắt / dán, kể cả ô chọn (Board, Contractor, PASS/FAIL…). Dán **1 giá trị vào vùng nhiều ô** = điền cả vùng;
  dán bảng copy từ Excel = trải xuống/sang phải từ ô đầu (thêm dòng mới nếu dư). Giá trị cho ô chọn được khớp với danh sách (không phân biệt hoa/thường;
  Board chỉ cần mã, vd `12070000153`); giá trị không có trong danh sách bị bỏ qua và báo số ô.
  Nếu ô chọn đang xổ danh sách xuống, bấm **Esc** trước khi Ctrl+C.
- **Kéo ô vuông** ở góc ô đang chọn xuống dưới để điền giống ô đó (như Excel). **Nháy đúp ô vuông**: điền mọi ô **trống** bên dưới (không ghi đè ô đã có). **Ctrl+D**: điền xuống theo dòng đầu của vùng. **Delete**: xoá cả vùng.
- **Thu phóng bảng**: Ctrl+Alt+= / Ctrl+Alt+−, hoặc thanh trượt trong ⚙️ (lưu riêng trên từng máy).
- Dòng mới mặc định: after_repair_result = **PASS**, charging_station_test_status = **Pending** (📌 Điền nhanh vẫn ghi đè được).
  **Original phenomenon để trống**: không lấy từ DB, cũng không lấy chữ AI đọc trên tem (vẫn xem được ở cột Tem/DB trong ngăn chi tiết, bấm ← Tem / ← DB nếu cần).

- **Bấm ảnh tem trên bảng**: mở khung cố định bên trái (ảnh tem lớn → bảng so sánh Tem / DB / Đang dùng → sửa chữa → ticket gần giống → bot),
  bảng tính dời sang phải để vừa nhìn ảnh vừa sửa ô. Bấm ảnh dòng khác để chuyển dòng; kéo mép phải khung để đổi độ rộng;
  ✕ hoặc Esc để đóng.
  **Phóng to ảnh tem**: lăn chuột (hoặc chụm 2 ngón) trên ảnh, nút − % + ở góc ảnh; kéo để di chuyển, nháy đúp để vừa khung;
  kéo góc dưới phải khung ảnh để đổi chiều cao (nhớ trên máy). Nút ⤢ mở ảnh lớn toàn màn hình. Bấm số thứ tự dòng vẫn mở ngăn chi tiết như cũ.

**Điện thoại**: mỗi thẻ gồm ảnh tem bên trái, ô nhập failure_cause + disposition ở giữa, ảnh sau sửa + nút 📷 *Sau sửa* bên phải.

## Các thao tác khác

- **📊 Tổng hợp**: bộ lọc (Cần xem / Chưa khớp DB / Lỗi / Đã kiểm) + bảng đếm theo loại Board. Khi đóng khung mà đang lọc, cạnh nút hiện chip bộ lọc (✕ để bỏ lọc).
- **⋯** chứa + Cột, + Dòng, dán, xoá… và **🗄 Cập nhật DB ticket từ web**.
- **Điền nhanh**: bấm tên cột (vd. Board) để điền cho các dòng trống hoặc tất cả các dòng. Tick 📌 để ảnh chụp sau đó tự điền giá trị này.
- **+ Cột**: thêm cột riêng (Kho, Người kiểm…).
- **Dán từ Excel**: copy một vùng ô, bấm vào ô bắt đầu rồi dán.
- **⬇ Excel**: tạo file `broken_items_YYYY-MM-DD.xlsx`; mọi giá trị ghi dạng chữ để giữ số 0 ở đầu.

## Cập nhật DB ticket từ web (⚙️ → tab Đồng bộ DB)

Dùng tool `D: _WORKSPACE_Python_Takedata_CCVN` (chạy ngầm, không sửa file của tool) để tải ticket mới vào `ccts_tickets.db`.
1. Nhập **tài khoản + mật khẩu** console.cnpowercore.com → **🔑 Lưu & thử đăng nhập**. Mỗi lần đồng bộ app tự đăng nhập
   (`ccts_login.py`: lấy khóa RSA → mã hoá md5 mật khẩu như trang web → `/authen/login/validate`) để lấy cookie `ssoticket` + `token` mới.
   Mật khẩu lưu trong `data/config.json` đã mã hoá Windows DPAPI (chỉ tài khoản Windows này trên máy này giải mã được), không gửi về trình duyệt.
   Cách cũ (dán cookie/token lấy bằng F12) vẫn còn trong mục "Cách cũ".
2. **Lấy ticket mới** (hằng ngày). *Tiếp tục* / *Quét lại toàn bộ* nằm trong mục nâng cao. *Kiểm tra kết nối*: 4 request, không ghi DB.
3. Xong: server tự nạp lại DB và khớp lại các dòng chưa khớp. Khi DB cũ hơn 2 ngày, đầu trang hiện nhắc ⟳ Cập nhật.

## Nhập lên hệ thống PCBA (thay bot Telegram từ 26/10/2026)

Nút **📤 Nhập** trên mỗi dòng tạo phiếu trên **https://pcba.pcba-api.workers.dev/intake** (`pcba_api.py`), đúng các bước
của trang *Nhận bo mạch*: Check SN bo mạch → tạo phiếu → ảnh trước kiểm tra (ảnh tem) → ảnh xác minh lỗi → kiểm tra ban đầu
(PASS / FAIL / Discard) → FAIL: nguyên nhân (`failure_cause`) + xử lý (`disposition` = ghi chú, loại = Sửa chữa) → ảnh sau sửa
→ kiểm tra lại (`after_repair_result`) → kiểm tra tại trạm (`charging_station_test_status`: OK / FAIL / Not used / Not tested;
Pending = để trống). Số phiếu trả về hiện ở cột **📤 PCBA · Số phiếu**. SN trụ sạc (cột SN) ghi vào *Ghi chú chi tiết*.

- **⚙️ → 📤 Nhập PCBA**: nhập tài khoản + mật khẩu → *Lưu & thử đăng nhập*. Mật khẩu mã hoá DPAPI trong `data/config.json`.
  Sai 5 lần liền thì tài khoản bị khoá 15 phút. Mục *Nâng cao* chuyển lại bot Telegram cũ nếu cần.
- **Check SN** = SN **bo mạch** (khác SN trụ sạc trên tem). Bấm **⌗ / ⌗ QR** để chụp mã QR trên bo: web đọc mã ngay trên máy
  (BarcodeDetector hoặc `js/vendor/jsQR.js`), ghi SN rồi tự tra trên hệ thống: `✓ model · n` (bo đã có, n phiếu cũ — ô Board
  trống thì tự điền), `mới` (bo chưa có), `⚠ số phiếu` (bo đang có phiếu mở → không tạo mới). Gõ tay SN cũng được.
- **Lý do không có SN**: Không có tem / Tem hỏng / Tem không đọc được / Lý do khác (bắt buộc khi không có SN).
- **Sửa chữa** (FAIL), như trang web: `failure_cause` = Nguyên nhân lỗi; **Vị trí đã xử lý** = các vị trí + hành động, gõ kiểu
  `U12, C46 Thay thế; R5 Tháo bỏ` (không ghi hành động = Sửa chữa) hoặc thêm bằng ô "Ví dụ: U12, C46" + chọn hành động + **+ Thêm**
  trong khung chi tiết; **Loại xử lý** = Sửa chữa / Không sửa chữa (trống = Sửa chữa); `disposition` = Ghi chú xử lý.
- Discard cần lý do: lấy từ cột `disposition` (hoặc `failure_cause`).
- Lỗi giữa chừng: bấm **📤 Tiếp tục** — làm tiếp từ bước dở (nhớ id phiếu + Idempotency-Key), không tạo phiếu trùng.

## Nhập lên bot Telegram PCBA-CCVN (cũ, chọn trong ⚙️ → Nhập PCBA → Nâng cao)

Mỗi dòng có cột **📤 Bot · Mã thiết bị**. Bấm **📤 Nhập bot**, xem lại các giá trị sẽ gửi, rồi bấm **Tạo Repair trên bot**.
Server tự đi hết các bước của bot (/repair → Thêm mới → Board → … → ảnh xác minh lỗi). Khi bot trả về
`✅ Đã tạo Repair #957 … Thiết bị (code): 12070000153BO`, mã thiết bị và số Repair hiện ngay trong cột đó và được xuất cùng Excel.

- **Cần**: `pip install playwright` (không cần tải trình duyệt). App **tự mở** một cửa sổ Edge **riêng cho bot** (có cổng debug) cùng tab Telegram Web
  khi bấm 📤 Nhập bot, kể cả khi cửa sổ đó đã bị đóng. Lần đầu dùng thì quét QR đăng nhập Telegram trong cửa sổ đó; sau đó phiên đăng nhập được giữ lại.
  Edge dùng hằng ngày không điều khiển được (Edge chặn cổng debug trên profile mặc định); profile riêng nằm ở `%LOCALAPPDATA%\PCBA-CCVN-EdgeAutomation`.
- **Ảnh**: ảnh tem = ảnh *TRƯỚC KHI TEST* (bắt buộc); ảnh sau sửa (tối đa 3) và ảnh xác minh lỗi (tối đa 1) lấy từ các cột 📷.
- **Ô trống** → nút bỏ qua của bot (`🚫 Bỏ qua`, `🚫 Không có ticket`, nhà thầu bỏ trống). Date onsite được đổi sang dạng M/D/YYYY của bot.
- **PASS / Discard**: bot tự bỏ qua failure_cause, disposition, after_repair_result, ảnh sau sửa, trạng thái trạm và sang thẳng Ticket ID
  (lịch sử chat: 23 PASS, 6 Discard). Các ô FAIL có điền cũng không được gửi. Discard: bot tự ghi `Disposition: Discard` và **không cấp mã thiết bị**
  (trả về `-`), nên cột chỉ hiện "✓ Repair #N · không có mã".
- **Chưa hỗ trợ** (bot chưa từng đi nhánh này trong lịch sử chat): `after_repair_result = FAIL`, `🔍 Tìm thiết bị`.
- **Thanh tiến độ** trên cùng: số dòng đã có mã / tổng số dòng, và bước đang chạy của dòng hiện tại. Bộ lọc **Đã nhập bot / Chưa nhập bot**.
- **An toàn**: bot tạo record ngay ở bước cuối, không hỏi xác nhận, nên app bắt xác nhận trước khi chạy. Mỗi lần chỉ chạy một dòng.
  Dòng đã nhập phải tick "vẫn nhập lại" mới chạy lại được, vì nhập lại sẽ tạo Repair mới. Lỗi trước bước cuối: app bấm ❎ Hủy trên bot.
  Lỗi sau bước cuối: dòng chuyển **? Kiểm tra chat bot** và không tự chạy lại. Nhật ký đầy đủ ở `data/telegram_log.jsonl`.
- **⛔ Dừng khẩn cấp** (hiện khi đang nhập: trên thanh nhập bot, ô của dòng đang chạy, ngăn chi tiết): bấm là dừng, không hỏi lại.
  Toàn bộ dòng đang chờ bị bỏ khỏi hàng đợi. Dòng đang chạy dừng trong khoảng 1 giây (lâu hơn nếu đang tải ảnh lên) và app bấm ❎ Hủy trên bot,
  nên **chưa có Repair nào được tạo**. Nếu đã qua bước cuối thì không rút lại được: dòng chuyển **? Kiểm tra chat bot**.
- Code: `telegram_bot.py` (quy trình + điều khiển Telegram Web). Test với bot giả lập: `python -m unittest discover -s tests`.

## Xử lý lỗi

- Gemini giới hạn tốc độ: server tự chờ rồi thử lại. Hết hạn mức ngày, key sai: hàng đợi tạm dừng, banner đỏ hiện lý do. Sửa trong ⚙️ rồi bấm Lưu, các ảnh đang chờ sẽ tự chạy tiếp.
- Ảnh mờ hoặc không phải tem: dòng báo lỗi. Hãy chụp lại bằng 📷 trên dòng đó.
- Điện thoại báo mất kết nối: kiểm tra máy tính còn chạy `server.py` và cả hai cùng Wi-Fi. Web tự kết nối lại.
- Tắt server khi còn ảnh chờ đọc: lần chạy sau server đọc tiếp.

## Ollama — đọc tem ngay trên máy (đang dùng)

Không giới hạn số ảnh/ngày, ảnh không rời khỏi máy. Model: **`qwen3-vl:4b-instruct`** (3,3 GB).
Model Ollama lưu ở `D:\03_Temp\ollama\models` (biến môi trường người dùng `OLLAMA_MODELS`). Nếu xóa biến này, Ollama sẽ quay về ổ C và không thấy model.

Đã thử trên máy này (Ryzen 7 5700U, RAM 14 GB, chạy bằng CPU) với tem thật viết tay khó đọc, chụp xoay ngang:
| Model | Thời gian/ảnh | Kết quả |
|---|---|---|
| qwen3-vl:4b-instruct | ~60 s (thêm ~60 s nếu phải xoay lại) | Ticket đọc lệch 1 ký tự → DB sửa đúng `8RW7G9DG1GXS` ✅ |
| minicpm-v4.6 | ~26 s | Đọc sai quá nhiều, không khớp được DB ❌ |

Cách app làm với model trên máy:
- Prompt ngắn, trả JSON một dòng, ký tự phân vân ghi trong ngoặc vuông `A[0O]B` → model phải viết ít chữ hơn nên nhanh hơn. Giới hạn độ dài câu trả lời để model không bị lặp chữ vô tận.
- Model nhỏ đọc rất kém khi ảnh bị xoay. Thứ tự thử chiều:
  1. **Tự nhận chiều tem, không gọi AI (~50 ms)**: tem đứng thẳng thì mực xanh (giá trị viết tay) nằm chéo xuống-phải so với mực đen
     (tiêu đề + nhãn in sẵn). Đo trên tem thật (`python tools/eval_orientation.py`): đúng 60% số ca, **sai 0**, còn lại "không chắc"
     (tem viết bút đen, tem mất tiêu đề) -> dùng cách cũ. Ảnh bo mạch không có tem: luôn "không chắc".
  2. Nếu không chắc: chiều của lần thành công trước (`rotPref` trong `data/config.json`).
  3. Không khớp DB thì xoay đọc lại (tối đa 3 lần). Ảnh đọc được được lưu ở chiều đứng; đọc lại sau đó không xoay thêm.
  Cần Pillow + numpy (`pip install pillow numpy`).
- **Xoay ảnh tay**: trong ngăn chi tiết / khung trái có nút ⟲ ⟳ 180° (lưu ảnh mới, bấm ↻ Đọc lại để AI đọc theo chiều mới);
  trình xem ảnh lớn có ⟲ ⟳ (phím Q / E) chỉ để xem. Nút 📌 ghim ảnh tem ở trên cùng khi cuộn xuống nhập liệu (mặc định bật).
- Model được giữ trong RAM 30 phút giữa các ảnh. Để **Số ảnh đọc song song = 1**.
- Đối chiếu DB chịu được mã đọc sai nhiều: tìm cả các trạm có mã gần giống, chấm điểm theo phần ký tự trùng nhau.

**Tăng tốc** (⚙️ Cài đặt):
- **Kích thước ảnh gửi AI = 1024 px** (mặc định): ảnh lưu trong bảng giữ nguyên, AI nhận bản thu nhỏ. Đo trên máy này (tem thật, qwen3-vl:4b):
  1568 px ~106–120 s/ảnh, **1024 px ~65–69 s/ảnh**, cùng ra đúng ticket; 800 px thì AI đọc hỏng SN.
- **Đọc những ô nào = Mã + ngày**: AI đọc Ticket, SN trụ sạc, Mã trạm, Ngày thay thế; bỏ Model, Mã lỗi, Mô tả lỗi (đoạn chữ dài,
  vốn để trống khi nhập bot). Vẫn so Ticket từng ký tự với DB nên chính xác gần bằng Đầy đủ; không cần đọc lại lần hai.
- **Đọc những ô nào = Nhanh nhất**: AI chỉ đọc Mã trạm, Model, SN, Ngày; Ticket ID + Original phenomenon lấy từ DB theo SN + ngày (±1) + trạm.
  Chỉ nhận khi **SN khớp DB và ra đúng một ticket**, nếu không app tự đọc lại đầy đủ (đo thực tế: chỉ trạm + ngày đọc lệch đã khớp nhầm ticket khác).
  Với Ollama trên CPU, phần lớn thời gian là xử lý ảnh nên chế độ này chỉ tiết kiệm vài giây; nút **↻ Đọc đầy đủ** trong ngăn chi tiết đọc lại riêng một dòng.

40–50 ảnh/ngày ≈ 50–80 phút máy tự chạy nền. Cứ chụp liên tục trên điện thoại; ảnh vào hàng đợi và được đọc dần.
Muốn chính xác hơn (chậm gấp ~2): `ollama pull qwen3-vl:8b-instruct` (6,1 GB) rồi đổi model trong ⚙️.

## Tốc độ đọc tem (đo 26/10/2026, `python tools/bench_ai.py`)

| Mô hình | Thời gian / tem | Ghi chú |
|---|---|---|
| Ollama `qwen3-vl:4b` (CPU Ryzen 7 5700U) | ~96-113 s | ảnh 640 px không nhanh hơn 1024 px |
| Gemini `gemini-flash-lite-latest` | ~25-36 s | API xếp hàng: câu "OK" cũng 28 s |
| **Gemini `gemini-3.1-flash-lite`** | **~4-9 s** | mặc định; quá 12 s chưa trả lời thì gửi thêm 1 yêu cầu song song (`ocr.hedged`) |

Ảnh tem gửi lên Google khi dùng Gemini. Muốn đọc hoàn toàn trên máy: ⚙️ → Đọc tem → Ollama (chậm hơn ~15 lần).
Tra DB: tìm + chấm điểm 10-20 ms/tem (chỉ mục trong RAM); khởi động nạp chỉ mục từ `data/ticket_index.cache` ~2 s
(dựng lại ~17 s khi DB thay đổi).
