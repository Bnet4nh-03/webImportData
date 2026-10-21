// Tổng hợp theo loại Board: tổng số bo, đạt (PASS / sửa xong PASS), Discard, chưa đạt, chưa có kết quả, đã nhập bot.
// Dùng chung cho bảng "📊 Tổng hợp" trên web và sheet "Tổng hợp" khi xuất Excel.

/** Kết quả cuối của một dòng theo test_tool_result + after_repair_result. */
export function outcome(r) {
  const res = String(r.values?.test_tool_result ?? "").trim().toUpperCase();
  const after = String(r.values?.after_repair_result ?? "").trim().toUpperCase();
  if (res === "DISCARD") return "discard";
  if (res === "PASS") return "pass";
  if (res === "FAIL") return after === "PASS" ? "repaired" : after === "FAIL" ? "failed" : "pending";
  return "pending";
}

export const SUMMARY_COLS = [
  { key: "total", label: "Tổng" },
  { key: "pass", label: "PASS", tip: "test_tool_result = PASS" },
  { key: "repaired", label: "Sửa → PASS", tip: "test FAIL, sửa xong after_repair_result = PASS" },
  { key: "discard", label: "Discard", tip: "test_tool_result = Discard" },
  { key: "failed", label: "FAIL chưa đạt", tip: "test FAIL, sửa xong vẫn FAIL" },
  { key: "pending", label: "Chưa có KQ", tip: "chưa nhập test_tool_result (hoặc FAIL chưa có after_repair_result)" },
  { key: "imported", label: "Đã nhập bot", tip: "đã tạo Repair trên bot PCBA-CCVN" },
];

/** -> {rows: [{board, code, name, total, pass, …}], total: {…}} — board nhiều bo nhất lên trước. */
export function summarize(rows) {
  const by = new Map();
  const blank = () => Object.fromEntries(SUMMARY_COLS.map((c) => [c.key, 0]));
  const total = blank();
  for (const r of rows) {
    const board = String(r.values?.board ?? "").trim() || "(Chưa chọn Board)";
    if (!by.has(board)) {
      const [code, ...rest] = board.split(" - ");
      by.set(board, { board, code: code.trim(), name: rest.join(" - ").trim(), ...blank() });
    }
    const g = by.get(board);
    const o = outcome(r);
    for (const t of [g, total]) {
      t.total++;
      t[o]++;
      if (r.tg?.status === "done") t.imported++;
    }
  }
  const list = [...by.values()].sort((a, b) => b.total - a.total || a.board.localeCompare(b.board));
  return { rows: list, total };
}

/** Bảng 2 chiều cho Excel. */
export function summaryMatrix(rows) {
  const { rows: list, total } = summarize(rows);
  const header = ["Mã Board", "Tên Board", ...SUMMARY_COLS.map((c) => c.label)];
  const data = list.map((g) => [g.code, g.name, ...SUMMARY_COLS.map((c) => g[c.key])]);
  data.push(["TỔNG", "", ...SUMMARY_COLS.map((c) => total[c.key])]);
  return [header, data];
}
