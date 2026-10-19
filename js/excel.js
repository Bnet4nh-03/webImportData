// Xuất .xlsx bằng SheetJS (biến toàn cục XLSX nạp từ index.html).

function today() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** Bảng 2 chiều: tiêu đề + các dòng, mọi giá trị là chuỗi (giữ số 0 ở đầu mã/serial). */
export function toMatrix(columns, rows) {
  const header = [...columns.map((c) => c.label), "Số ảnh sau sửa", "Số ảnh xác minh lỗi", "Repair # (bot)", "Mã thiết bị (bot)"];
  const data = rows.map((r) => [
    ...columns.map((c) => String(r.values?.[c.key] ?? "")),
    String(r.photos?.length ?? 0),
    String(r.vphotos?.length ?? 0),
    String(r.tg?.repairId ?? ""),
    String(r.tg?.deviceCode ?? ""),
  ]);
  return [header, data];
}

export function exportXlsx(columns, rows) {
  const XLSX = window.XLSX;
  if (!XLSX) throw new Error("Chưa tải được thư viện Excel — kiểm tra kết nối mạng rồi tải lại trang.");
  const [header, data] = toMatrix(columns, rows);
  const ws = XLSX.utils.aoa_to_sheet([header, ...data]);
  ws["!autofilter"] = {
    ref: XLSX.utils.encode_range({ s: { r: 0, c: 0 }, e: { r: Math.max(data.length, 1), c: header.length - 1 } }),
  };
  ws["!cols"] = header.map((h, i) => {
    let w = h.length;
    for (const row of data) w = Math.max(w, row[i].length);
    return { wch: Math.min(60, Math.max(10, w + 2)) };
  });
  const wb = XLSX.utils.book_new();
  XLSX.utils.book_append_sheet(wb, ws, "Broken Items");
  const name = `broken_items_${today()}.xlsx`;
  XLSX.writeFile(wb, name);
  return name;
}

/** Bảng dạng TSV để dán thẳng vào Excel / Google Sheets. */
export function toTsv(columns, rows) {
  const clean = (v) => String(v ?? "").replace(/[\t\r\n]+/g, " ");
  const [header, data] = toMatrix(columns, rows);
  return [header, ...data].map((r) => r.map(clean).join("\t")).join("\n");
}
