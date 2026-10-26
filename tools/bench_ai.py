"""Đo tốc độ + độ đúng của mô hình AI đọc tem trên ẢNH TEM THẬT trong bảng (dòng đã khớp DB = đáp án).

    python tools/bench_ai.py --provider ollama --model qwen3-vl:4b-instruct --side 1024 --n 3
    python tools/bench_ai.py --provider gemini --model gemini-flash-lite-latest --n 10

Chỉ đọc data/state*.json + ảnh; không sửa bảng. Gemini: ảnh tem gửi lên Google (dùng key trong data/config.json).
Kết quả mỗi tem: thời gian, SN / Ticket / Trạm đọc đúng không, và app có tự khớp đúng ticket không.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import matching as M  # noqa: E402
import ocr  # noqa: E402

DATA = ROOT / "data"
DB = r"D:\00_WORKSPACE\01_Python\04_Takedata_CCVN\ccts_tickets.db"


def samples() -> list[dict]:
    """Dòng đã khớp DB tự động (đáp án = giá trị ticket) còn ảnh; mỗi ảnh một lần."""
    seen, out = set(), []
    for f in sorted(DATA.glob("state*.json"), reverse=True):
        for r in json.loads(f.read_text("utf-8")).get("rows", []):
            m = r.get("match") or {}
            img = DATA / "images" / f"{r.get('image')}.jpg"
            if m.get("status") != "matched" or not img.exists() or r["image"] in seen:
                continue
            seen.add(r["image"])
            deg = 0 if r.get("upright") else int((r.get("orient") or {}).get("deg") or 0)
            out.append({"id": r["id"], "img": img, "deg": deg, "truth": {k: r["values"].get(k, "") for k in
                                                                          ("ticket_id", "sn", "station_code", "date_onsite")}})
    return out


def same(key: str, a: str, b: str) -> bool:
    x, y = M.norm_id(a), M.norm_id(b)
    if key == "station_code":
        x, y = M.norm_id(a.split(".")[-1]), M.norm_id(b.split(".")[-1])
    return bool(x) and x == y


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="ollama")
    ap.add_argument("--model", default="")
    ap.add_argument("--side", type=int, default=1024, help="cạnh dài ảnh gửi AI (0 = giữ nguyên)")
    ap.add_argument("--mode", default="full", help="full | quick | codes")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--no-match", action="store_true", help="không nạp DB để thử khớp ticket")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = json.loads((DATA / "config.json").read_text("utf-8"))
    cfg.update(provider=a.provider, aiMaxSide=a.side, readMode=a.mode, speed="fast")
    if a.model:
        cfg["ollamaModel" if a.provider == "ollama" else "geminiModel"] = a.model
    model = cfg["ollamaModel"] if a.provider == "ollama" else cfg["geminiModel"]
    idx = None
    if not a.no_match:
        idx = M.TicketIndex(DB)
        idx.ensure()
    rows = samples()[: a.n]
    print(f"== {a.provider} {model} · ảnh {a.side or 'gốc'}px · {a.mode} · {len(rows)} tem")
    tot = {"t": 0.0, "sn": 0, "ticket_id": 0, "station_code": 0, "match": 0}
    for s in rows:
        jpeg = s["img"].read_bytes()
        if s["deg"]:
            jpeg = ocr.rotate_jpeg(jpeg, s["deg"])
        t0 = time.time()
        try:
            res = ocr.extract(cfg, jpeg)
        except Exception as e:  # noqa: BLE001
            print(f"  {s['id']}: LỖI {e}")
            continue
        dt = time.time() - t0
        mapped = {k: f["value"] for k, f in ocr.map_to_columns(res["fields"], cfg["dateFormat"]).items()}
        ok = {k: same(k, mapped.get(k, ""), s["truth"][k]) for k in ("sn", "ticket_id", "station_code")}
        hit = "-"
        if idx:
            chars = ocr.map_chars(res["fields"])
            d = M.decide(mapped, idx.search_for(mapped, chars, fmt=cfg["dateFormat"]), cfg["dateFormat"], chars)
            hit = "✓" if d["status"] == "matched" and same("ticket_id", d["best"]["ticket"]["id"], s["truth"]["ticket_id"]) \
                else ("?" if d["status"] == "ambiguous" else "✗")
            tot["match"] += hit == "✓"
        tot["t"] += dt
        for k, v in ok.items():
            tot[k] += v
        print(f"  {s['id']}: {dt:6.1f}s  SN {'✓' if ok['sn'] else '✗'} {mapped.get('sn', '')!r:16} "
              f"Ticket {'✓' if ok['ticket_id'] else '✗'} {mapped.get('ticket_id', '')!r:22} "
              f"Trạm {'✓' if ok['station_code'] else '✗'} {mapped.get('station_code', '')!r:12} khớp DB {hit}")
    n = max(1, len(rows))
    print(f"-- TB {tot['t'] / n:.1f}s/tem · SN {tot['sn']}/{n} · Ticket {tot['ticket_id']}/{n} · Trạm {tot['station_code']}/{n}"
          + (f" · tự khớp đúng {tot['match']}/{n}" if idx else ""))


if __name__ == "__main__":
    main()
