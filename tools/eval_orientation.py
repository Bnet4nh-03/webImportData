"""Đo độ chính xác của ocr.detect_orientation trên ảnh tem thật trong data/images (chỉ đọc, không gọi AI).

    python tools/eval_orientation.py [-v]

Đáp án xác định bằng mắt (21/10/2026): góc cần xoay (ngược chiều kim đồng hồ, như ocr.rotate_jpeg) để tem đứng thẳng.
Mỗi ảnh còn được xoay thêm 0/90/180/270° -> 4 ca thử biết trước đáp án.
Ảnh không có tem in sẵn (bo mạch, giấy ghi tay, băng dính) phải ra "không chắc".
"""

from __future__ import annotations

import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ocr  # noqa: E402

IMAGES = ROOT / "data" / "images"
TRUTH = {  # ảnh tem -> góc cần xoay
    "i1a8b92bd0f": 0, "i4696eba182": 0, "i14ce59283f": 0, "id4327f47aa": 0, "i47919da896": 0,
    "ib09c0d9283": 0, "ib38ccfb9ae": 0, "if1333f942a": 0, "i917d514254": 0,
    # 3 ảnh dưới đây đã được xoay đứng và đổi tên ngày 21/10 (if4c3207219, i45684be254, i99c11a9723 cũ cần 270°)
    "if34e142980": 0, "i3b57d76b6f": 0, "i2a7ec63124": 0, "i0c8795f9e4": 270,
    "i11b34fb114": 90, "i0cee1afd5e": 90,
}
NO_LABEL = ["ia8102a54d8", "i05f9ba05b0", "i611d22f8df", "i19d34792e8", "i55cb3bc0d2", "i8973772ec8", "id364677da7", "i5e0a7ace34"]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    verbose = "-v" in sys.argv
    res, ms = Counter(), []
    for img, truth in TRUTH.items():
        f = IMAGES / f"{img}.jpg"
        if not f.exists():
            print("thiếu ảnh", img)
            continue
        base = f.read_bytes()
        for k in (0, 90, 180, 270):
            want = (truth - k) % 360
            t = time.perf_counter()
            d = ocr.detect_orientation(ocr.rotate_jpeg(base, k))
            ms.append((time.perf_counter() - t) * 1000)
            tag = "đúng" if d["sure"] and d["deg"] == want else "SAI" if d["sure"] else "không chắc"
            res[tag] += 1
            if verbose or tag == "SAI":
                print(f"  {img} +{k:3}° cần {want:3} -> {d}  [{tag}]")
    n = sum(res.values())
    print(f"Ảnh có tem: {n} ca | đúng {res['đúng']} ({100 * res['đúng'] / n:.0f}%) | SAI {res['SAI']} | không chắc {res['không chắc']}"
          f" | {sum(ms) / len(ms):.0f} ms/ảnh")
    bad = 0
    for img in NO_LABEL:
        f = IMAGES / f"{img}.jpg"
        if f.exists():
            d = ocr.detect_orientation(f.read_bytes())
            bad += d["sure"]
            if verbose or d["sure"]:
                print(f"  (không tem) {img} -> {d}")
    print(f"Ảnh không có tem: {len(NO_LABEL)} ảnh, đoán chắc chắn (không mong muốn) {bad}")


if __name__ == "__main__":
    main()
