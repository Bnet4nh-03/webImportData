// Kết nối tới server.py: gửi thao tác (POST) và nhận thay đổi từ mọi thiết bị (Server-Sent Events).

export const imgUrl = (id) => `api/img/${encodeURIComponent(id)}`;

async function readJson(res) {
  const j = await res.json().catch(() => ({}));
  if (res.status === 404 && j.error === "not found") {
    // Web đã cập nhật nhưng server.py vẫn chạy bản cũ (chưa có API này).
    throw new Error("Server đang chạy phiên bản cũ — tắt cửa sổ python server.py rồi chạy lại.");
  }
  if (!res.ok || j.ok === false) throw new Error(j.error || `Lỗi server (HTTP ${res.status})`);
  return j;
}

export async function get(path) {
  let res;
  try {
    res = await fetch(path, { cache: "no-store" });
  } catch {
    throw new Error("Không kết nối được server. Máy tính đã chạy python server.py chưa?");
  }
  return readJson(res);
}

export async function post(path, body = {}) {
  let res;
  try {
    res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  } catch {
    throw new Error("Mất kết nối tới server — thao tác chưa được lưu.");
  }
  return readJson(res);
}

/** Gửi ảnh JPEG. kind: "label" (ảnh tem, tạo dòng nếu chưa có row) | "after" (ảnh sau sửa). */
export async function upload(kind, rowId, blob, ocr = true) {
  const q = new URLSearchParams({ kind, ...(rowId ? { row: rowId } : {}), ...(ocr ? {} : { ocr: "0" }) });
  let res;
  try {
    res = await fetch(`api/upload?${q}`, { method: "POST", headers: { "Content-Type": "image/jpeg" }, body: blob });
  } catch {
    throw new Error("Mất kết nối tới server — ảnh chưa được gửi.");
  }
  return readJson(res);
}

/**
 * Nghe thay đổi từ server. Mỗi lần (kết nối lại), sự kiện đầu tiên là toàn bộ bảng ("state").
 * @param {(ev:object)=>void} onEvent
 * @param {(connected:boolean)=>void} onStatus
 */
export function listen(onEvent, onStatus) {
  let es;
  let retry;
  const open = () => {
    es = new EventSource("api/events");
    es.onopen = () => onStatus(true);
    es.onmessage = (e) => {
      try { onEvent(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };
    es.onerror = () => {
      onStatus(false);
      if (es.readyState === EventSource.CLOSED) {
        clearTimeout(retry);
        retry = setTimeout(open, 3000);
      }
    };
  };
  open();
  // Điện thoại khóa màn hình làm rớt kết nối: mở lại ngay khi quay lại tab.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && es.readyState === EventSource.CLOSED) open();
  });
}
