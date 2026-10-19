// Đọc, xoay, cắt, nén ảnh và trình chỉnh ảnh.

const WORK_MAX_SIDE = 2400; // giới hạn khi chỉnh (tiết kiệm RAM điện thoại)
const SEND_MAX_SIDE = 1568; // kích thước tối ưu cho Claude Vision

export async function fileToCanvas(file) {
  let src;
  try {
    src = await createImageBitmap(file); // tự áp dụng hướng EXIF
  } catch {
    src = await loadImg(file);
  }
  if (!src.width || !src.height) throw new Error("Ảnh rỗng");
  const s = Math.min(1, WORK_MAX_SIDE / Math.max(src.width, src.height));
  const c = document.createElement("canvas");
  c.width = Math.round(src.width * s);
  c.height = Math.round(src.height * s);
  c.getContext("2d").drawImage(src, 0, 0, c.width, c.height);
  src.close?.();
  return c;
}

function loadImg(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("Định dạng ảnh không hỗ trợ")); };
    img.src = url;
  });
}

export function rotate(src, dir) {
  const c = document.createElement("canvas");
  c.width = src.height;
  c.height = src.width;
  const g = c.getContext("2d");
  g.translate(c.width / 2, c.height / 2);
  g.rotate((dir * Math.PI) / 2);
  g.drawImage(src, -src.width / 2, -src.height / 2);
  return c;
}

export function crop(src, x, y, w, h) {
  const c = document.createElement("canvas");
  c.width = Math.max(1, Math.round(w));
  c.height = Math.max(1, Math.round(h));
  c.getContext("2d").drawImage(src, x, y, w, h, 0, 0, c.width, c.height);
  return c;
}

export function canvasToJpeg(src, maxSide = SEND_MAX_SIDE, quality = 0.85) {
  const s = Math.min(1, maxSide / Math.max(src.width, src.height));
  let c = src;
  if (s < 1) {
    c = document.createElement("canvas");
    c.width = Math.round(src.width * s);
    c.height = Math.round(src.height * s);
    const g = c.getContext("2d");
    g.imageSmoothingQuality = "high";
    g.drawImage(src, 0, 0, c.width, c.height);
  }
  return new Promise((resolve, reject) =>
    c.toBlob((b) => (b ? resolve(b) : reject(new Error("Không nén được ảnh"))), "image/jpeg", quality),
  );
}

export function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1]);
    r.onerror = () => reject(r.error);
    r.readAsDataURL(blob);
  });
}

// ---------- Trình chỉnh ảnh ----------
/**
 * Mở trình chỉnh ảnh.
 * @param {HTMLCanvasElement} original
 * @param {{counter?:string,onRetake?:()=>void}} opts onRetake được gọi đồng bộ trong sự kiện click
 *   (cần cho iOS để mở lại camera).
 * @returns {Promise<{action:"ok",canvas:HTMLCanvasElement}|{action:"skip"}>}
 */
export function editImage(original, { counter = "", onRetake } = {}) {
  const modal = document.getElementById("editor");
  const view = document.getElementById("editorCanvas");
  const rectEl = document.getElementById("cropRect");
  const stage = view.parentElement;
  document.getElementById("editorCounter").textContent = counter;

  let cur = original;
  let sel = null; // vùng chọn theo tọa độ canvas thật
  let drag = null;

  const draw = () => {
    view.width = cur.width;
    view.height = cur.height;
    view.getContext("2d").drawImage(cur, 0, 0);
    sel = null;
    rectEl.hidden = true;
  };

  const toCanvasPt = (e) => {
    const r = view.getBoundingClientRect();
    const k = view.width / r.width;
    return {
      x: Math.min(Math.max(e.clientX - r.left, 0), r.width) * k,
      y: Math.min(Math.max(e.clientY - r.top, 0), r.height) * k,
    };
  };

  const showRect = () => {
    if (!sel) { rectEl.hidden = true; return; }
    const r = view.getBoundingClientRect();
    const sr = stage.getBoundingClientRect();
    const k = r.width / view.width;
    rectEl.hidden = false;
    rectEl.style.left = `${r.left - sr.left + sel.x * k}px`;
    rectEl.style.top = `${r.top - sr.top + sel.y * k}px`;
    rectEl.style.width = `${sel.w * k}px`;
    rectEl.style.height = `${sel.h * k}px`;
  };

  const onDown = (e) => {
    view.setPointerCapture(e.pointerId);
    drag = toCanvasPt(e);
    sel = null;
    showRect();
  };
  const onMove = (e) => {
    if (!drag) return;
    const p = toCanvasPt(e);
    sel = { x: Math.min(p.x, drag.x), y: Math.min(p.y, drag.y), w: Math.abs(p.x - drag.x), h: Math.abs(p.y - drag.y) };
    showRect();
  };
  const onUp = () => {
    drag = null;
    if (sel && (sel.w < 30 || sel.h < 30)) { sel = null; showRect(); } // chạm nhầm
  };

  view.addEventListener("pointerdown", onDown);
  view.addEventListener("pointermove", onMove);
  view.addEventListener("pointerup", onUp);
  view.addEventListener("pointercancel", onUp);
  window.addEventListener("resize", showRect);

  draw();
  modal.hidden = false;

  return new Promise((resolve) => {
    const onClick = (e) => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (!act) return;
      if (act === "rotl" || act === "rotr") { cur = rotate(cur, act === "rotl" ? -1 : 1); draw(); return; }
      if (act === "reset") { cur = original; draw(); return; }
      if (act === "ok") finish({ action: "ok", canvas: sel ? crop(cur, sel.x, sel.y, sel.w, sel.h) : cur });
      if (act === "skip") finish({ action: "skip" });
      if (act === "retake") { onRetake?.(); finish({ action: "skip" }); }
    };
    const finish = (result) => {
      modal.hidden = true;
      modal.removeEventListener("click", onClick);
      view.removeEventListener("pointerdown", onDown);
      view.removeEventListener("pointermove", onMove);
      view.removeEventListener("pointerup", onUp);
      view.removeEventListener("pointercancel", onUp);
      window.removeEventListener("resize", showRect);
      resolve(result);
    };
    modal.addEventListener("click", onClick);
  });
}
