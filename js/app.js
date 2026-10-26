// Giao diện. Dữ liệu nằm trên server.py; mọi thiết bị mở web đều thấy cùng một bảng theo thời gian thực.
import { BOARDS, COLUMNS, COMPARE_KEYS, DATE_FORMATS, PHOTO, boardsForModel } from "./columns.js";
import * as API from "./api.js";
import * as IMG from "./image.js";
import { exportXlsx, toTsv } from "./excel.js";
import { initSheet } from "./sheet.js";
import { decodeFile } from "./qr.js";
import { SUMMARY_COLS, summarize } from "./summary.js";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pref = {
  get: (k, d) => { try { return localStorage.getItem("ui_" + k) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem("ui_" + k, v); } catch { /* bỏ qua */ } },
};

// ---------- Trạng thái (bản sao từ server) ----------
const st = {
  rows: [],
  cols: [],
  colDefaults: {},
  dateFormat: "mdy",
  devices: [],
  worker: { paused: "", queue: 0, running: 0, ready: "" },
  connected: false,
  loaded: false,
  info: null,
  tg: { current: "", queue: [] }, // hàng đợi nhập bot (từ server)
  tgConn: null, // trạng thái Edge/Telegram Web (GET api/tg/status)
};
let filter = pref.get("filter", "all");
let search = "";
let openId = null; // dòng đang mở trong ngăn chi tiết
let uploading = 0;

const columns = () => [...COLUMNS, ...st.cols.map((c) => ({ key: c.id, label: c.label, custom: true }))];
const findRow = (id) => st.rows.find((r) => r.id === id);
const rowNo = (r) => st.rows.indexOf(r) + 1;

// ---------- Thông báo ----------
let toastTimer;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isError ? " error" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), isError ? 6000 : 2800);
}
const fail = (err) => toast(err?.message || String(err), true);
const send = (path, body) => API.post(path, body).catch((e) => { fail(e); throw e; });

// ---------- Trạng thái ô / dòng ----------
function cellState(r, key) {
  const note = r.flags?.[key];
  if (note != null) return { cls: "uncertain", tip: `AI chưa chắc: ${note}` };
  const src = r.src?.[key];
  const tem = r.tem?.[key] ?? "";
  const db = r.dbv?.[key] ?? "";
  if (src === "mismatch") return { cls: "mismatch", tip: `Khác DB — DB ghi: ${db}` };
  if (src === "fixed") return { cls: "fixed", tip: `Đã sửa theo DB — tem đọc: ${tem}` };
  if (src === "ticket") return { cls: "fromdb", tip: "Điền từ DB" };
  if (src === "same") return { cls: "same", tip: "Khớp DB" };
  return { cls: "", tip: "" };
}

/** Người dùng đã bỏ khớp để nhập tay: không đối chiếu DB nữa. */
const isManual = (r) => r.match?.how === "manual" && !r.match?.pk;

const needsReview = (r) =>
  !r.checked && (r.status === "error" || Object.keys(r.flags ?? {}).length > 0 || Object.values(r.src ?? {}).includes("mismatch") || (r.status === "done" && r.match?.status !== "matched" && !isManual(r)));

const FILTERS = [
  { key: "all", label: "Tất cả", test: () => true },
  { key: "review", label: "Cần xem", test: needsReview },
  { key: "nodb", label: "Chưa khớp DB", test: (r) => r.status !== "queued" && r.status !== "processing" && r.match?.status !== "matched" && !isManual(r) },
  { key: "error", label: "Lỗi", test: (r) => r.status === "error" },
  { key: "checked", label: "Đã kiểm", test: (r) => r.checked },
  { key: "tgdone", label: "Đã nhập", test: (r) => r.tg?.status === "done" },
  { key: "tgtodo", label: "Chưa nhập", test: (r) => r.tg?.status !== "done" },
];

function visibleRows() {
  const f = FILTERS.find((x) => x.key === filter) ?? FILTERS[0];
  const q = search.trim().toLowerCase();
  return st.rows.filter((r) => f.test(r) && (!q || Object.values(r.values ?? {}).some((v) => String(v).toLowerCase().includes(q))));
}

function statusPill(r) {
  return {
    queued: `<span class="pill wait">⏳ Chờ đọc</span>`,
    processing: `<span class="pill run"><i class="spin">◌</i> Đang đọc</span>`,
    error: `<span class="pill err" title="${esc(r.error)}">! Lỗi</span>`,
  }[r.status] ?? "";
}

function dbBadge(r) {
  if (r.status === "queued" || r.status === "processing") return "";
  const m = r.match;
  if (isManual(r)) return `<button class="db manual" data-act="open" title="Đã bỏ khớp — nhập tay, không đối chiếu DB">✎ Tay</button>`;
  if (m?.status === "matched") return `<button class="db ok" data-act="open" title="Khớp ticket ${esc(m.id)}${m.how === "manual" ? " (chọn tay)" : m.how === "ai" ? " (AI xác nhận)" : ""}">✓ ${m.how === "ai" ? "AI" : "DB"}</button>`;
  if (m?.status === "ambiguous") return `<button class="db amb" data-act="open" title="${m.alts} ticket gần giống — bấm để chọn">?${m.alts}</button>`;
  return `<button class="db none" data-act="open" title="Chưa khớp ticket — bấm để tìm">–</button>`;
}

// ---------- Nhập bo mạch: hệ thống PCBA (mặc định) hoặc bot Telegram cũ ----------
const isPcba = () => (st.tgConn?.target ?? "pcba") === "pcba";
const tgPct = (t) => (t?.total ? Math.round((100 * Math.min(t.n || 0, t.total)) / t.total) : 0);

/** Ô "Telegram" trên mỗi dòng: nút nhập, tiến độ, hoặc mã thiết bị bot trả về. */
function tgCell(r) {
  const t = r.tg ?? {};
  if (isPcba()) return pcbaCell(r, t);
  const code = t.deviceCode
    ? `<button class="devcode" data-act="copy-code" title="Repair #${esc(t.repairId)} — bấm để sao chép mã">${esc(t.deviceCode)}</button><small class="muted">Repair #${esc(t.repairId)}</small>`
    : t.repairId // Discard: bot tạo Repair nhưng không cấp mã thiết bị
      ? `<span class="pill okp" title="Bot trả về mã thiết bị '-'">✓ Repair #${esc(t.repairId)}</span><small class="muted">${esc(t.result || "")} · không có mã</small>`
      : "";
  switch (t.status) {
    case "queued":
      return `<span class="pill wait">⏳ Chờ nhập</span><button class="btn xs" data-act="tg-cancel" title="Bỏ khỏi hàng đợi">✕</button>`;
    case "running":
      return `<div class="tgrun"><div class="tgprog" title="${esc(t.step)}${t.detail ? ": " + esc(t.detail) : ""}"><i style="width:${tgPct(t)}%"></i></div>${stopBtn("xs")}</div><small class="tgstep">${t.stopping ? "⛔ Đang dừng…" : `<i class="spin">◌</i> ${t.n || 0}/${t.total || "?"} ${esc(t.step || "Đang mở bot")}`}</small>`;
    case "done":
      return code;
    case "uncertain":
      return `<button class="pill warn" data-act="open" title="${esc(t.error)}">? Kiểm tra chat bot</button>${code}`;
    case "error":
      return `<button class="pill err" data-act="open" title="${esc(t.error)}">! Lỗi nhập</button><button class="btn xs" data-act="tg" title="${esc(t.error)}">📤 Thử lại</button>`;
    default:
      return `<button class="btn xs tg-go" data-act="tg" title="Tạo Repair trên bot PCBA-CCVN cho dòng này">📤 Nhập bot</button>${code}`;
  }
}

/** Ô nhập trên mỗi dòng (hệ thống PCBA): nút nhập, tiến độ, hoặc số phiếu. */
function pcbaCell(r, t) {
  const no = t.deviceCode || t.pcba?.ticketNo || "";
  const code = no ? `<button class="devcode" data-act="copy-code" title="Phiếu PCBA ${esc(no)} — bấm để sao chép">${esc(no)}</button>` : "";
  switch (t.status) {
    case "queued":
      return `<span class="pill wait">⏳ Chờ nhập</span><button class="btn xs" data-act="tg-cancel" title="Bỏ khỏi hàng đợi">✕</button>`;
    case "running":
      return `<div class="tgrun"><div class="tgprog" title="${esc(t.step)}"><i style="width:${tgPct(t)}%"></i></div>${stopBtn("xs")}</div><small class="tgstep">${t.stopping ? "⛔ Đang dừng…" : `<i class="spin">◌</i> ${t.n || 0}/${t.total || "?"} ${esc(t.step || "Đang nhập")}`}</small>`;
    case "done":
      return `${code}<small class="muted">${esc(t.pcba?.status || t.result || "")}</small>`;
    case "error":
      return `<button class="pill err" data-act="open" title="${esc(t.error)}">! Lỗi nhập</button><button class="btn xs" data-act="tg" title="${esc(t.error)}">📤 ${no ? "Tiếp tục" : "Thử lại"}</button>${code}`;
    default:
      return `<button class="btn xs tg-go" data-act="tg" title="Tạo phiếu trên hệ thống PCBA cho dòng này">📤 Nhập</button>${code}`;
  }
}

const stopBtn = (size = "sm") =>
  `<button class="btn ${size} tgstop" data-tgstop title="Dừng ngay: bỏ hàng đợi, hủy phiên đang chạy trên bot (nếu chưa tới bước cuối)">⛔${size === "xs" ? "" : " Dừng khẩn cấp"}</button>`;

/** Dừng khẩn cấp: không hỏi lại — bấm là dừng. */
async function emergencyStop() {
  document.querySelectorAll("[data-tgstop]").forEach((b) => (b.disabled = true));
  try {
    const j = await API.post("api/tg/stop");
    toast(j.running ? `Đang dừng dòng đang chạy${j.dropped ? `, đã bỏ ${j.dropped} dòng chờ` : ""}…` : j.dropped ? `Đã bỏ ${j.dropped} dòng khỏi hàng đợi.` : "Không có dòng nào đang nhập.");
  } catch (err) {
    fail(err);
  }
}
document.addEventListener("click", (e) => {
  if (e.target.closest("[data-tgstop]")) {
    e.preventDefault();
    e.stopPropagation();
    emergencyStop();
  }
}, true);

/** Thanh trên cùng: kết nối Telegram + số dòng đã nhập + dòng đang chạy. */
function renderTgBar() {
  const el = $("#tgbar");
  if (!el) return;
  const rows = st.rows;
  const done = rows.filter((r) => r.tg?.status === "done").length;
  const bad = rows.filter((r) => r.tg?.status === "error" || r.tg?.status === "uncertain").length;
  const queued = rows.filter((r) => r.tg?.status === "queued").length;
  const c = st.tgConn;
  const pc = isPcba();
  const conn = pc
    ? `<span class="tgconn on">● Hệ thống PCBA</span>`
    : !c
    ? `<span class="tgconn">● Đang kiểm tra Telegram…</span>`
    : c.telegram
      ? `<span class="tgconn on" title="${esc(c.url)}">● Telegram Web sẵn sàng</span>`
      : `<span class="tgconn off">● ${c.edge ? "Edge đang mở nhưng chưa có tab Telegram" : "Chưa mở Edge cho bot"}</span><button class="btn xs" id="tgLaunch">Mở Telegram</button>`;
  const warn = !pc && c && !c.playwright ? `<span class="err-text small">Thiếu Playwright: pip install playwright</span>` : "";
  const cur = st.tg.current && findRow(st.tg.current);
  const job = cur
    ? `<div class="tgjob"><b>Dòng #${rowNo(cur)}</b> · Bước ${cur.tg?.n || 0}/${cur.tg?.total || "?"}: ${esc(cur.tg?.step || "mở bot")}${cur.tg?.detail ? ` — <span class="muted">${esc(String(cur.tg.detail).slice(0, 60))}</span>` : ""}
        <div class="tgprog big"><i style="width:${tgPct(cur.tg)}%"></i></div></div>`
    : "";
  const pct = rows.length ? Math.round((100 * done) / rows.length) : 0;
  const busy = Boolean(st.tg.current || st.tg.queue?.length || queued || rows.some((r) => r.tg?.status === "running"));
  // Gọn: tiến độ + trạng thái Telegram nằm ở nút nhỏ 📤 trên thanh tiêu đề; thanh chi tiết chỉ hiện khi đang nhập.
  $("#tgpillCount").textContent = `${done}/${rows.length}`;
  const dot = busy ? "busy" : bad ? "bad" : pc ? "on" : !c ? "" : c.telegram ? "on" : "off";
  $("#tgpillDot").className = "dot " + dot;
  $("#tgpill").title = pc
    ? `Nhập hệ thống PCBA: ${done}/${rows.length} dòng đã nhập${queued ? `, ${queued} chờ` : ""}${bad ? `, ${bad} lỗi (bấm để xem)` : ""}`
    : [
    `Nhập bot PCBA-CCVN: ${done}/${rows.length} dòng đã nhập${queued ? `, ${queued} chờ` : ""}${bad ? `, ${bad} lỗi (bấm để xem)` : ""}`,
    !c ? "Đang kiểm tra Telegram…" : c.telegram ? "Telegram Web sẵn sàng" : `${c.edge ? "Edge mở nhưng chưa có tab Telegram" : "Chưa mở Edge cho bot"} — bấm để mở`,
    c && !c.playwright ? "Thiếu Playwright: pip install playwright" : "",
  ].filter(Boolean).join("\n");
  $("#tglineFill").style.width = `${pct}%`;
  el.hidden = !busy;
  el.classList.toggle("busy", busy);
  el.innerHTML = busy
    ? `<div class="tgbar-head"><span class="tgbar-title">📤 Đang nhập${pc ? " PCBA" : " bot"}</span>${conn}${warn}
        <span class="tgcount">${queued ? `${queued} dòng chờ` : ""}</span><span class="grow"></span>${stopBtn()}</div>${job}`
    : "";
}

$("#tgpill").onclick = async () => {
  const c = st.tgConn;
  const bad = st.rows.some((r) => r.tg?.status === "error" || r.tg?.status === "uncertain");
  if (!isPcba() && c && !c.telegram) {
    toast("Đang mở Telegram Web cho bot…");
    try {
      const j = await API.post("api/tg/ensure");
      toast(j.ready ? "Telegram Web sẵn sàng." : j.message || "Chưa mở được Telegram Web.", !j.ready);
    } catch (err) { fail(err); }
    return refreshTgStatus();
  }
  const todo = st.rows.filter((r) => r.tg?.status !== "done").length;
  if (!todo && filter !== "tgtodo") return toast(`Cả ${st.rows.length} dòng đã nhập.`);
  filter = filter === "tgtodo" && !bad ? "all" : "tgtodo";
  pref.set("filter", filter);
  render();
};

async function refreshTgStatus() {
  try {
    const j = await API.get("api/tg/status");
    st.tgConn = j;
    st.tg = { current: j.current, queue: j.queue };
  } catch {
    st.tgConn = null;
  }
  renderTgBar();
}

let tgRowId = null;
async function openTgConfirm(r) {
  tgRowId = r.id;
  const pc = isPcba();
  $("#tgTitle").textContent = pc ? `Nhập dòng #${rowNo(r)} lên hệ thống PCBA` : `Nhập dòng #${rowNo(r)} lên bot PCBA-CCVN`;
  $("#tgBody").innerHTML = `<p class="muted">Đang kiểm tra dữ liệu…</p>`;
  $("#tgGo").disabled = true;
  $("#tgModal").hidden = false;
  let pv;
  try {
    pv = await API.get(`api/rows/${r.id}/tgpreview`);
  } catch (err) {
    $("#tgBody").innerHTML = `<p class="err-text">${esc(err.message)}</p>`;
    return;
  }
  if (tgRowId !== r.id) return;
  const t = pv.tg ?? {};
  const again = t.status === "done" || t.status === "uncertain";
  const pcNo = t.deviceCode || t.pcba?.ticketNo || "";
  const warnAgain = again && pc
    ? `<div class="tgwarn">Dòng này <b>đã nhập</b>: phiếu <b>${esc(pcNo)}</b>. Nhập lại sẽ tạo <b>thêm một phiếu mới</b>.
        <label class="check-row"><input type="checkbox" id="tgForce"> Tôi đã kiểm tra, vẫn nhập lại</label></div>`
    : !again && pc && t.status === "error" && pcNo
    ? `<div class="tgwarn ok">Lần trước dừng giữa chừng: phiếu <b>${esc(pcNo)}</b> đã tạo — lần này sẽ <b>làm tiếp</b> các bước còn lại, không tạo phiếu mới.</div>`
    : again
    ? `<div class="tgwarn">${t.status === "done" ? `Dòng này <b>đã nhập</b>: Repair #${esc(t.repairId)} · mã <b>${esc(t.deviceCode)}</b>.` : `Lần nhập trước dừng ở/ sau bước cuối — <b>có thể bot đã tạo Repair</b>. ${esc(t.error || "")}`}
        Nhập lại sẽ tạo <b>thêm một Repair mới</b>. Hãy kiểm tra chat bot trước.
        <label class="check-row"><input type="checkbox" id="tgForce"> Tôi đã kiểm tra, vẫn nhập lại</label></div>`
    : "";
  const errs = pv.errors.length ? `<ul class="tgerrs">${pv.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : "";
  const steps = `<ol class="tgsteps">${pv.steps.map((s) => `<li><span>${esc(s.label)}</span><b>${esc(s.value) || "<i>—</i>"}</b></li>`).join("")}</ol>`;
  $("#tgBody").innerHTML = pc
    ? `${errs}${warnAgain}<p class="small">Hệ thống PCBA sẽ nhận lần lượt như trang <b>Nhận bo mạch</b>: tạo phiếu, ảnh, kết quả kiểm tra.
      Số phiếu trả về được ghi vào dòng này. Lỗi giữa chừng thì bấm nhập lại để làm tiếp, không tạo phiếu trùng.</p>${steps}`
    : `${errs}<div id="tgConnBox"></div>${warnAgain}
    <p class="small">Bot sẽ nhận lần lượt các giá trị dưới đây. <b>Bot không có bước xác nhận</b>: bước cuối tạo Repair ngay, mã thiết bị trả về được ghi vào dòng này.</p>${steps}`;
  let ready = pc;
  const sync = () => ($("#tgGo").disabled = pv.errors.length > 0 || !ready || (again && !$("#tgForce")?.checked));
  $("#tgForce")?.addEventListener("change", sync);
  sync();
  if (pv.errors.length || pc) return; // dữ liệu chưa đủ / hệ thống PCBA: không cần mở Telegram
  // Tự mở Edge (profile riêng) + Telegram Web nếu chưa có, rồi kiểm tra đã đăng nhập chưa.
  const check = async () => {
    const box = $("#tgConnBox");
    if (!box) return;
    ready = false;
    sync();
    box.innerHTML = `<p class="tgconnmsg"><i class="spin">◌</i> Đang mở / kiểm tra Telegram Web…</p>`;
    let j;
    try {
      j = await API.post("api/tg/ensure");
    } catch (err) {
      j = { ready: false, state: "error", message: err.message };
    }
    if (tgRowId !== r.id || $("#tgModal").hidden) return;
    refreshTgStatus();
    ready = j.ready;
    box.innerHTML = ready
      ? `<p class="tgconnmsg ok">● Telegram Web sẵn sàng${j.launched ? " (vừa tự mở Edge cho bot)" : ""}.</p>`
      : `<div class="tgwarn">${j.state === "login" ? "🔑 " : "⚠ "}${esc(j.message || "Telegram Web chưa sẵn sàng.")}
          <div class="actions left"><button type="button" class="btn sm" id="tgRecheck">↻ Kiểm tra lại</button></div></div>`;
    $("#tgRecheck")?.addEventListener("click", check);
    sync();
  };
  check();
}

$("#tgGo").onclick = async () => {
  const id = tgRowId;
  $("#tgGo").disabled = true;
  try {
    await API.post(`api/rows/${id}/tg`, { force: Boolean($("#tgForce")?.checked) });
    $("#tgModal").hidden = true;
    toast("Đã đưa dòng vào hàng đợi nhập.");
  } catch (err) {
    fail(err);
    $("#tgGo").disabled = false;
  }
};

document.addEventListener("click", async (e) => {
  if (!e.target.closest("#tgLaunch")) return;
  e.target.disabled = true;
  toast("Đang mở Edge cho bot…");
  try {
    const j = await API.post("api/tg/ensure");
    toast(j.ready ? "Telegram Web sẵn sàng." : j.message || "Chưa mở được Telegram Web.", !j.ready);
  } catch (err) { fail(err); }
  refreshTgStatus();
});

function tgSection(r) {
  const t = r.tg ?? {};
  if (isPcba()) return pcbaSection(r, t);
  const label = { queued: "⏳ Đang chờ nhập", running: "◌ Đang nhập", done: "✓ Đã nhập", error: "! Lỗi", uncertain: "? Cần kiểm tra chat bot" }[t.status] ?? "Chưa nhập";
  const prog = t.status === "running" ? `<div class="tgprog big"><i style="width:${tgPct(t)}%"></i></div><p class="small">Bước ${t.n || 0}/${t.total || "?"}: ${esc(t.step || "")} ${t.detail ? `— ${esc(t.detail)}` : ""}</p>` : "";
  const code = t.deviceCode
    ? `<div class="tgresult">Mã thiết bị <button class="devcode big" data-act2="copy-code">${esc(t.deviceCode)}</button> <span class="muted">Repair #${esc(t.repairId)} · board ${esc(t.boardCode || "")}</span></div>`
    : t.repairId ? `<div class="tgresult">✓ Đã tạo <b>Repair #${esc(t.repairId)}</b> <span class="muted">· ${esc(t.result || "")} · bot không cấp mã thiết bị (trả về "-")</span></div>` : "";
  const err = t.error ? `<p class="${t.status === "uncertain" ? "warn-text" : "err-text"} small">${esc(t.error)}</p>` : "";
  const hist = t.history?.length ? `<p class="muted small">Lần nhập trước: ${t.history.map((h) => `#${esc(h.repairId)} (${esc(h.deviceCode)})`).join(", ")}</p>` : "";
  const log = t.log?.length ? `<details class="tglog"><summary>Nhật ký (${t.log.length})</summary><pre>${esc(t.log.slice(-25).join("\n"))}</pre></details>` : "";
  const btn = t.status === "queued"
    ? `<button class="btn sm" data-act2="tg-cancel">Bỏ khỏi hàng đợi</button>`
    : t.status === "running" ? stopBtn() :`<button class="btn sm ${t.status === "done" ? "" : "primary"}" data-act2="tg">📤 ${t.status === "done" || t.status === "uncertain" ? "Nhập lại" : t.status === "error" ? "Thử lại" : "Nhập lên bot"}</button>`;
  return `<section><div class="sec-head"><h3>Bot Telegram <span class="pill">${label}</span></h3>${btn}</div>${code}${prog}${err}${hist}${log}</section>`;
}

function pcbaSection(r, t) {
  const label = { queued: "⏳ Đang chờ nhập", running: "◌ Đang nhập", done: "✓ Đã nhập", error: "! Lỗi" }[t.status] ?? "Chưa nhập";
  const no = t.deviceCode || t.pcba?.ticketNo || "";
  const prog = t.status === "running" ? `<div class="tgprog big"><i style="width:${tgPct(t)}%"></i></div><p class="small">Bước ${t.n || 0}/${t.total || "?"}: ${esc(t.step || "")}</p>` : "";
  const code = no ? `<div class="tgresult">Số phiếu <button class="devcode big" data-act2="copy-code">${esc(no)}</button> <span class="muted">${esc(t.pcba?.status || "")}</span></div>` : "";
  const err = t.error ? `<p class="err-text small">${esc(t.error)}</p>` : "";
  const note = t.pcba?.note ? `<p class="warn-text small">${esc(t.pcba.note)}</p>` : "";
  const log = t.log?.length ? `<details class="tglog"><summary>Nhật ký (${t.log.length})</summary><pre>${esc(t.log.slice(-25).join("\n"))}</pre></details>` : "";
  const btn = t.status === "queued"
    ? `<button class="btn sm" data-act2="tg-cancel">Bỏ khỏi hàng đợi</button>`
    : t.status === "running" ? stopBtn() : `<button class="btn sm ${t.status === "done" ? "" : "primary"}" data-act2="tg">📤 ${t.status === "done" ? "Nhập lại" : t.status === "error" ? (no ? "Tiếp tục" : "Thử lại") : "Nhập lên PCBA"}</button>`;
  return `<section><div class="sec-head"><h3>Hệ thống PCBA <span class="pill">${label}</span></h3>${btn}</div>${code}${prog}${err}${note}${log}</section>`;
}

// ---------- Check SN bo mạch (quét QR) ----------
/** Kết quả tra SN bo mạch trên hệ thống PCBA (hiện cạnh ô Check SN). */
function snBadge(r, a = "data-act") {
  const k = r.snCheck;
  if (!r.values?.board_sn) return "";
  const qr = r.qr?.source === "QR" ? `<span class="snsrc" title="SN đọc từ mã QR">QR</span>` : "";
  if (!k) return `${qr}<button class="snchk" ${a}="sncheck" title="Tra SN này trên hệ thống PCBA">Check</button>`;
  if (k.status === "checking") return `${qr}<span class="snchk run"><i class="spin">◌</i></span>`;
  if (k.status === "error") return `${qr}<button class="snchk err" ${a}="sncheck" title="${esc(k.error)} — bấm để thử lại">! lỗi</button>`;
  if (k.openTicketNo) return `${qr}<span class="snchk err" title="SN này đang có phiếu mở ${esc(k.openTicketNo)} — cập nhật trên phiếu đó, không tạo mới">⚠ ${esc(k.openTicketNo)}</span>`;
  if (k.found) return `${qr}<span class="snchk ok" title="Bo đã có trên hệ thống: ${esc(k.boardModelCode)} ${esc(k.boardModelName)} · ${k.history} phiếu cũ">✓ ${esc(k.boardModelCode)}${k.history ? ` · ${k.history}` : ""}</span>`;
  return `${qr}<span class="snchk new" title="Chưa có bo này trên hệ thống — sẽ tạo mới">mới</span>`;
}

/** Chụp ảnh mã QR trên bo -> đọc SN -> ghi vào dòng (server tự tra SN). */
function scanQr(rowId) {
  const inp = document.createElement("input");
  inp.type = "file";
  inp.accept = "image/*";
  inp.capture = "environment";
  inp.onchange = async () => {
    const f = inp.files?.[0];
    if (!f) return;
    toast("Đang đọc mã QR…");
    try {
      const raw = await decodeFile(f);
      if (!raw) return toast("Không thấy mã QR trong ảnh — chụp gần hơn, rõ nét, đủ sáng.", true);
      await API.post(`api/rows/${rowId}/qr`, { raw });
      toast(`SN bo mạch: ${raw.trim().slice(0, 60)}`);
    } catch (err) {
      fail(err);
    }
  };
  inp.click();
}

// Bo đã có trên hệ thống mà ô Board còn trống: điền Board theo model đã đăng ký (một lần mỗi SN).
const boardFilled = new Set();
function autoBoardFromSn(r) {
  const k = r.snCheck;
  const code = k?.status === "ok" && k.boardModelCode;
  if (!code || r.values?.board || boardFilled.has(r.id + k.sn)) return;
  boardFilled.add(r.id + k.sn);
  const label = BOARDS.find((b) => b.split(" - ")[0] === code) || code;
  API.post(`api/rows/${r.id}/patch`, { values: { board: label } }).catch(() => {});
}

async function copyCode(r) {
  const code = r.tg?.deviceCode || r.tg?.pcba?.ticketNo;
  if (!code) return;
  try {
    await navigator.clipboard.writeText(code);
    toast(`Đã sao chép ${code}`);
  } catch {
    toast(code);
  }
}

// ---------- Ô nhập ----------
/** Ô chọn/gõ. Board: các board gợi ý theo model ticket được xếp lên đầu. */
function control(c, r, extra = "") {
  const v = r.values?.[c.key] ?? "";
  const attrs = `data-row="${r.id}" data-key="${c.key}" aria-label="${esc(c.label)}" ${extra}`;
  if (c.options) {
    const opt = (o) => `<option${o === v ? " selected" : ""}>${esc(o)}</option>`;
    const sug = c.key === "board" ? boardsForModel(r.ticket?.model || r.tem?.model) : [];
    let html = `<option value=""></option>`;
    if (v && !c.options.includes(v)) html += opt(v); // giữ giá trị lạ (vd. dán vào)
    if (sug.length) {
      html += `<optgroup label="Gợi ý theo model ${esc(r.ticket?.model || r.tem?.model)}">${sug.map(opt).join("")}</optgroup>`;
      html += `<optgroup label="Tất cả board">${c.options.filter((o) => !sug.includes(o)).map(opt).join("")}</optgroup>`;
    } else html += c.options.map(opt).join("");
    return `<select ${attrs}>${html}</select>`;
  }
  if (c.long && extra.includes("data-area")) return `<textarea ${attrs} rows="3">${esc(v)}</textarea>`;
  return `<input ${attrs} value="${esc(v)}" autocomplete="off">`;
}

const photoKind = (kind) => Object.values(PHOTO).find((p) => p.kind === kind) ?? PHOTO.after;

function photoStrip(r, p, max = 3) {
  const ph = r[p.field] ?? [];
  if (!ph.length) return "";
  const more = ph.length > max ? `<span class="more">+${ph.length - max}</span>` : "";
  return `<button class="strip" data-act="photos" data-kind="${p.kind}" title="${ph.length} ${p.name}">${ph
    .slice(0, max)
    .map((id) => `<img src="${API.imgUrl(id)}" alt="" loading="lazy">`)
    .join("")}${more}</button>`;
}

// ---------- Bảng (máy tính) ----------
function renderHead() {
  const cells = columns()
    .map((c) => {
      if (c.custom) return `<th class="custom"><button data-col="${c.key}" title="Sửa / xóa cột">${esc(c.label)}</button></th>`;
      const tip = c.db ? `AI đọc từ tem (${c.ai}), đối chiếu DB: ${c.db}` : "Nhập tay";
      return `<th class="${c.db ? "aicol" : ""}" title="${esc(tip)}"><button data-fill="${c.key}" title="Điền nhanh cho nhiều dòng">${c.db ? "🤖 " : ""}${esc(c.label)}${st.colDefaults?.[c.key] ? " 📌" : ""}</button></th>`;
    })
    .join("");
  $("#grid thead").innerHTML = `<tr><th class="sticky">#</th><th>Ảnh tem</th><th title="Đã kiểm tra">✓</th><th title="Khớp ccts_tickets.db">DB</th><th class="tgcol" title="${isPcba() ? "Nhập lên hệ thống PCBA · số phiếu trả về" : "Nhập lên bot PCBA-CCVN · mã thiết bị bot trả về"}">${isPcba() ? "📤 PCBA · Số phiếu" : "📤 Bot · Mã thiết bị"}</th>${cells}<th></th></tr>`;
}

function rowHtml(r) {
  const thumb = r.image
    ? `<img src="${API.imgUrl(r.image)}" data-act="zoom" alt="Tem" loading="lazy" title="Bấm để so sánh ảnh tem với các ô (khung bên trái)">`
    : `<span class="noimg">nhập tay</span>`;
  const cells = columns()
    .map((c, ci) => {
      const { cls, tip } = cellState(r, c.key);
      const photo = c.photo
        ? `<div class="with-photo">${control(c, r)}<button class="cam" data-act="cam" data-kind="${c.photo.kind}" title="Chụp ${c.photo.name}">📷</button>${photoStrip(r, c.photo, 2)}</div>`
        : c.qr
          ? `<div class="with-photo">${control(c, r)}<button class="cam" data-act="qr" title="Chụp mã QR trên bo mạch để lấy SN">⌗</button>${snBadge(r)}</div>`
          : control(c, r);
      return `<td class="cell ${c.wide ? "wide" : ""} ${c.options ? "opt" : ""} ${cls}" data-c="${ci}" title="${esc(tip || (c.wide ? r.values?.[c.key] ?? "" : ""))}">${photo}</td>`;
    })
    .join("");
  const cls = [`st-${r.status}`, r.checked ? "checked" : "", r.id === openId ? "open" : ""].join(" ");
  return `<tr data-id="${r.id}" class="${cls}">
    <td class="idx sticky"><button data-act="open" title="Mở chi tiết">${rowNo(r)}</button></td>
    <td class="thumb"><div class="thumb-in">${thumb}<button class="cam mini" data-act="cam-label" title="${r.image ? "Chụp lại tem" : "Chụp tem"}">📷</button></div>${statusPill(r)}</td>
    <td class="check"><input type="checkbox" data-act="check" ${r.checked ? "checked" : ""} aria-label="Đã kiểm tra"></td>
    <td class="tk">${dbBadge(r)}</td>
    <td class="tgcell tg-${esc(r.tg?.status || "none")}">${tgCell(r)}</td>
    ${cells}
    <td class="acts">
      ${r.image ? `<button data-act="retry" title="Đọc lại bằng AI">↻</button>` : ""}
      <button data-act="del" title="Xóa dòng">🗑</button>
    </td>
  </tr>`;
}

// ---------- Thẻ (điện thoại) ----------
function cardHtml(r) {
  const v = r.values ?? {};
  const title = v.ticket_id || v.sn || (r.status === "processing" || r.status === "queued" ? "Đang đọc tem…" : "Chưa có mã");
  const res = v.after_repair_result ? `<span class="pill res-${esc(v.after_repair_result.toLowerCase())}">${esc(v.after_repair_result)}</span>` : "";
  const review = needsReview(r) && r.status === "done" ? `<span class="pill warn">Cần xem</span>` : "";
  const after = r.photos ?? [];
  const afterBox = `<div class="card-after">
      ${after.length ? `<button class="after-thumb" data-act="photos" data-kind="after" title="${after.length} ảnh sau sửa"><img src="${API.imgUrl(after[after.length - 1])}" alt="" loading="lazy">${after.length > 1 ? `<b>${after.length}</b>` : ""}</button>` : ""}
      <button class="cam after-cam" data-act="cam" data-kind="after" title="Chụp ảnh sau sửa (after_repair_result)">📷<small>Sau sửa</small></button>
    </div>`;
  const inp = (key, ph) => `<input class="card-inp" data-row="${r.id}" data-key="${key}" value="${esc(v[key] ?? "")}" placeholder="${ph}" autocomplete="off" aria-label="${key}">`;
  return `<div class="card ${r.checked ? "checked" : ""} st-${r.status}" data-id="${r.id}">
    <div class="card-img" data-act="open">${r.image ? `<img src="${API.imgUrl(r.image)}" alt="" loading="lazy">` : `<span class="noimg">nhập tay</span>`}</div>
    <div class="card-main">
      <div class="card-title" data-act="open"><span class="no">#${rowNo(r)}</span> ${esc(title)} ${r.checked ? `<span class="ok-text">✓</span>` : ""}</div>
      <div class="card-sub" data-act="open">${esc([v.sn && v.ticket_id ? `SN ${v.sn}` : "", v.station_code, v.date_onsite, v.board && v.board.split(" - ")[0]].filter(Boolean).join(" · "))}</div>
      <div class="card-sn">${inp("board_sn", "Check SN bo mạch")}<button class="btn sm qrbtn" data-act="qr" title="Chụp mã QR trên bo mạch">⌗ QR</button>${snBadge(r)}</div>
      ${inp("failure_cause", "failure_cause")}
      ${inp("disposition", "disposition")}
      <div class="card-tags">${statusPill(r)}${dbBadge(r)}${review}${res}${r.tg?.status === "done" ? `<span class="pill okp" title="Repair #${esc(r.tg.repairId)}">📤 ${esc(r.tg.deviceCode || "#" + r.tg.repairId)}</span>` : r.tg?.status === "running" ? `<span class="pill run">📤 ${r.tg.n || 0}/${r.tg.total || "?"}</span>` : r.tg?.status === "queued" ? `<span class="pill wait">📤 chờ</span>` : r.tg?.status === "error" || r.tg?.status === "uncertain" ? `<span class="pill err">📤 lỗi</span>` : ""}${v.failure_verification ? `<span class="pill res-${esc(v.failure_verification.toLowerCase())}" title="Failure verification">XM ${esc(v.failure_verification)}</span>` : ""}${r.vphotos?.length ? `<span class="pill" title="ảnh xác minh lỗi">🖼 XM ${r.vphotos.length}</span>` : ""}</div>
    </div>
    ${afterBox}
  </div>`;
}

// ---------- Tổng hợp theo Board ----------
const showSummary = () => pref.get("summary", "0") === "1";

function renderSummary() {
  const el = $("#summary");
  el.hidden = !showSummary() || !st.rows.length;
  $("#btnSummary").classList.toggle("on", showSummary());
  // Bộ lọc nằm trong khung Tổng hợp; khung đóng mà đang lọc -> nhãn nhỏ cạnh nút để biết + bỏ lọc nhanh.
  const f = FILTERS.find((x) => x.key === filter);
  $("#filterBadge").innerHTML = filter !== "all" && f && el.hidden
    ? `<button class="chip on fbadge" data-clear-filter title="Đang lọc — bấm để bỏ lọc">${esc(f.label)} <b>${st.rows.filter(f.test).length}</b> ✕</button>`
    : "";
  if (el.hidden) return;
  const list = visibleRows();
  const { rows, total } = summarize(list);
  const narrowed = list.length !== st.rows.length;
  const cell = (g, c) => {
    const v = g[c.key];
    const pct = c.key !== "total" && g.total ? ` <small>${Math.round((100 * v) / g.total)}%</small>` : "";
    return `<td class="n ${c.key} ${v ? "" : "zero"}">${v}${v && c.key !== "total" && c.key !== "imported" ? pct : ""}</td>`;
  };
  $("#sumBody").innerHTML = `<div class="sum-head"><b>Theo loại Board</b>
      <span class="muted small">${narrowed ? `theo bộ lọc / tìm kiếm hiện tại: ${list.length}/${st.rows.length} dòng` : `${st.rows.length} dòng`} · bấm tên Board để lọc bảng</span>
      ${search ? `<button class="link-btn small" data-sum-clear>Bỏ lọc Board</button>` : ""}</div>
    <div class="sum-wrap"><table class="sum">
      <thead><tr><th>Board</th>${SUMMARY_COLS.map((c) => `<th class="n ${c.key}" title="${esc(c.tip || "")}">${esc(c.label)}</th>`).join("")}</tr></thead>
      <tbody>${rows.map((g) => `<tr><td class="b"><button class="link-btn" data-sum-board="${esc(g.code)}" title="${esc(g.board)}"><b>${esc(g.code)}</b> ${esc(g.name)}</button></td>${SUMMARY_COLS.map((c) => cell(g, c)).join("")}</tr>`).join("")}</tbody>
      <tfoot><tr><td class="b"><b>Tổng</b></td>${SUMMARY_COLS.map((c) => cell(total, c)).join("")}</tr></tfoot>
    </table></div>`;
}

$("#filterBadge").addEventListener("click", (e) => {
  if (!e.target.closest("[data-clear-filter]")) return;
  filter = "all";
  pref.set("filter", filter);
  render();
});
$("#btnSummary").onclick = () => {
  pref.set("summary", showSummary() ? "0" : "1");
  renderSummary();
  fitTable();
};
$("#summary").addEventListener("click", (e) => {
  const code = e.target.closest("[data-sum-board]")?.dataset.sumBoard;
  if (code === undefined && !e.target.closest("[data-sum-clear]")) return;
  search = code && code !== "(Chưa chọn Board)" ? code : "";
  $("#search").value = search;
  render();
});

// ---------- Vẽ ----------
function render() {
  renderHead();
  const list = visibleRows();
  $("#grid tbody").innerHTML = list.map(rowHtml).join("");
  $("#cards").innerHTML = list.map(cardHtml).join("");
  $("#emptyState").hidden = !st.loaded || st.rows.length > 0;
  $(".table-wrap").hidden = st.rows.length === 0;
  renderFilters();
  renderBanners();
  renderTgBar();
  renderSummary();
  sheet.paint();
  if (list.length === 0 && st.rows.length) {
    $("#cards").innerHTML = `<p class="muted center">Không có dòng nào trong bộ lọc này.</p>`;
  }
}

/** Thay phần tử, giữ nguyên ô đang gõ (giá trị trên máy này mới hơn bản server gửi về). */
function swap(oldEl, html) {
  const a = document.activeElement;
  let keep = null;
  if (a && oldEl.contains(a) && a.matches("input[data-key], textarea[data-key], select[data-key]")) {
    keep = { key: a.dataset.key, value: a.value, tag: a.tagName, s: a.selectionStart, e: a.selectionEnd };
  }
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  const n = t.content.firstElementChild;
  oldEl.replaceWith(n);
  if (keep) {
    const el = n.querySelector(`[data-key="${keep.key}"]`);
    if (el) {
      if (keep.tag !== "SELECT") el.value = keep.value;
      el.focus({ preventScroll: true });
      try { el.setSelectionRange(keep.s, keep.e); } catch { /* select/checkbox */ }
    }
  }
  return n;
}

function updateRowDom(r) {
  const tr = $(`#grid tbody tr[data-id="${r.id}"]`);
  const card = $(`#cards .card[data-id="${r.id}"]`);
  const visible = visibleRows().includes(r);
  if (!visible || !tr || !card) return render();
  swap(tr, rowHtml(r));
  swap(card, cardHtml(r));
  renderFilters();
  renderBanners();
  renderTgBar();
  renderSummary();
  sheet.paint();
}

function renderFilters() {
  const all = st.rows.length;
  // Gọn: ẩn bộ lọc đang bằng 0 hoặc trùng "Tất cả" (vd "Đã nhập bot 23/23"), trừ bộ lọc đang chọn.
  $("#filters").innerHTML = FILTERS.map((f) => {
    const n = st.rows.filter(f.test).length;
    if (f.key !== "all" && filter !== f.key && (n === 0 || n === all)) return "";
    return `<button role="tab" class="chip ${filter === f.key ? "on" : ""} ${f.key}" data-filter="${f.key}">${f.label}<b>${n}</b></button>`;
  }).join("");
}

function renderBanners() {
  const b = [];
  const w = st.worker;
  const pending = st.rows.filter((r) => r.status === "queued" || r.status === "processing").length;
  if (!st.connected && st.loaded) b.push(`<div class="banner err">⚠ Mất kết nối tới máy tính chạy server — đang thử kết nối lại…</div>`);
  if (w.paused) b.push(`<div class="banner err">⏸ Đã dừng đọc ảnh: ${esc(w.paused)} <button class="link-btn" data-open-settings>Mở cài đặt</button></div>`);
  else if (w.ready) b.push(`<div class="banner warn">${esc(w.ready)} Ảnh sẽ chờ trong hàng đợi. <button class="link-btn" data-open-settings>Mở cài đặt</button></div>`);
  if (st.info && !st.info.db.ok) b.push(`<div class="banner warn">Không mở được ccts_tickets.db — vẫn đọc tem nhưng chưa đối chiếu được DB.</div>`);
  const age = dayDiff(st.info?.db?.newest);
  if (st.info?.db?.ok && age > 2 && !st.dbsync?.running) {
    b.push(`<div class="banner warn">DB ticket cũ: ticket mới nhất ngày ${fmtDbTime(st.info.db.newest).slice(0, 10)} (${age} ngày trước) — tem mới sẽ không khớp được. <button class="link-btn" data-open-dbsync>⟳ Cập nhật DB</button></div>`);
  }
  if (st.dbsync?.running) b.push(`<div class="banner info"><i class="spin">◌</i> Đang cập nhật DB ticket từ web${st.dbsync.progress ? ` — trang ${st.dbsync.progress.page}/${st.dbsync.progress.pages}, +${st.dbsync.progress.new} ticket mới ở trang gần nhất` : "…"} <button class="link-btn" data-open-dbsync>Xem</button></div>`);
  if (uploading || pending) {
    b.push(`<div class="banner info"><i class="spin">◌</i> ${[uploading && `Đang gửi ${uploading} ảnh`, pending && `${pending} ảnh đang chờ/đang đọc`].filter(Boolean).join(" · ")}</div>`);
  }
  $("#banners").innerHTML = b.join("");
}

function renderSync() {
  const el = $("#sync");
  const n = st.devices.length;
  el.className = "sync " + (st.connected ? "on" : "off");
  el.innerHTML = st.connected ? `● ${n}` : "● Mất kết nối";
  el.title = st.connected ? `Đồng bộ · ${n} thiết bị đang mở:\n${st.devices.join("\n")}` : "Mất kết nối tới máy tính chạy server";
}

// ---------- Nhận thay đổi từ server ----------
function onEvent(ev) {
  if (ev.type === "state") {
    Object.assign(st, { rows: ev.rows, cols: ev.cols, colDefaults: ev.colDefaults, dateFormat: ev.dateFormat, loaded: true });
    render();
    if (openId) renderDrawer();
    return;
  }
  if (ev.type === "row") {
    const i = st.rows.findIndex((r) => r.id === ev.row.id);
    if (i >= 0) {
      st.rows[i] = ev.row;
      updateRowDom(ev.row);
    } else {
      st.rows.push(ev.row);
      render();
    }
    if (openId === ev.row.id) renderDrawer();
    autoBoardFromSn(ev.row);
    return;
  }
  if (ev.type === "delete") {
    st.rows = st.rows.filter((r) => r.id !== ev.id);
    if (openId === ev.id) closeDrawer();
    render();
    return;
  }
  if (ev.type === "meta") {
    Object.assign(st, { cols: ev.cols, colDefaults: ev.colDefaults });
    render();
    return;
  }
  if (ev.type === "presence") {
    st.devices = ev.devices;
    renderSync();
    return;
  }
  if (ev.type === "worker") {
    st.worker = ev;
    renderBanners();
  }
  if (ev.type === "dbsync") {
    const was = st.dbsync?.running;
    st.dbsync = ev;
    if (!$("#settings").hidden) renderDbSync();
    if (was !== ev.running) renderBanners();
    return;
  }
  if (ev.type === "dbsync_done") {
    st.dbinfo = ev.db;
    if (st.info) st.info.db = { ...st.info.db, newest: ev.db?.newest, count: ev.db?.count };
    toast(`Đã cập nhật DB: ${Number(ev.db?.count || 0).toLocaleString("vi-VN")} ticket. Đối chiếu lại ${ev.rematched} dòng chưa khớp.`);
    if (!$("#settings").hidden) renderDbSync();
    renderBanners();
    return;
  }
  if (ev.type === "tg") {
    st.tg = { current: ev.current, queue: ev.queue };
    renderTgBar();
  }
}

// ---------- Sửa ô (gửi lên server) ----------
const timers = new Map();
function queuePatch(rowId, key, value, commit) {
  const id = `${rowId}|${key}`;
  clearTimeout(timers.get(id));
  timers.delete(id);
  const r = findRow(rowId);
  if (r) r.values[key] = value; // cập nhật ngay trên máy này
  const go = () => send(`api/rows/${rowId}/patch`, { values: { [key]: value }, commit }).catch(() => {});
  if (commit) go();
  else timers.set(id, setTimeout(go, 450));
}

document.addEventListener("input", (e) => {
  const el = e.target.closest("[data-row][data-key]");
  if (!el || el.tagName === "SELECT") return;
  queuePatch(el.dataset.row, el.dataset.key, el.value, false);
  el.closest("td, .cmp-row, .field")?.classList.remove("uncertain", "fixed", "fromdb", "same", "mismatch");
});
document.addEventListener("change", (e) => {
  const el = e.target.closest("[data-row][data-key]");
  if (!el) return;
  queuePatch(el.dataset.row, el.dataset.key, el.value, true);
});
// Enter: xuống ô cùng cột ở dòng dưới.
$("#grid tbody").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || !e.target.matches("input[data-key]")) return;
  e.preventDefault();
  const next = e.target.closest("tr")[e.shiftKey ? "previousElementSibling" : "nextElementSibling"];
  next?.querySelector(`[data-key="${e.target.dataset.key}"]`)?.focus();
});

// Thao tác kiểu Excel: chọn vùng, copy/dán/xoá/điền nhiều ô (js/sheet.js).
const sheet = initSheet({
  tbody: $("#grid tbody"),
  cols: columns,
  rows: visibleRows,
  apply: async (changes) => {
    for (const ch of changes) { // cập nhật ngay trên máy này, server gửi lại bản chuẩn
      const r = findRow(ch.id);
      if (r) Object.assign(r.values, ch.values);
    }
    await API.post("api/rows/batch", { changes });
  },
  addRows: async (list) => {
    for (const values of list) await API.post("api/rows", { values });
  },
  toast,
});

// ---------- Thao tác trên dòng ----------
let camTarget = null; // {rowId, kind}
function openCamera(rowId, kind, pick = false) {
  camTarget = { rowId, kind };
  (pick ? $("#inpRowPick") : $("#inpRowCam")).click(); // phải gọi ngay trong sự kiện bấm (iOS)
}

async function rowAction(act, r, el) {
  switch (act) {
    case "open": return openDrawer(r.id);
    // Bấm ảnh tem trên bảng: mở khung so sánh bên trái (máy tính); điện thoại: xem ảnh lớn.
    case "zoom": return wide() ? openDrawer(r.id, true) : r.image && openViewer([r.image], 0, `Tem · dòng ${rowNo(r)}`);
    case "view": return r.image && openViewer([r.image], 0, `Tem · dòng ${rowNo(r)}`);
    case "photos": {
      const p = photoKind(el.dataset.kind);
      return openViewer(r[p.field], 0, `${p.name} · dòng ${rowNo(r)}`, r.id);
    }
    case "cam-label": return openCamera(r.id, "label");
    case "cam": return openCamera(r.id, el.dataset.kind);
    case "pick": return openCamera(r.id, el.dataset.kind, true);
    case "check": {
      const on = el.checked;
      if (on && Object.keys(r.flags ?? {}).length && !confirm("Dòng này còn ô AI chưa chắc chắn (màu vàng). Vẫn đánh dấu đã kiểm?")) {
        el.checked = false;
        return;
      }
      return send(`api/rows/${r.id}/patch`, { checked: on }).catch(() => {});
    }
    case "retry": {
      const edited = COMPARE_KEYS.some((k) => r.edited?.[k]);
      const reset = edited && confirm("Đọc lại tem. Ghi đè cả các ô Ticket/SN/ngày/mô tả/trạm bạn đã sửa tay?\nOK = ghi đè · Hủy = giữ ô đã sửa");
      return send(`api/rows/${r.id}/retry`, { resetEdits: reset }).catch(() => {});
    }
    case "del":
      if (!confirm(`Xóa dòng ${rowNo(r)} (kèm ảnh) trên mọi thiết bị?`)) return;
      return send(`api/rows/${r.id}/delete`).catch(() => {});
    case "tg": return openTgConfirm(r);
    case "tg-cancel": return send(`api/rows/${r.id}/tgcancel`).catch(() => {});
    case "copy-code": return copyCode(r);
    case "qr": return scanQr(r.id);
    case "sncheck": return API.post(`api/rows/${r.id}/sncheck`).catch(fail);
  }
}

for (const root of [$("#grid tbody"), $("#cards")]) {
  root.addEventListener("click", (e) => {
    const el = e.target.closest("[data-act]");
    const holder = e.target.closest("[data-id]");
    if (!el || !holder) return;
    const r = findRow(holder.dataset.id);
    if (r) rowAction(el.dataset.act, r, el);
  });
}

// ---------- Ngăn chi tiết: đối chiếu tem ↔ DB ----------
let candCache = { id: null, items: null, loading: false };

const wide = () => matchMedia("(min-width: 821px)").matches;
/** Ghim ảnh tem ở đầu ngăn chi tiết khi cuộn (mặc định bật, lưu trên máy này). */
const pinImg = () => pref.get("pinImg", "1") === "1";
const docked = () => $("#drawer").classList.contains("docked") && !$("#drawer").hidden;

/** dock = true: khung cố định bên trái (ảnh + so sánh), bảng dời sang phải để vừa xem vừa sửa.
 *  Đang ở dạng khung trái thì mở dòng khác vẫn giữ dạng đó. */
function openDrawer(id, dock = false) {
  dock = wide() && (dock || docked());
  openId = id;
  candCache = { id, items: null, loading: false };
  $("#drawer").hidden = false;
  $("#drawer").classList.toggle("docked", dock);
  document.body.classList.toggle("has-dock", dock);
  document.body.classList.toggle("noscroll", !dock);
  if (dock) document.documentElement.style.setProperty("--top-h", `${$(".topbar").offsetHeight}px`);
  renderDrawer();
  loadCandidates();
  document.querySelectorAll("#grid tbody tr.open").forEach((tr) => tr.classList.remove("open"));
  const tr = $(`#grid tbody tr[data-id="${id}"]`);
  tr?.classList.add("open");
  if (dock) tr?.scrollIntoView({ block: "nearest" });
  fitTable();
}
function closeDrawer() {
  openId = null;
  $("#drawer").hidden = true;
  $("#drawer").classList.remove("docked");
  document.body.classList.remove("noscroll", "has-dock");
  document.querySelectorAll("#grid tbody tr.open").forEach((tr) => tr.classList.remove("open"));
  fitTable();
}

// Kéo mép phải khung trái để đổi độ rộng (lưu trên máy này).
(function dockResize() {
  const set = (w) => document.documentElement.style.setProperty("--dock-w", `${Math.round(w)}px`);
  set(Number(pref.get("dockW", 460)));
  const h = document.createElement("div");
  h.className = "dock-resize";
  h.title = "Kéo để đổi độ rộng";
  $("#drawer .drawer-panel").appendChild(h);
  h.addEventListener("mousedown", (e) => {
    e.preventDefault();
    document.body.classList.add("dock-resizing");
    const move = (ev) => set(Math.max(320, Math.min(window.innerWidth * 0.7, ev.clientX)));
    const up = (ev) => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      document.body.classList.remove("dock-resizing");
      pref.set("dockW", String(Math.max(320, Math.min(window.innerWidth * 0.7, ev.clientX))));
      fitTable();
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
})();

async function loadCandidates() {
  const id = openId;
  if (!id) return;
  candCache = { id, items: candCache.id === id ? candCache.items : null, loading: true };
  renderCandidates();
  try {
    const j = await API.get(`api/rows/${id}/candidates`);
    if (openId === id) candCache = { id, items: j.items, loading: false };
  } catch (err) {
    if (openId === id) candCache = { id, items: [], loading: false, error: err.message };
  }
  renderCandidates();
}

const day = (s) => (s ? s.slice(0, 10) : "—");

function renderCandidates() {
  const box = $("#cands");
  if (!box) return;
  const r = findRow(openId);
  if (candCache.loading && !candCache.items) { box.innerHTML = `<p class="muted">Đang tìm trong DB…</p>`; return; }
  if (candCache.error) { box.innerHTML = `<p class="err-text">${esc(candCache.error)}</p>`; return; }
  const items = candCache.items ?? [];
  if (!items.length) {
    box.innerHTML = `<p class="muted">Không có ticket nào có Ticket ID hoặc SN gần giống. Kiểm tra lại Ticket ID / SN trên tem.</p>`;
    return;
  }
  const part = (p, label) => (p > 0 ? `<span class="hit">${label} +${p}</span>` : "");
  box.innerHTML = items.map(({ ticket: t, score, parts, values }) => {
    const cur = r?.match?.pk === t.pk;
    return `<div class="cand ${cur ? "current" : ""}">
      <div class="cand-head">
        <div><b>${esc(t.id)}</b> <span class="muted small">${esc(t.status)}</span></div>
        <button class="btn ${cur ? "" : "primary"} sm" data-pick="${esc(t.pk)}" ${cur ? "disabled" : ""}>${cur ? "Đang dùng" : "Chọn ticket này"}</button>
      </div>
      <div class="cand-meta">SN ${esc(t.sn)} · ${esc(t.station)} · ${esc(t.model)} · onsite ${esc(values.date_onsite || "—")} · tạo ${day(t.createTime)}</div>
      <div class="cand-score">Điểm ${score} ${part(parts.id, "Ticket")}${part(parts.sn, "SN")}${part(parts.station, "Trạm")}${part(parts.date, "Ngày")}</div>
      <div class="cand-desc">${esc(values.original_phenomenon)}</div>
    </div>`;
  }).join("");
}

function matchLine(r) {
  const m = r.match;
  if (r.status === "queued" || r.status === "processing") return `<div class="mstat wait">Đang đọc tem… sẽ tự đối chiếu khi đọc xong.</div>`;
  if (m?.status === "matched") {
    const t = r.ticket ?? {};
    const how = { manual: "(chọn tay)", ai: "(AI xác nhận khi so ảnh với các ticket ứng viên)", auto: "(tự động)" }[m.how] ?? "";
    const why = m.how === "ai" && r.confirm?.reason ? `<div class="tinfo">AI: ${esc(r.confirm.reason)}</div>` : "";
    return `<div class="mstat ok">✓ Khớp ticket <b>${esc(m.id)}</b> ${how}${why}
      <div class="tinfo">${esc(t.status)} · model <b>${esc(t.model)}</b> · tạo ${day(t.createTime)} · onsite ${day(t.onsiteTime)}<br>${esc(t.address)}</div></div>`;
  }
  const conf = r.confirm && r.confirm.confidence !== "high" && r.confirm.confidence !== "medium"
    ? `<div class="tinfo">AI xác nhận: ${r.confirm.confidence === "error" ? "lỗi — " : "chưa chắc — "}${esc(r.confirm.reason)}</div>` : "";
  if (m?.status === "ambiguous") return `<div class="mstat amb">${conf}? Có ${m.alts} ticket gần giống — chưa tự chọn được. Chọn ticket đúng ở danh sách bên dưới. Chỉ điền những trường các ticket đều giống nhau.</div>`;
  if (m?.how === "manual") return `<div class="mstat none">✎ <b>Nhập tay</b> — đã bỏ khớp ticket, app không tự đối chiếu DB cho dòng này.
    Gõ trực tiếp vào cột <b>Dùng</b> bên dưới, hoặc bấm “Chọn ticket này” nếu thấy ticket đúng trong danh sách. Bấm <b>Tự động</b> để bật lại đối chiếu.</div>`;
  return `<div class="mstat none">${conf}Không tìm thấy ticket khớp. Sửa Ticket ID / SN bên dưới (nhấn Enter) hoặc chọn ở danh sách.</div>`;
}

/** Giá trị tem tô màu từng ký tự: xanh = đúng DB, vàng = AI phân vân và DB xác nhận, đỏ = khác DB. */
function temChars(r, key, tem) {
  const diff = r.diff?.[key];
  const tip = (x) => (x.alts && x.alts.length > 1 ? `AI phân vân: ${[...x.alts].join(" / ")}` : `AI đọc: ${x.tem}`) + ` · DB: ${x.db}`;
  if (diff?.length) {
    const bad = diff.filter((x) => x.state === "bad").length;
    const res = diff.filter((x) => x.state === "resolved").length;
    const sum = bad ? `${bad} ký tự khác DB` : res ? `${res} ký tự phân vân, DB xác nhận` : "khớp từng ký tự";
    return `<span class="chars">${diff.map((x) => `<b class="ch ${x.state}" title="${esc(tip(x))}">${esc(x.tem)}${x.state !== "ok" && x.db !== x.tem ? `<sub>${esc(x.db)}</sub>` : ""}</b>`).join("")}</span><em class="${bad ? "bad" : "okc"}">${sum}</em>`;
  }
  const sets = r.chars?.[key];
  if (sets?.length && !r.edited?.[key]) {
    return `<span class="chars">${sets.map((c) => (c === null ? `<b class="ch bad" title="Không đọc được">?</b>` : `<b class="ch ${c.length > 1 ? "resolved" : ""}" title="${c.length > 1 ? esc("AI phân vân: " + [...c].join(" / ")) : ""}">${esc(c[0])}</b>`)).join("")}</span>`;
  }
  return esc(tem) || "<i>—</i>";
}

function compareTable(r) {
  const rows = COLUMNS.filter((c) => c.db).map((c) => {
    const { cls, tip } = cellState(r, c.key);
    const tem = r.tem?.[c.key] ?? "";
    const db = r.dbv?.[c.key] ?? "";
    const cur = r.values?.[c.key] ?? "";
    const icon = { same: "✓", fixed: "✎", fromdb: "＋", mismatch: "≠", uncertain: "?" }[cls] ?? "";
    const useTem = tem && tem !== cur ? `<button class="btn xs" data-use="tem" data-ukey="${c.key}">← Tem</button>` : "";
    const useDb = db && db !== cur ? `<button class="btn xs" data-use="db" data-ukey="${c.key}">← DB</button>` : "";
    return `<div class="cmp-row ${cls}" title="${esc(tip)}">
      <div class="cmp-label">${esc(c.label)}<small>${esc(c.db)}</small></div>
      <div class="cmp-val tem"><small>Tem (AI)</small>${temChars(r, c.key, tem)}${r.flags?.[c.key] != null ? `<em>? ${esc(r.flags[c.key])}</em>` : ""}</div>
      <div class="cmp-val db"><small>DB</small>${esc(db) || "<i>—</i>"}</div>
      <div class="cmp-use"><small>Dùng <span class="st-ico">${icon}</span></small>${control(c, r, c.long ? "data-area" : "")}<div class="cmp-btns">${useTem}${useDb}</div></div>
    </div>`;
  });
  return `<div class="cmp">${rows.join("")}</div>`;
}

function renderDrawer() {
  const r = findRow(openId);
  const body = $("#drawerBody");
  if (!r) return closeDrawer();
  // Giữ ô đang gõ khi dữ liệu cập nhật từ thiết bị khác.
  const old = body.firstElementChild;
  const repair = COLUMNS.filter((c) => c.group === "repair");
  const custom = st.cols.map((c) => ({ key: c.id, label: c.label, custom: true }));
  const field = (c) => `<label class="field ${cellState(r, c.key).cls}"><span>${esc(c.label)}</span>${control(c, r)}${
    c.qr ? `<div class="snrow"><button type="button" class="btn sm" data-act2="qr">⌗ Quét QR trên bo</button>${snBadge(r, "data-act2")}</div>` : ""}${
    c.photo
      ? `<div class="photo-row">${(r[c.photo.field] ?? []).map((id, i) => `<button class="ph" data-ph="${i}" data-kind="${c.photo.kind}"><img src="${API.imgUrl(id)}" alt=""></button>`).join("")}
         <button class="ph add" data-act2="cam" data-kind="${c.photo.kind}">📷<small>Chụp</small></button><button class="ph add" data-act2="pick" data-kind="${c.photo.kind}">🖼️<small>Chọn</small></button></div>`
      : ""
  }</label>`;
  const html = `<div class="dr">
    <div class="dr-head">
      <div><b>Dòng #${rowNo(r)}</b> ${statusPill(r)} ${r.checked ? `<span class="pill okp">✓ Đã kiểm</span>` : ""}</div>
      <button class="icon-btn dark" data-close title="Đóng">✕</button>
    </div>
    <div class="dr-scroll">
      <section class="dr-img ${r.image && pinImg() ? "pinned" : ""}">
        ${r.image ? `<div class="zwrap"><div class="zbox" data-zkey="${esc(r.image)}" title="Lăn chuột / chụm 2 ngón: phóng to · kéo: di chuyển · nháy đúp: vừa khung · kéo góc dưới phải: đổi chiều cao khung">
          <img src="${API.imgUrl(r.image)}" alt="Ảnh tem" draggable="false"></div>
          <div class="zoombtns">
            <button class="btn sm" data-act2="zout" title="Thu nhỏ">−</button>
            <button class="btn sm zval" data-act2="zreset" title="Vừa khung">100%</button>
            <button class="btn sm" data-act2="zin" title="Phóng to">+</button>
            <button class="btn sm" data-act2="view" title="Xem ảnh lớn toàn màn hình">⤢</button>
          </div></div>` : `<div class="noimg big">Chưa có ảnh tem</div>`}
        <div class="dr-img-btns">
          <button class="btn primary" data-act2="cam-label">📷 ${r.image ? "Chụp lại tem" : "Chụp tem"}</button>
          ${r.image ? `<button class="btn" data-act2="retry">↻ Đọc lại</button>` : ""}
          ${r.image ? `<span class="rotbtns">
            <button class="btn sm" data-act2="rot" data-deg="90" title="Xoay trái 90° (lưu ảnh, đọc lại sẽ theo chiều mới)">⟲</button>
            <button class="btn sm" data-act2="rot" data-deg="270" title="Xoay phải 90°">⟳</button>
            <button class="btn sm" data-act2="rot" data-deg="180" title="Xoay 180° (lộn ngược)">180°</button>
            <button class="btn sm ${pinImg() ? "on" : ""}" data-act2="pin" title="${pinImg() ? "Bỏ ghim: ảnh cuộn theo nội dung" : "Ghim ảnh ở trên khi cuộn xuống nhập liệu"}">📌</button>
          </span>` : ""}
          ${r.image && (r.readMode === "quick" || r.readMode === "codes") ? `<button class="btn" data-act2="retry-full" title="Đọc lại cả Ticket, Mã lỗi, Mô tả lỗi trên tem">↻ Đọc đầy đủ (cả Ticket)</button>` : ""}
        </div>
        ${r.error ? `<p class="err-text small">${esc(r.error)}</p>` : r.warn ? `<p class="warn-text small">⚠ ${esc(r.warn)}</p>` : ""}
        ${r.tem?.model || r.ms ? `<p class="muted small">${[r.tem?.model && `Model trên tem: ${esc(r.tem.model)}`, r.readMode && { quick: "⚡ Đọc nhanh (Ticket lấy từ DB)", "quick+full": "⚡ Đọc nhanh không khớp DB → đã đọc đầy đủ", codes: "⚡ Đọc mã + ngày", full: "Đọc đầy đủ" }[r.readMode], r.ms && `AI đọc ${(r.ms / 1000).toFixed(1)}s`, r.orient && (r.orient.sure ? `chiều tự nhận: xoay ${r.orient.deg}°` : "chiều: không tự nhận được"), r.confirm?.ms && `xác nhận ${(r.confirm.ms / 1000).toFixed(1)}s`].filter(Boolean).join(" · ")}</p>` : ""}
      </section>

      ${tgSection(r)}

      <section>
        <h3>Đối chiếu với ccts_tickets.db</h3>
        ${matchLine(r)}
        ${compareTable(r)}
      </section>

      <section>
        <h3>Sửa chữa</h3>
        <div class="form-grid">${repair.map(field).join("")}</div>
      </section>

      ${custom.length ? `<section><h3>Cột thêm</h3><div class="form-grid">${custom.map(field).join("")}</div></section>` : ""}

      <section>
        <div class="sec-head"><h3>Ticket gần giống</h3>
          <span><button class="btn xs" data-act2="research">🔍 Tìm lại</button>
          ${r.match?.how === "manual" ? `<button class="btn xs" data-act2="auto" title="Bật lại tự đối chiếu DB cho dòng này">Tự động</button>` : ""}
          ${isManual(r) ? "" : `<button class="btn xs danger-outline" data-act2="unlink" title="Xoá Ticket/SN/ngày/mô tả/trạm để tự nhập; dòng này không tự đối chiếu DB nữa">✎ Bỏ khớp & nhập tay</button>`}</span>
        </div>
        <div id="cands"></div>
      </section>
    </div>
    <div class="dr-foot">
      <button class="btn danger-outline" data-act2="del">🗑 Xóa dòng</button>
      <span class="grow"></span>
      <button class="btn" data-act2="prev">‹</button>
      <button class="btn" data-act2="next">›</button>
      <button class="btn ${r.checked ? "" : "primary"}" data-act2="toggle-check">${r.checked ? "Bỏ đã kiểm" : "✓ Đã kiểm tra"}</button>
    </div>
  </div>`;
  const scroll = old?.querySelector(".dr-scroll")?.scrollTop ?? 0;
  if (old) swap(old, html);
  else body.innerHTML = html;
  const sc = body.querySelector(".dr-scroll");
  if (sc) sc.scrollTop = scroll;
  applyZoom();
  renderCandidates();
}

// ---------- Phóng to / kéo ảnh tem trong ngăn chi tiết (giữ nguyên mức phóng khi dòng được vẽ lại) ----------
const Z_MAX = 8;
let zoom = { key: "", s: 1, x: 0, y: 0 };
const zbox = () => $("#drawerBody .zbox");

function applyZoom() {
  const box = zbox();
  if (!box) return;
  if (box.dataset.zkey !== zoom.key) zoom = { key: box.dataset.zkey, s: 1, x: 0, y: 0 }; // ảnh khác: vừa khung
  const h = Number(pref.get("zoomH", "0"));
  if (h) box.style.height = `${h}px`;
  box.classList.toggle("zoomed", zoom.s > 1);
  box.querySelector("img").style.transform = `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.s})`;
  const v = $("#drawerBody .zval");
  if (v) v.textContent = `${Math.round(zoom.s * 100)}%`;
}

function clampZoom(box) {
  const w = box.clientWidth, h = box.clientHeight;
  zoom.s = Math.min(Z_MAX, Math.max(1, zoom.s));
  zoom.x = Math.min(0, Math.max(w - w * zoom.s, zoom.x));
  zoom.y = Math.min(0, Math.max(h - h * zoom.s, zoom.y));
}

/** Phóng tới mức s, giữ điểm (px, py) của khung đứng yên (mặc định: giữa khung). */
function zoomTo(s, px, py) {
  const box = zbox();
  if (!box) return;
  px ??= box.clientWidth / 2;
  py ??= box.clientHeight / 2;
  const k = Math.min(Z_MAX, Math.max(1, s)) / zoom.s;
  zoom.x = px - (px - zoom.x) * k;
  zoom.y = py - (py - zoom.y) * k;
  zoom.s *= k;
  clampZoom(box);
  applyZoom();
}

const boxPoint = (box, e) => {
  const b = box.getBoundingClientRect();
  return [e.clientX - b.left, e.clientY - b.top];
};

$("#drawerBody").addEventListener("wheel", (e) => {
  const box = e.target.closest(".zbox");
  if (!box) return;
  e.preventDefault();
  zoomTo(zoom.s * Math.exp(-e.deltaY * 0.0015), ...boxPoint(box, e));
}, { passive: false });

$("#drawerBody").addEventListener("dblclick", (e) => {
  const box = e.target.closest(".zbox");
  if (!box) return;
  if (zoom.s > 1) zoomTo(1);
  else zoomTo(2.5, ...boxPoint(box, e));
});

// Kéo (1 ngón / chuột) để di chuyển ảnh đã phóng; chụm 2 ngón để phóng to.
const zPtr = new Map();
let zPinch = null;
$("#drawerBody").addEventListener("pointerdown", (e) => {
  const box = e.target.closest(".zbox");
  if (!box || e.button > 0) return;
  const b = box.getBoundingClientRect();
  if (e.clientX > b.right - 18 && e.clientY > b.bottom - 18) return; // góc đổi chiều cao khung
  zPtr.set(e.pointerId, [e.clientX, e.clientY]);
  box.setPointerCapture(e.pointerId);
  if (zPtr.size === 2) {
    const [a, c] = [...zPtr.values()];
    zPinch = { d: Math.hypot(a[0] - c[0], a[1] - c[1]), s: zoom.s };
  }
});
$("#drawerBody").addEventListener("pointermove", (e) => {
  const last = zPtr.get(e.pointerId);
  const box = zbox();
  if (!last || !box) return;
  zPtr.set(e.pointerId, [e.clientX, e.clientY]);
  if (zPtr.size === 2 && zPinch) {
    const [a, c] = [...zPtr.values()];
    const b = box.getBoundingClientRect();
    zoomTo(zPinch.s * Math.hypot(a[0] - c[0], a[1] - c[1]) / zPinch.d, (a[0] + c[0]) / 2 - b.left, (a[1] + c[1]) / 2 - b.top);
    return;
  }
  if (zoom.s <= 1) return;
  zoom.x += e.clientX - last[0];
  zoom.y += e.clientY - last[1];
  clampZoom(box);
  applyZoom();
});
const zEnd = (e) => {
  zPtr.delete(e.pointerId);
  if (zPtr.size < 2) zPinch = null;
};
$("#drawerBody").addEventListener("pointerup", zEnd);
$("#drawerBody").addEventListener("pointercancel", zEnd);
// Kéo góc dưới phải để đổi chiều cao khung ảnh: nhớ cho lần sau.
document.addEventListener("mouseup", () => {
  const box = zbox();
  if (box && box.offsetHeight && Math.abs(box.offsetHeight - Number(pref.get("zoomH", "0"))) > 4 && box.style.height) {
    pref.set("zoomH", String(box.offsetHeight));
    clampZoom(box);
    applyZoom();
  }
});

$("#drawer").addEventListener("click", async (e) => {
  if (e.target.closest("[data-close]")) return closeDrawer();
  const r = findRow(openId);
  if (!r) return;
  const pick = e.target.closest("[data-pick]")?.dataset.pick;
  if (pick) {
    await send(`api/rows/${r.id}/match`, { pk: pick }).catch(() => {});
    toast("Đã áp dụng ticket cho dòng này.");
    return setTimeout(loadCandidates, 400);
  }
  const use = e.target.closest("[data-use]");
  if (use) return send(`api/rows/${r.id}/use`, { key: use.dataset.ukey, source: use.dataset.use }).catch(() => {});
  const ph = e.target.closest("[data-ph]");
  if (ph) {
    const p = photoKind(ph.dataset.kind);
    return openViewer(r[p.field], Number(ph.dataset.ph), `${p.name} · dòng ${rowNo(r)}`, r.id);
  }
  const actEl = e.target.closest("[data-act2]");
  const act = actEl?.dataset.act2;
  if (!act) return;
  const list = visibleRows();
  const i = list.indexOf(r);
  switch (act) {
    case "view": case "cam-label": case "cam": case "pick": case "retry": case "del":
    case "tg": case "tg-cancel": case "copy-code": case "qr": case "sncheck":
      return rowAction(act, r, actEl);
    case "research": return loadCandidates();
    case "rot": {
      const reading = r.status === "queued" || r.status === "processing";
      actEl.disabled = true;
      try {
        await send(`api/rows/${r.id}/rotate`, { deg: Number(actEl.dataset.deg) });
      } catch {
        actEl.disabled = false;
        return;
      }
      return toast(reading ? "Đã xoay ảnh tem — sẽ tự đọc lại theo chiều mới." : "Đã xoay ảnh tem. Bấm ↻ Đọc lại để AI đọc theo chiều mới.");
    }
    case "zin": return zoomTo(zoom.s * 1.5);
    case "zout": return zoomTo(zoom.s / 1.5);
    case "zreset": return zoomTo(1);
    case "pin":
      pref.set("pinImg", pinImg() ? "0" : "1");
      return renderDrawer();
    case "retry-full": return send(`api/rows/${r.id}/retry`, { resetEdits: false, mode: "full" }).catch(() => {});
    case "auto": await send(`api/rows/${r.id}/match`, { pk: null }).catch(() => {}); return setTimeout(loadCandidates, 400);
    case "unlink": {
      const filled = COMPARE_KEYS.filter((k) => r.values?.[k]).length;
      const reading = r.status === "queued" || r.status === "processing";
      if (reading && !confirm("Dòng này đang chờ AI đọc. Dừng đọc và chuyển sang nhập tay?")) return;
      if (filled && !reading && !confirm("Bỏ khớp và XOÁ TRỐNG Ticket ID, SN, Date onsite, Original phenomenon, Station code để tự nhập tay?\nDòng này sẽ không tự đối chiếu DB nữa (bấm Tự động để bật lại).")) return;
      await send(`api/rows/${r.id}/match`, { pk: "", clear: true }).catch(() => {});
      toast("Đã bỏ khớp — nhập tay các ô bên dưới. Có thể bấm “Chọn ticket này” ở danh sách nếu tìm thấy ticket đúng.");
      setTimeout(() => {
        const el = $(`#drawer .cmp-use [data-key="ticket_id"]`);
        el?.scrollIntoView({ block: "center" });
        el?.focus();
      }, 250);
      return;
    }
    case "toggle-check": {
      if (!r.checked && Object.keys(r.flags ?? {}).length && !confirm("Dòng này còn ô AI chưa chắc chắn. Vẫn đánh dấu đã kiểm?")) return;
      await send(`api/rows/${r.id}/patch`, { checked: !r.checked }).catch(() => {});
      if (!r.checked && list[i + 1]) openDrawer(list[i + 1].id); // kiểm xong -> sang dòng tiếp
      return;
    }
    case "prev": if (list[i - 1]) openDrawer(list[i - 1].id); return;
    case "next": if (list[i + 1]) openDrawer(list[i + 1].id); return;
  }
});
$("#drawer").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.matches("input[data-key]")) { e.preventDefault(); e.target.blur(); }
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("#viewer").hidden) return ($("#viewer").hidden = true);
  if (!$("#drawer").hidden) closeDrawer();
});

// ---------- Xem ảnh ----------
const viewer = { imgs: [], i: 0, rowId: null };
function openViewer(imgs, i, title, rowId = null) {
  if (!imgs?.length) return;
  Object.assign(viewer, { imgs: [...imgs], i, rowId, title });
  showViewer();
  $("#viewer").hidden = false;
}
function showViewer() {
  const n = viewer.imgs.length;
  $("#viewerImg").src = API.imgUrl(viewer.imgs[viewer.i]);
  $("#viewerTitle").textContent = viewer.title;
  $("#viewerCount").textContent = n > 1 ? `${viewer.i + 1} / ${n}` : "";
  $("#viewerPrev").hidden = $("#viewerNext").hidden = n < 2;
  $("#viewerDel").hidden = !viewer.rowId;
  viewer.rot = 0;
  rotateViewer(0);
}
/** Xoay ảnh đang xem (chỉ để xem, không lưu). Ảnh xoay ngang được thu cho vừa khung. */
function rotateViewer(step) {
  viewer.rot = ((viewer.rot ?? 0) + step + 360) % 360;
  const img = $("#viewerImg");
  const side = viewer.rot % 180 !== 0;
  img.style.transform = viewer.rot ? `rotate(${viewer.rot}deg)` : "";
  img.classList.toggle("sideways", side);
}
$("#viewerRotL").onclick = () => rotateViewer(-90);
$("#viewerRotR").onclick = () => rotateViewer(90);
document.addEventListener("keydown", (e) => {
  if ($("#viewer").hidden || e.target.matches("input, textarea, select")) return;
  if (e.key === "q" || e.key === "Q") rotateViewer(-90);
  if (e.key === "e" || e.key === "E") rotateViewer(90);
});
$("#viewerPrev").onclick = () => { viewer.i = (viewer.i - 1 + viewer.imgs.length) % viewer.imgs.length; showViewer(); };
$("#viewerNext").onclick = () => { viewer.i = (viewer.i + 1) % viewer.imgs.length; showViewer(); };
$("#viewerClose").onclick = () => ($("#viewer").hidden = true);
$("#viewerImg").onclick = () => ($("#viewer").hidden = true);
$("#viewerDel").onclick = async () => {
  if (!confirm("Xóa ảnh này?")) return;
  const img = viewer.imgs[viewer.i];
  await send(`api/rows/${viewer.rowId}/photodel`, { img }).catch(() => {});
  viewer.imgs.splice(viewer.i, 1);
  if (!viewer.imgs.length) { $("#viewer").hidden = true; return; }
  viewer.i = Math.min(viewer.i, viewer.imgs.length - 1);
  showViewer();
};

// ---------- Thêm ảnh ----------
async function prepare(files, retake) {
  const out = [];
  for (let i = 0; i < files.length; i++) {
    let canvas;
    try {
      canvas = await IMG.fileToCanvas(files[i]);
    } catch {
      toast(`Không mở được ảnh "${files[i].name}".`, true);
      continue;
    }
    if ($("#chkEdit").checked) {
      const res = await IMG.editImage(canvas, { counter: files.length > 1 ? `${i + 1}/${files.length}` : "", onRetake: retake });
      if (res.action !== "ok") continue;
      canvas = res.canvas;
    }
    out.push(await IMG.canvasToJpeg(canvas));
  }
  return out;
}

async function sendImages(kind, rowId, files, retake) {
  const blobs = await prepare([...files], retake);
  uploading += blobs.length;
  renderBanners();
  let ok = 0;
  for (const b of blobs) {
    try {
      await API.upload(kind, rowId, b);
      ok++;
    } catch (err) {
      fail(err);
    } finally {
      uploading--;
      renderBanners();
    }
  }
  if (ok) toast(kind !== "label" ? `Đã thêm ${ok} ${photoKind(kind).name}.` : rowId ? "Đã thay ảnh tem, đang đọc lại…" : ok > 1 ? `Đã gửi ${ok} ảnh tem.` : "Đã gửi ảnh tem, đang đọc…");
}

$("#inpCamera").onchange = (e) => { sendImages("label", null, e.target.files, () => $("#inpCamera").click()); e.target.value = ""; };
$("#inpGallery").onchange = (e) => { sendImages("label", null, e.target.files); e.target.value = ""; };
for (const id of ["#inpRowCam", "#inpRowPick"]) {
  $(id).onchange = (e) => {
    const t = camTarget;
    const files = [...e.target.files];
    e.target.value = "";
    if (!t || !files.length) return;
    sendImages(t.kind, t.rowId, t.kind === "label" ? files.slice(0, 1) : files);
  };
}
$("#chkEdit").checked = pref.get("editBefore", "0") === "1";
$("#chkEdit").onchange = (e) => pref.set("editBefore", e.target.checked ? "1" : "0");

// ---------- Thu phóng bảng (mỗi máy một mức, lưu trong trình duyệt) ----------
const ZOOM_MIN = 60, ZOOM_MAX = 130;
function setZoom(pct) {
  const z = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, Math.round(Number(pct) / 5) * 5 || 100));
  document.documentElement.style.setProperty("--grid-zoom", String(z / 100));
  $("#setZoom").value = String(z);
  $("#setZoomVal").textContent = `${z}%`;
  pref.set("gridZoom", String(z));
  fitTable();
}
$("#setZoom").oninput = (e) => setZoom(e.target.value);
document.addEventListener("keydown", (e) => {
  if (!(e.ctrlKey && e.altKey)) return;
  if (e.key === "=" || e.key === "+") { e.preventDefault(); setZoom(Number(pref.get("gridZoom", 100)) + 5); }
  if (e.key === "-") { e.preventDefault(); setZoom(Number(pref.get("gridZoom", 100)) - 5); }
});

/** Bảng chiếm hết phần còn lại của màn hình (cuộn trong bảng, đầu bảng luôn thấy). */
function fitTable() {
  const w = $(".table-wrap");
  if (!w || w.hidden) return;
  document.documentElement.style.setProperty("--wrap-top", `${Math.max(0, w.getBoundingClientRect().top + window.scrollY)}px`);
}
window.addEventListener("resize", fitTable);
new ResizeObserver(fitTable).observe(document.querySelector("main"));
$("#cards").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.matches("input[data-key]")) e.target.blur(); // lưu ô nhập trên thẻ
});

// ---------- Thanh công cụ ----------
$("#filters").addEventListener("click", (e) => {
  const f = e.target.closest("[data-filter]")?.dataset.filter;
  if (!f) return;
  filter = f;
  pref.set("filter", f);
  render();
});
$("#search").addEventListener("input", (e) => { search = e.target.value; render(); });

$("#btnAddRow").onclick = async () => {
  closeMenu();
  try {
    const j = await API.post("api/rows", {});
    setTimeout(() => openDrawer(j.id), 150);
  } catch (err) { fail(err); }
};
$("#btnExport").onclick = () => {
  if (!st.rows.length) return toast("Chưa có dữ liệu để xuất.");
  const pending = st.rows.filter((r) => r.status === "queued" || r.status === "processing").length;
  const review = st.rows.filter(needsReview).length;
  const warn = [pending && `${pending} ảnh đang đọc`, review && `${review} dòng cần xem lại`].filter(Boolean);
  if (warn.length && !confirm(`Còn ${warn.join(", ")}. Vẫn xuất Excel?`)) return;
  try { toast(`Đã tạo ${exportXlsx(columns(), st.rows)}`); } catch (err) { fail(err); }
};
$("#btnCopy").onclick = async () => {
  closeMenu();
  if (!st.rows.length) return toast("Chưa có dữ liệu.");
  try {
    await navigator.clipboard.writeText(toTsv(columns(), st.rows));
    toast("Đã sao chép — dán vào Excel/Google Sheets.");
  } catch {
    toast("Trình duyệt không cho sao chép (cần mở bằng localhost). Hãy dùng Xuất Excel.", true);
  }
};
const closeMenu = () => document.querySelector("details.menu")?.removeAttribute("open");
$("#btnRematch").onclick = async () => { closeMenu(); await send("api/rematch").catch(() => {}); toast("Đang đối chiếu lại toàn bộ bảng với DB…"); };
$("#btnRetryErrors").onclick = async () => { closeMenu(); await send("api/retry-errors").catch(() => {}); };
$("#btnClearAll").onclick = async () => {
  closeMenu();
  if (!st.rows.length) return;
  if (!confirm(`Xóa toàn bộ ${st.rows.length} dòng và ảnh trên MỌI thiết bị? Nên xuất Excel + tải ảnh trước.`)) return;
  if (prompt('Gõ "XOA" để xác nhận') !== "XOA") return;
  send("api/clear").catch(() => {});
};
document.addEventListener("click", (e) => {
  if (!e.target.closest("details.menu")) closeMenu();
  if (e.target.closest("[data-open-settings]")) openSettings();
  if (e.target.closest("[data-open-dbsync]")) { closeMenu(); openSettings("db"); }
  if (e.target.closest("[data-close-modal]")) e.target.closest(".modal").hidden = true;
});

// ---------- Cột tùy chỉnh + điền nhanh ----------
let editingCol = null;
function openColModal(col) {
  editingCol = col;
  $("#colTitle").textContent = col ? "Sửa cột" : "Cột mới";
  $("#colName").value = col?.label ?? "";
  $("#colDefault").value = col?.def ?? "";
  $("#colFill").checked = !col;
  $("#colDelete").hidden = !col;
  $("#colModal").hidden = false;
  $("#colName").focus();
}
$("#btnAddCol").onclick = () => { closeMenu(); openColModal(null); };
$("#colForm").onsubmit = async (e) => {
  e.preventDefault();
  const label = $("#colName").value.trim();
  if (!label) return;
  if (columns().some((c) => c.label.toLowerCase() === label.toLowerCase() && c.key !== editingCol?.id)) return toast("Đã có cột trùng tên.", true);
  const def = $("#colDefault").value.trim();
  const cols = st.cols.map((c) => ({ ...c }));
  let col = cols.find((c) => c.id === editingCol?.id);
  if (col) Object.assign(col, { label, def });
  else cols.push((col = { id: "c" + Math.random().toString(36).slice(2, 10), label, def }));
  await send("api/cols", { cols, fillId: $("#colFill").checked ? col.id : null }).catch(() => {});
  $("#colModal").hidden = true;
};
$("#colDelete").onclick = async () => {
  if (!editingCol || !confirm(`Xóa cột "${editingCol.label}" và toàn bộ dữ liệu trong cột?`)) return;
  await send("api/cols", { cols: st.cols.filter((c) => c.id !== editingCol.id) }).catch(() => {});
  $("#colModal").hidden = true;
};

let fillCol = null;
$("#grid thead").addEventListener("click", (e) => {
  const id = e.target.closest("button[data-col]")?.dataset.col;
  if (id) return openColModal(st.cols.find((c) => c.id === id));
  const key = e.target.closest("button[data-fill]")?.dataset.fill;
  if (!key) return;
  fillCol = COLUMNS.find((c) => c.key === key);
  $("#fillTitle").textContent = `Điền nhanh: ${fillCol.label}`;
  $("#fillField").innerHTML = control(fillCol, { id: "", values: { [key]: st.colDefaults?.[key] ?? "" } }, 'id="fillValue"').replace(/data-row="" data-key="[^"]*"/, "");
  $("#fillDefault").checked = Boolean(st.colDefaults?.[key]);
  $("#fillModal").hidden = false;
});
async function applyFill(mode) {
  const value = $("#fillValue").value.trim();
  try {
    const j = await API.post("api/fill", { key: fillCol.key, value, mode, setDefault: $("#fillDefault").checked });
    toast(mode === "none" ? "Đã lưu giá trị mặc định." : `Đã điền ${j.n} dòng.`);
    $("#fillModal").hidden = true;
  } catch (err) { fail(err); }
}
$("#fillEmpty").onclick = () => applyFill("empty");
$("#fillAll").onclick = () => confirm(`Ghi đè cột "${fillCol.label}" ở tất cả ${st.rows.length} dòng?`) && applyFill("all");
$("#fillSaveOnly").onclick = () => applyFill("none");

// ---------- Kết nối điện thoại (QR) ----------
let qrLib;
function loadQr() {
  qrLib ??= new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://cdn.jsdelivr.net/npm/qrcode-generator@1.4.4/qrcode.min.js";
    s.onload = () => resolve(window.qrcode);
    s.onerror = reject;
    document.head.appendChild(s);
  });
  return qrLib;
}
$("#btnPhone").onclick = async () => {
  $("#phoneModal").hidden = false;
  const body = $("#phoneBody");
  const info = st.info ?? (await API.get("api/info").catch(() => null));
  const here = location.origin;
  const urls = info?.urls?.length ? info.urls : [];
  if (!info?.lan) {
    body.innerHTML = `<p>Server đang chạy chế độ <b>chỉ máy này</b>. Tắt server và chạy lại bằng <code>python server.py</code> (không có <code>--local</code>) để điện thoại vào được.</p>`;
    return;
  }
  const isLocal = /localhost|127\.0\.0\.1/.test(here);
  const main = isLocal ? urls[0] : here;
  body.innerHTML = `<p>Điện thoại và máy tính phải dùng <b>cùng Wi-Fi</b>. Quét mã bằng camera điện thoại:</p>
    <div id="qr" class="qr"></div>
    <p class="center"><a href="${esc(main)}" target="_blank"><b>${esc(main || "Không tìm thấy IP mạng LAN")}</b></a></p>
    ${urls.length > 1 ? `<p class="muted small">Địa chỉ khác của máy tính: ${urls.slice(1).map(esc).join(" · ")}</p>` : ""}
    <p class="note">Không vào được? Cho phép Python trong <b>Windows Defender Firewall</b> (mạng Private) hoặc mở cổng ${esc(location.port || "8000")}.
    Thiết bị đang kết nối: ${st.devices.map(esc).join(", ") || "—"}</p>`;
  if (!main) return;
  try {
    const qr = (await loadQr())(0, "M");
    qr.addData(main);
    qr.make();
    $("#qr").innerHTML = qr.createSvgTag({ cellSize: 6, margin: 2, scalable: true });
  } catch {
    $("#qr").innerHTML = `<p class="muted small">Không tải được thư viện QR (cần Internet). Gõ địa chỉ bên dưới vào trình duyệt điện thoại.</p>`;
  }
};

// ---------- Cài đặt (lưu trên server) ----------
let cfg = null;
$("#setDateFormat").innerHTML = Object.entries(DATE_FORMATS).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
function showProviderFields() {
  const p = $("#setProvider").value;
  document.querySelectorAll("#settings [data-provider]").forEach((el) => (el.hidden = !el.dataset.provider.split(" ").includes(p)));
}
function settingsTab(name) {
  document.querySelectorAll("#settings [data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  document.querySelectorAll("#settings [data-tabpane]").forEach((p) => (p.hidden = p.dataset.tabpane !== name));
  if (name === "db") loadDbSync();
  if (name === "pcba") fillPcbaFields();
}
function fillPcbaFields() {
  if (!cfg) return;
  $("#setPcbaUser").value = cfg.pcbaUser ?? "";
  $("#setPcbaPass").value = "";
  $("#setPcbaPass").placeholder = cfg.pcbaPassHint ? "Đã lưu (mã hoá) — để trống để giữ nguyên" : "mật khẩu hệ thống PCBA";
  $("#setImportTarget").value = cfg.importTarget ?? "pcba";
}
$("#btnPcbaLogin").onclick = async () => {
  const msg = $("#pcbaMsg");
  const body = { pcbaUser: $("#setPcbaUser").value.trim(), importTarget: $("#setImportTarget").value };
  if ($("#setPcbaPass").value) body.pcbaPass = $("#setPcbaPass").value;
  $("#btnPcbaLogin").disabled = true;
  msg.textContent = "Đang đăng nhập…";
  try {
    cfg = (await API.post("api/config", body)).config;
    fillPcbaFields();
    const j = await API.post("api/pcba/login", {});
    msg.textContent = `✔ Đăng nhập được${j.name ? ` — ${j.name}` : ""}.`;
  } catch (err) {
    msg.textContent = `✖ ${err?.message || err}`;
  } finally {
    $("#btnPcbaLogin").disabled = false;
    refreshTgStatus().then(render);
  }
};
$("#setImportTarget").onchange = async () => {
  try {
    cfg = (await API.post("api/config", { importTarget: $("#setImportTarget").value })).config;
  } catch (err) { fail(err); }
  refreshTgStatus().then(render);
};
$("#settings").addEventListener("click", (e) => {
  const t = e.target.closest("[data-tab]")?.dataset.tab;
  if (t) settingsTab(t);
});

// ---------- Đồng bộ ccts_tickets.db từ web (⚙️ → Đồng bộ DB) ----------
const dayDiff = (s) => (s ? Math.floor((Date.now() - new Date(s.replace(" ", "T")).getTime()) / 864e5) : null);
const fmtDbTime = (s) => (s ? `${s.slice(8, 10)}/${s.slice(5, 7)}/${s.slice(0, 4)} ${s.slice(11, 16)}` : "—");

async function loadDbSync() {
  try {
    const j = await API.get("api/dbsync");
    st.dbsync = j;
    st.dbinfo = j.db;
  } catch (err) {
    $("#dbInfo").textContent = err.message;
  }
  renderDbSync();
}

function renderDbSync() {
  const s = st.dbsync ?? {};
  const db = st.dbinfo ?? {};
  const age = dayDiff(db.newest);
  $("#dbInfo").innerHTML = db.ok
    ? `DB: <b>${Number(db.count).toLocaleString("vi-VN")}</b> ticket · ticket mới nhất <b>${fmtDbTime(db.newest)}</b>${age != null ? ` <span class="${age > 2 ? "warn-text" : "ok-text"}">(${age <= 0 ? "hôm nay" : `${age} ngày trước`})</span>` : ""}`
    : `<span class="err-text">Không đọc được DB ${esc(db.error || "")}</span>`;
  const running = Boolean(s.running);
  for (const id of ["#btnDbNew", "#btnDbProbe", "#btnDbAuto", "#btnDbFull", "#btnDbSave", "#btnDbLogin"]) $(id).disabled = running;
  $("#btnDbStop").hidden = !running;
  const p = s.progress;
  $("#dbProg").hidden = !(running && p);
  if (p) $("#dbProg i").style.width = `${Math.min(100, Math.round((100 * p.page) / Math.max(1, p.pages)))}%`;
  const lines = s.lines ?? [];
  $("#dbLog").hidden = !lines.length;
  $("#dbLog").textContent = lines.slice(-14).join("\n");
  $("#dbLog").scrollTop = $("#dbLog").scrollHeight;
}

function dbFields() {
  const c = { cctsPageSize: Number($("#setCctsPage").value), cctsDelay: Number($("#setCctsDelay").value), cctsToolDir: $("#setCctsDir").value.trim() };
  if ($("#setSso").value.trim()) c.cctsSso = $("#setSso").value.trim();
  if ($("#setToken").value.trim()) c.cctsToken = $("#setToken").value.trim();
  c.cctsUser = $("#setCctsUser").value.trim();
  if ($("#setCctsPass").value) c.cctsPass = $("#setCctsPass").value;
  return c;
}
async function saveDb(quiet) {
  const j = await API.post("api/config", dbFields());
  cfg = j.config;
  $("#setSso").value = $("#setToken").value = $("#setCctsPass").value = "";
  fillDbFields();
  if (!quiet) toast("Đã lưu cài đặt đồng bộ DB.");
}
function fillDbFields() {
  if (!cfg) return;
  $("#setSso").placeholder = cfg.cctsSsoHint ? `Đã lưu ${cfg.cctsSsoHint} — để trống để giữ nguyên` : "dán giá trị sau ssoticket=";
  $("#setToken").placeholder = cfg.cctsTokenHint ? `Đã lưu ${cfg.cctsTokenHint} — để trống để giữ nguyên` : "dán giá trị token trong Payload";
  $("#setCctsUser").value = cfg.cctsUser ?? "";
  $("#setCctsPass").placeholder = cfg.cctsPassHint ? "Đã lưu (mã hoá) — để trống để giữ nguyên" : "mật khẩu đăng nhập console.cnpowercore.com";
  $("#btnDbForget").hidden = !cfg.cctsUser && !cfg.cctsPassHint;
  $("#setCctsPage").value = String(cfg.cctsPageSize ?? 100);
  $("#setCctsDelay").value = String(cfg.cctsDelay ?? 3);
  $("#setCctsDir").value = cfg.cctsToolDir ?? "";
}
async function startDbSync(mode) {
  if (mode === "full" && !confirm("Quét lại toàn bộ ~370.000 ticket từ trang 1 — có thể mất nhiều giờ. Tiếp tục?")) return;
  try {
    await saveDb(true);
    const j = await API.post("api/dbsync/start", { mode });
    toast((j.login ? "Đang tự đăng nhập… " : "") + (mode === "probe" ? "Đang kiểm tra kết nối…" : "Đang lấy dữ liệu từ web — theo dõi tiến độ bên dưới."));
  } catch (err) { fail(err); }
  loadDbSync();
}
$("#btnDbSave").onclick = () => saveDb(false).catch(fail);
$("#btnDbLogin").onclick = async () => {
  const msg = $("#dbLoginMsg");
  $("#btnDbLogin").disabled = true;
  msg.textContent = "Đang đăng nhập…";
  try {
    await saveDb(true);
    const j = await API.post("api/dbsync/login", {});
    cfg = j.config;
    fillDbFields();
    msg.textContent = "✔ Đăng nhập được — đã lấy cookie + token mới.";
  } catch (err) {
    msg.textContent = `✖ ${err?.message || err}`;
  } finally {
    $("#btnDbLogin").disabled = false;
  }
};
$("#btnDbForget").onclick = async () => {
  if (!confirm("Xoá tài khoản + mật khẩu CNPowercore đã lưu?")) return;
  cfg = (await API.post("api/config", { cctsUser: "", cctsPass: "" })).config;
  fillDbFields();
  $("#dbLoginMsg").textContent = "";
};
$("#btnDbProbe").onclick = () => startDbSync("probe");
$("#btnDbNew").onclick = () => startDbSync("new");
$("#btnDbAuto").onclick = () => startDbSync("auto");
$("#btnDbFull").onclick = () => startDbSync("full");
$("#btnDbStop").onclick = () => send("api/dbsync/stop").catch(() => {});

async function openSettings(tab = "ai") {
  $("#settings").hidden = false;
  $("#setStatus").textContent = "";
  settingsTab(tab);
  try {
    cfg = await API.get("api/config");
  } catch (err) {
    $("#setStatus").className = "small err-text";
    $("#setStatus").textContent = err.message;
    return;
  }
  $("#setProvider").value = cfg.provider;
  $("#setKey").value = "";
  $("#setKey").placeholder = cfg.geminiKeyHint ? `Đã lưu key ${cfg.geminiKeyHint} — để trống để giữ nguyên` : "AIza…";
  $("#setGeminiModel").value = cfg.geminiModel;
  $("#setConfirmModel").value = cfg.geminiConfirmModel ?? "gemini-flash-latest";
  $("#setOllamaUrl").value = cfg.ollamaUrl;
  $("#setOllamaModel").value = cfg.ollamaModel;
  $("#setParallel").value = String(cfg.parallel);
  $("#setDateFormat").value = cfg.dateFormat;
  $("#setSpeed").value = cfg.speed ?? "fast";
  $("#setReadMode").value = cfg.readMode ?? "full";
  $("#setAiMaxSide").value = String(cfg.aiMaxSide ?? 1024);
  showReadModeNote();
  $("#setConfirm").checked = cfg.aiConfirm !== false;
  showProviderFields();
  fillDbFields();
  fillPcbaFields();
}
function formConfig() {
  const c = {
    provider: $("#setProvider").value,
    geminiModel: $("#setGeminiModel").value.trim(),
    geminiConfirmModel: $("#setConfirmModel").value.trim(),
    ollamaUrl: $("#setOllamaUrl").value.trim(),
    ollamaModel: $("#setOllamaModel").value.trim(),
    parallel: Number($("#setParallel").value),
    dateFormat: $("#setDateFormat").value,
    speed: $("#setSpeed").value,
    readMode: $("#setReadMode").value,
    aiMaxSide: Number($("#setAiMaxSide").value),
    aiConfirm: $("#setConfirm").checked,
  };
  const key = $("#setKey").value.trim();
  if (key) c.geminiKey = key;
  return c;
}
$("#setProvider").onchange = showProviderFields;
function showReadModeNote() {
  document.querySelectorAll("#settings [data-readmode]").forEach((el) => (el.hidden = el.dataset.readmode !== $("#setReadMode").value));
}
$("#setReadMode").onchange = showReadModeNote;
$("#btnSettings").onclick = () => openSettings();
$("#btnTestKey").onclick = async () => {
  const s = $("#setStatus");
  s.className = "small muted";
  s.textContent = "Đang kiểm tra…";
  const c = formConfig();
  const j = await API.post("api/config/test", c).catch((e) => ({ ok: false, error: e.message }));
  if (!j.ok) { s.className = "small err-text"; s.textContent = j.error; return; }
  s.className = "small ok-text";
  if (c.provider === "gemini") {
    $("#geminiModels").innerHTML = j.models.map((m) => `<option value="${esc(m)}">`).join("");
    if (!j.models.includes(c.geminiModel) && !/latest$/.test(c.geminiModel)) $("#setGeminiModel").value = j.suggest;
    s.textContent = `Key hợp lệ ✓ — ${j.models.length} model khả dụng.`;
  } else {
    $("#ollamaModels").innerHTML = j.models.map((m) => `<option value="${esc(m)}">`).join("");
    s.textContent = j.models.includes(c.ollamaModel) ? `Ollama đang chạy ✓ — có model ${c.ollamaModel}.` : `Ollama đang chạy nhưng chưa có "${c.ollamaModel}". Có sẵn: ${j.models.join(", ") || "không có"}`;
    if (!j.models.includes(c.ollamaModel)) s.className = "small err-text";
  }
};
$("#btnDeleteKey").onclick = async () => {
  if (!confirm("Xóa Gemini API key khỏi máy tính chạy server?")) return;
  await send("api/config", { geminiKey: "" }).catch(() => {});
  $("#setKey").placeholder = "AIza…";
  $("#setStatus").className = "small muted";
  $("#setStatus").textContent = "Đã xóa key.";
};
$("#settingsForm").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await API.post("api/config", formConfig());
    $("#settings").hidden = true;
    toast("Đã lưu cài đặt — áp dụng cho mọi thiết bị.");
  } catch (err) {
    $("#setStatus").className = "small err-text";
    $("#setStatus").textContent = err.message;
  }
};

// Đóng modal khi bấm ra nền.
for (const id of ["settings", "colModal", "fillModal", "phoneModal", "tgModal"]) {
  $(`#${id}`).addEventListener("click", (e) => { if (e.target.id === id) e.target.hidden = true; });
}

// ---------- Khởi động ----------
API.listen(onEvent, (ok) => {
  st.connected = ok;
  renderSync();
  renderBanners();
});
API.get("api/info").then((info) => { st.info = info; renderBanners(); }).catch(() => {});
$("#tgbar").addEventListener("click", (e) => {
  const f = e.target.closest("[data-filter]")?.dataset.filter;
  if (!f) return;
  filter = f;
  pref.set("filter", f);
  render();
});
refreshTgStatus();
setInterval(() => document.visibilityState === "visible" && refreshTgStatus(), 15000);
setZoom(pref.get("gridZoom", 100));
render();
