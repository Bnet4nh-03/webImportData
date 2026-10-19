# Phân tích: thông tin nào khai thác được để tăng tốc xử lý tem

Ngày đo: 16/10/2026. Dữ liệu: `ccts_tickets.db` (369.612 ticket, chỉ đọc), các tem đã xác nhận trong bảng, Ollama `qwen3-vl:4b-instruct` chạy CPU trên máy này.

## 1. Kết luận ngắn

1. **Thời gian chủ yếu nằm ở AI chạy trên CPU (~65–110 s/ảnh)**. Đọc ít ô hơn và cắt riêng vùng tem đều **không** làm nhanh hơn một cách ổn định.
   Cách duy nhất giảm thời gian mạnh là dùng **Gemini** (~2–9 s/ảnh, đã có sẵn trong ⚙️) hoặc máy có GPU.
2. Với model trên máy, nên tối ưu **số lần phải đọc lại và số dòng phải chọn tay**, tức là để DB "sửa" giúp những chỗ AI đọc sai.
   AI hiện đọc sai nhiều ở mọi ô: Ticket chỉ đúng hoàn toàn 1/5 tem, SN 2/5, Mã trạm 0/5, và có tem đọc ngày sai năm (2020).
3. Cấu trúc dữ liệu có 4 điểm khai thác được, trình bày ở mục 3.

## 2. Độ phân biệt của từng thông tin (trên 331.358 ticket có ngày onsite)

| Khoá dùng để tìm | Ra đúng 1 ticket |
|---|---|
| Ticket ID | **100%** (0 mã trùng) |
| SN (không có ngày) | 4,1% (TB 6,2 ticket/SN, có SN tới 494 ticket) |
| Trạm (không có ngày) | 2,2% (TB 12 ticket/trạm, có trạm tới 1.741) |
| SN + ngày ±0 | 83,3% |
| **SN + ngày ±1** | **69,9%**: cứ khoảng 3 tem thì 1 tem có nhiều ticket cùng SN, cùng ngày |
| Trạm + ngày ±1 | 39,0% |
| Trạm + SN + ngày ±1 | 70,3%: **trạm gần như không thêm gì khi đã có SN** |
| SN + ngày ±1 + mã lỗi | 78,9%: mã lỗi giúp ít (903 mã, 33% ticket không có mã lỗi) |
| SN + ngày ±1 + **2 ký tự cuối ticket** | **97,7%** |
| SN + ngày ±1 + **3 ký tự cuối ticket** | **99,4%** |
| Trạm + ngày ±1 + **4 ký tự cuối ticket** (khi SN đọc hỏng) | **99,4%** |
| Trạm + ngày ±1 + 3 ký tự cuối ticket | 97,6% |
| Chỉ ngày ±1 + 5 ký tự cuối ticket (không SN, không trạm) | 70,4% |

Ý nghĩa: **không cần đọc đúng cả 12 ký tự ticket**. Chỉ cần 3–4 ký tự cuối đúng, cộng với SN **hoặc** trạm và ngày, là gần như chắc chắn ra đúng ticket.

## 3. Bốn điểm khai thác được

**a) Mã ticket tăng theo thời gian**
Sắp ticket loại 12 ký tự theo ngày tạo, 3 ký tự đầu chỉ ngược thứ tự ở 0,04% cặp liên tiếp. Trong mỗi cửa sổ ±3 ngày chỉ có **2 kiểu 2-ký-tự-đầu**: tháng 7 là `8M/8K`, tháng 8 là `8N/8P`, tháng 9 là `8R`, tháng 10 là `8T`.
→ Đầu mã ticket đoán được từ ngày thay thế. AI đọc sai phần đầu (ví dụ `87…`, `80#…` thay vì `8T…`, `8Q…`) không còn ảnh hưởng.

**b) Đuôi ticket là phần phân biệt**
Như bảng ở mục 2: 3–4 ký tự cuối kết hợp với SN hoặc trạm và ngày là đủ.
→ Chấm điểm riêng cho **đuôi ticket** thay vì so cả chuỗi. Hiện tại đọc lệch 5/12 ký tự thì so cả chuỗi coi như không khớp.

**c) Ngày thay thế có giới hạn hợp lý**
Tem được dán gần ngày onsite. Chỉ giữ ticket cùng SN có onsite trong khoảng [ngày thay thế − 60, + 3] thì **loại được 61%** ticket ứng viên của cùng SN.
Ngày đọc sai năm (`3/1/2020`, `5/17/2020`) hoặc lệch xa ngày chụp ảnh nên được tự sửa về năm hiện tại, hoặc bỏ qua thay vì dùng để chấm điểm.

**d) Loại mã ticket theo thời gian**
Từ tháng 8/2026 gần như chỉ còn loại 12 ký tự `8…`. Loại 19 chữ số `1…` chỉ còn 44 ticket trong tháng 10.
→ Với tem mới, chỉ cần tìm trong loại 12 ký tự, trừ khi AI đọc ra chuỗi số dài.

## 4. Đã thử nhưng không hiệu quả

| Thử | Kết quả |
|---|---|
| Chỉ đọc Mã trạm, Model, SN, Ngày ("Nhanh nhất") | Khoảng 30% tem phải đọc lại đầy đủ, vì SN + ngày không đủ phân biệt |
| Cắt riêng vùng tem trước khi gửi AI (bớt ~33% điểm ảnh) | 84 / 77 / 89 s so với 111 / 77 / 79 s: không nhanh hơn ổn định, độ chính xác như nhau |
| Thu nhỏ ảnh 1568 → 1024 px | Nhanh hơn ~40% trong lần đo trước, vẫn đúng ticket (**đang dùng**) |
| Thu nhỏ còn 800 px | AI đọc hỏng SN, không dùng |

Lưu ý: thời gian đo dao động nhiều. Ollama dùng lại phần xử lý ảnh nếu cùng một ảnh, và server thật có thể đang dùng Ollama cùng lúc. Các số liệu ở trên chỉ dựa trên 3–5 tem.

## 5. Đề xuất thứ tự làm

| # | Việc | Lợi ích | Rủi ro |
|---|---|---|---|
| 1 | Chấm điểm **đuôi ticket** (3–4 ký tự cuối) + **đầu ticket đoán từ ngày** | Khớp đúng dù AI đọc sai phần đầu; giảm dòng "?N" và số lần chọn tay | Thấp: chỉ cộng thêm điểm, có test |
| 2 | **Sửa ngày hợp lý**: năm lệch thì đưa về năm của ngày chụp; ngày quá xa thì không dùng để chấm điểm | Bỏ được lỗi đọc năm 2020 | Thấp |
| 3 | **Cửa sổ ngày** cho ticket cùng SN/trạm: [−60, +3] ngày | Bớt 61% ứng viên nhiễu | Trung bình: tem dán muộn hơn 60 ngày sẽ bị loại (có thể nới rộng cửa sổ) |
| 4 | **Trạm + đuôi ticket** làm đường dự phòng khi SN đọc hỏng | 99,4% ra đúng 1 ticket | Thấp |
| 5 | Ít phải xoay ảnh đọc lại: dừng ngay khi đuôi ticket + SN/trạm khớp | Giảm trường hợp xấu nhất (đọc 3 lần × ~70 s) | Thấp |
| 6 | Dùng **Gemini** cho lô lớn (ảnh gửi lên Google) | Nhanh hơn 10–30 lần | Hạn mức miễn phí theo ngày |
