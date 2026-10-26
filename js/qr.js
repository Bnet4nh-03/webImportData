// Đọc mã QR (SN bo mạch) từ ảnh chụp. Dùng ảnh chụp thay vì camera trực tiếp vì điện thoại mở web qua
// http://IP-LAN không được phép bật camera trực tiếp (cần https), còn chụp ảnh thì luôn được.
// Ưu tiên BarcodeDetector có sẵn của trình duyệt (Chrome Android: đọc cả QR, DataMatrix, mã vạch);
// không có thì dùng jsQR (js/vendor/jsQR.js, Apache-2.0, chỉ tải khi cần).

const FORMATS = ["qr_code", "data_matrix", "code_128", "code_39", "ean_13", "pdf417", "aztec"];

let jsqrLoading = null;
function loadJsQR() {
  if (window.jsQR) return Promise.resolve(window.jsQR);
  jsqrLoading ??= new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "js/vendor/jsQR.js";
    s.onload = () => resolve(window.jsQR);
    s.onerror = () => reject(new Error("Không tải được bộ đọc QR."));
    document.head.appendChild(s);
  });
  return jsqrLoading;
}

async function viaDetector(bitmap) {
  if (!("BarcodeDetector" in window)) return null;
  try {
    const supported = await window.BarcodeDetector.getSupportedFormats();
    const formats = FORMATS.filter((f) => supported.includes(f));
    if (!formats.length) return null;
    const found = await new window.BarcodeDetector({ formats }).detect(bitmap);
    // Nhiều mã trong ảnh: ưu tiên QR, rồi mã to nhất (gần camera nhất).
    found.sort((a, b) => (b.format === "qr_code") - (a.format === "qr_code")
      || b.boundingBox.width * b.boundingBox.height - a.boundingBox.width * a.boundingBox.height);
    return found[0]?.rawValue || null;
  } catch {
    return null;
  }
}

function pixels(bitmap, maxSide, crop = 1) {
  const sw = bitmap.width * crop, sh = bitmap.height * crop;
  const k = Math.min(1, maxSide / Math.max(sw, sh));
  const c = document.createElement("canvas");
  c.width = Math.round(sw * k);
  c.height = Math.round(sh * k);
  const ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(bitmap, (bitmap.width - sw) / 2, (bitmap.height - sh) / 2, sw, sh, 0, 0, c.width, c.height);
  return ctx.getImageData(0, 0, c.width, c.height);
}

async function viaJsQR(bitmap) {
  const jsQR = await loadJsQR();
  // Thử vài cỡ / vùng giữa: QR nhỏ trong ảnh lớn thường đọc được khi cắt vùng giữa.
  for (const [side, crop] of [[1600, 1], [1000, 1], [1400, 0.5], [2000, 0.6]]) {
    const img = pixels(bitmap, side, crop);
    const res = jsQR(img.data, img.width, img.height, { inversionAttempts: "attemptBoth" });
    if (res?.data) return res.data;
  }
  return null;
}

/** File ảnh -> nội dung mã (chuỗi) hoặc null nếu không thấy mã nào. */
export async function decodeFile(file) {
  const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  try {
    return (await viaDetector(bitmap)) ?? (await viaJsQR(bitmap));
  } finally {
    bitmap.close?.();
  }
}
