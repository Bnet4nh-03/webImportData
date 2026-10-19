// Thao tác kiểu Excel trên bảng (máy tính): chọn vùng (kéo chuột / Shift+click), Ctrl+C / Ctrl+X / Ctrl+V,
// Delete xoá vùng, Ctrl+D điền xuống, ô vuông ở góc ô đang chọn để kéo điền. Dùng được cho cả ô chọn (select):
// giá trị dán vào được khớp với danh sách lựa chọn (Board chỉ cần mã, vd "12070000153").

/**
 * @param {object} o
 * @param {HTMLElement} o.tbody
 * @param {() => object[]} o.cols   cột đang hiện (theo thứ tự td.cell)
 * @param {() => object[]} o.rows   dòng đang hiện (theo thứ tự tr)
 * @param {(changes: {id:string, values:object}[]) => Promise<void>} o.apply
 * @param {(values: object[]) => Promise<void>} o.addRows  thêm dòng mới khi dán quá cuối bảng
 * @param {(msg:string, err?:boolean) => void} o.toast
 */
export function initSheet(o) {
  const { tbody } = o;
  let anchor = null; // {r, c}
  let focus = null; // {r, c} góc đối diện của vùng
  let drag = null; // "select" | "fill"
  let fillTo = null; // dòng cuối khi kéo ô điền

  const rect = () => {
    if (!anchor) return null;
    const f = focus ?? anchor;
    return { r0: Math.min(anchor.r, f.r), r1: Math.max(anchor.r, f.r), c0: Math.min(anchor.c, f.c), c1: Math.max(anchor.c, f.c) };
  };
  const multi = () => {
    const R = rect();
    return R && (R.r0 !== R.r1 || R.c0 !== R.c1);
  };
  const cellAt = (td) => {
    const tr = td?.closest("tr");
    if (!td?.matches("td.cell") || !tr || tr.parentElement !== tbody) return null;
    return { r: [...tbody.rows].indexOf(tr), c: Number(td.dataset.c) };
  };
  const tdAt = (r, c) => tbody.rows[r]?.querySelector(`td.cell[data-c="${c}"]`);

  /** Tô vùng chọn (gọi lại sau mỗi lần vẽ bảng). */
  function paint() {
    tbody.querySelectorAll("td.insel, td.active").forEach((td) => td.classList.remove("insel", "active", "fill-edge"));
    tbody.querySelectorAll(".fillh").forEach((x) => x.remove());
    const R = rect();
    if (!R) return;
    const rEnd = drag === "fill" && fillTo != null ? Math.max(R.r1, fillTo) : R.r1;
    for (let r = R.r0; r <= rEnd; r++) {
      for (let c = R.c0; c <= R.c1; c++) {
        const td = tdAt(r, c);
        if (!td) continue;
        if (multi() || r > R.r1) td.classList.add("insel");
        if (r > R.r1) td.classList.add("fill-edge");
      }
    }
    tdAt(anchor.r, anchor.c)?.classList.add("active");
    const corner = tdAt(rEnd, R.c1);
    if (corner) {
      const h = document.createElement("i");
      h.className = "fillh";
      h.title = "Kéo xuống để điền giống ô đang chọn";
      corner.appendChild(h);
    }
  }

  function clear() {
    anchor = focus = null;
    paint();
  }

  // ---------- Giá trị ----------
  /** Chuẩn hoá giá trị cho một cột; null = không hợp lệ (ô chọn không có lựa chọn này). */
  function normalize(col, raw) {
    const v = String(raw ?? "").replace(/\r/g, "").trim();
    if (!col.options || !v) return v;
    const low = v.toLowerCase();
    const hit = col.options.find((x) => x.toLowerCase() === low);
    if (hit) return hit;
    if (col.key === "board") {
      const code = v.split(" - ")[0].trim().toLowerCase();
      return col.options.find((x) => x.split(" - ")[0].toLowerCase() === code) ?? null;
    }
    return null;
  }

  function valueOf(row, col) {
    return String(row?.values?.[col.key] ?? "");
  }

  function tsv(R) {
    const cols = o.cols();
    const rows = o.rows();
    const clean = (v) => String(v).replace(/[\t\r\n]+/g, " ");
    const lines = [];
    for (let r = R.r0; r <= R.r1; r++) {
      const cells = [];
      for (let c = R.c0; c <= R.c1; c++) cells.push(clean(valueOf(rows[r], cols[c])));
      lines.push(cells.join("\t"));
    }
    return lines.join("\n");
  }

  /** Ghi nhiều ô: grid[r][c] = giá trị (tương đối so với r0/c0). Trả về số ô đã ghi. */
  async function write(r0, c0, grid) {
    // Ô đang gõ giữ giá trị cũ khi bảng vẽ lại -> thôi sửa ô trước khi ghi hàng loạt.
    if (tbody.contains(document.activeElement)) document.activeElement.blur();
    const cols = o.cols();
    const rows = o.rows();
    const changes = new Map();
    const fresh = [];
    let bad = 0;
    grid.forEach((line, i) => {
      const values = {};
      line.forEach((raw, j) => {
        const col = cols[c0 + j];
        if (!col || col.readonly) return;
        const v = normalize(col, raw);
        if (v === null) bad++;
        else values[col.key] = v;
      });
      if (!Object.keys(values).length) return;
      const row = rows[r0 + i];
      if (row) {
        const prev = changes.get(row.id) ?? {};
        changes.set(row.id, { ...prev, ...values });
      } else fresh.push(values);
    });
    const list = [...changes].map(([id, values]) => ({ id, values }));
    const n = list.reduce((s, x) => s + Object.keys(x.values).length, 0) + fresh.reduce((s, v) => s + Object.keys(v).length, 0);
    if (list.length) await o.apply(list);
    if (fresh.length) await o.addRows(fresh);
    o.toast(`Đã ghi ${n} ô${fresh.length ? ` (thêm ${fresh.length} dòng)` : ""}${bad ? ` · bỏ qua ${bad} ô không có trong danh sách chọn` : ""}.`, bad > 0 && n === 0);
    return n;
  }

  function parseClip(text) {
    const t = text.replace(/\r/g, "").replace(/\n$/, "");
    return t.split("\n").map((l) => l.split("\t"));
  }

  /** Dán: 1 giá trị + nhiều ô đang chọn -> điền cả vùng; bảng -> dán từ góc trên trái. */
  async function paste(text) {
    const R = rect();
    if (!R) return 0;
    let grid = parseClip(text);
    if (grid.length === 1 && grid[0].length === 1 && multi()) {
      grid = Array.from({ length: R.r1 - R.r0 + 1 }, () => Array(R.c1 - R.c0 + 1).fill(grid[0][0]));
    } else if (multi() && grid.length < R.r1 - R.r0 + 1 && (R.r1 - R.r0 + 1) % grid.length === 0) {
      // dán 1 dòng vào nhiều dòng đang chọn: lặp lại như Excel
      const k = (R.r1 - R.r0 + 1) / grid.length;
      grid = Array.from({ length: k }, () => grid).flat();
    }
    return write(R.r0, R.c0, grid);
  }

  function clearRange() {
    const R = rect();
    if (!R) return null;
    const grid = Array.from({ length: R.r1 - R.r0 + 1 }, () => Array(R.c1 - R.c0 + 1).fill(""));
    return write(R.r0, R.c0, grid);
  }

  /** Điền xuống: lặp lại các dòng nguồn [r0..srcEnd] cho tới dòng `to`. */
  function fillDown(R, srcEnd, to) {
    const cols = o.cols();
    const rows = o.rows();
    const src = [];
    for (let r = R.r0; r <= srcEnd; r++) {
      const line = [];
      for (let c = R.c0; c <= R.c1; c++) line.push(valueOf(rows[r], cols[c]));
      src.push(line);
    }
    const grid = [];
    for (let r = srcEnd + 1; r <= to; r++) grid.push(src[(r - srcEnd - 1) % src.length]);
    return grid.length ? write(srcEnd + 1, R.c0, grid) : null;
  }

  // ---------- Chuột ----------
  tbody.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    if (e.target.closest(".fillh")) {
      e.preventDefault();
      drag = "fill";
      fillTo = rect().r1;
      document.body.classList.add("sheet-drag");
      return;
    }
    const cell = cellAt(e.target.closest("td"));
    if (!cell) return;
    if (e.shiftKey && anchor) {
      e.preventDefault();
      focus = cell;
      paint();
      return;
    }
    anchor = cell;
    focus = null;
    drag = "select";
    paint();
  });

  tbody.addEventListener("mouseover", (e) => {
    if (!drag) return;
    const cell = cellAt(e.target.closest("td"));
    if (!cell) return;
    if (drag === "fill") {
      fillTo = Math.max(rect().r1, cell.r);
      paint();
      return;
    }
    if (cell.r === (focus ?? anchor).r && cell.c === (focus ?? anchor).c) return;
    focus = cell;
    if (multi()) {
      document.body.classList.add("sheet-drag");
      document.activeElement?.blur(); // đang chọn vùng: thôi sửa ô
    }
    paint();
  });

  document.addEventListener("mouseup", () => {
    if (!drag) return;
    const was = drag;
    drag = null;
    document.body.classList.remove("sheet-drag");
    if (was === "fill") {
      const R = rect();
      const to = fillTo;
      fillTo = null;
      if (R && to > R.r1) {
        focus = { r: to, c: R.c1 };
        anchor = { r: R.r0, c: R.c0 };
        fillDown(R, R.r1, to);
      }
    }
    paint();
  });

  // Bấm ra ngoài bảng: bỏ chọn vùng.
  document.addEventListener("mousedown", (e) => {
    if (!e.target.closest("#grid tbody, .modal, .drawer, .toast")) clear();
  });

  // ---------- Bàn phím + clipboard ----------
  const inGrid = () => anchor && (document.activeElement === document.body || tbody.contains(document.activeElement));
  /** Đang gõ chữ trong một ô (có bôi đen chữ) -> để trình duyệt xử lý copy/cut như bình thường. */
  const textEditing = () => {
    const a = document.activeElement;
    return !multi() && a?.matches?.("input, textarea") && a.selectionStart !== a.selectionEnd;
  };

  /** Ghi clipboard: API mới (localhost/https) hoặc cách cũ execCommand (web mở qua IP LAN, http). */
  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      const ta = Object.assign(document.createElement("textarea"), { value: text });
      ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
      document.body.appendChild(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch { /* bỏ qua */ }
      ta.remove();
      return ok;
    }
  }

  // Ctrl+C / Ctrl+X: tự xử lý (trình duyệt không phát sự kiện copy khi đang ở ô chọn select).
  async function copyRange(cut) {
    const R = rect();
    const n = (R.r1 - R.r0 + 1) * (R.c1 - R.c0 + 1);
    if (!(await copyText(tsv(R)))) return o.toast("Trình duyệt không cho sao chép.", true);
    if (cut) await clearRange();
    else o.toast(n > 1 ? `Đã sao chép ${n} ô — Ctrl+V để dán vào vùng khác hoặc Excel.` : "Đã sao chép ô.");
  }

  document.addEventListener("paste", (e) => {
    if (!inGrid()) return;
    const text = e.clipboardData?.getData("text/plain") ?? "";
    const a = document.activeElement;
    // Dán 1 giá trị vào đúng 1 ô đang gõ: để ô tự xử lý (giữ con trỏ, chèn giữa chữ).
    if (!multi() && !/[\t\n]/.test(text.replace(/\r?\n$/, "")) && a?.matches?.("input, textarea")) return;
    e.preventDefault();
    paste(text).catch((err) => o.toast(err?.message || String(err), true));
  });

  document.addEventListener("keydown", (e) => {
    if (!inGrid()) return;
    const a = document.activeElement;
    const typing = a?.matches?.("input, textarea") && !multi();
    const k = e.key.toLowerCase();
    if ((e.ctrlKey || e.metaKey) && !e.altKey && (k === "c" || k === "x") && !textEditing()) {
      e.preventDefault();
      copyRange(k === "x");
      return;
    }
    if ((e.key === "Delete" || e.key === "Backspace") && (multi() || a?.tagName === "SELECT" || a === document.body)) {
      e.preventDefault();
      clearRange();
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "d" && multi()) {
      e.preventDefault();
      const R = rect();
      fillDown(R, R.r0, R.r1);
    } else if (e.key === "Escape" && multi()) {
      focus = null;
      paint();
    } else if (e.shiftKey && e.key.startsWith("Arrow") && !typing && a?.tagName !== "SELECT") {
      e.preventDefault();
      const f = { ...(focus ?? anchor) };
      const d = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] }[e.key];
      f.r = Math.max(0, Math.min(o.rows().length - 1, f.r + d[0]));
      f.c = Math.max(0, Math.min(o.cols().length - 1, f.c + d[1]));
      focus = f;
      paint();
    }
  });

  // Chọn ô bằng phím Tab/Enter (focus vào ô khác) -> ô đó thành ô đang chọn.
  tbody.addEventListener("focusin", (e) => {
    if (drag) return;
    const cell = cellAt(e.target.closest("td"));
    if (!cell || (anchor && !multi() && anchor.r === cell.r && anchor.c === cell.c)) return;
    if (multi() && e.target.closest("td.insel")) return;
    anchor = cell;
    focus = null;
    paint();
  });

  return { paint, clear, normalize, paste, write, rect };
}
