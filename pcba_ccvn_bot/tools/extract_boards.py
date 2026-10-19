"""Trích xuất danh sách Board (Bước 1) từ DOM Telegram Web của chat PCBA-CCVN.

Chỉ ĐỌC DOM, không click, không gửi tin nhắn.
Yêu cầu: Edge chạy với --remote-debugging-port=9222 (profile tự động hoá riêng),
tab web.telegram.org/k đang mở chat @pcba_ccvn_bot.

    python extract_boards.py --out ../output
"""
import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

CDP_URL = "http://127.0.0.1:9222"
BOT_PEER_ID = "8656506505"
BOARD_STEP_MARKER = "Bước 1: Chọn Board"
NON_BOARD_LABELS = {"❎ Hủy"}

log = logging.getLogger("extract_boards")

# Đọc mọi bubble đang có trong DOM; emoji <img alt> được thay bằng ký tự gốc.
JS_BUBBLES = r"""() => {
  const t = el => { if (!el) return ''; const c = el.cloneNode(true);
    c.querySelectorAll('img.emoji').forEach(i => i.replaceWith(i.alt));
    c.querySelectorAll('.time, .reply-markup').forEach(x => x.remove());
    return c.innerText.trim(); };
  return [...document.querySelectorAll('.bubble[data-mid]')].map(b => ({
    mid: +b.dataset.mid, out: b.classList.contains('is-out'), ts: +b.dataset.timestamp,
    text: t(b.querySelector('.message')),
    kb: [...b.querySelectorAll('.reply-markup-row')].map(r =>
          [...r.querySelectorAll('.reply-markup-button')].map(x => t(x)))
  }));
}"""


def get_telegram_page(browser):
    for ctx in browser.contexts:
        for page in ctx.pages:
            if "web.telegram.org" in page.url:
                return page
    raise RuntimeError("Không tìm thấy tab web.telegram.org trong Edge (CDP 9222)")


def ensure_bot_chat_open(page):
    if page.locator(".bubble[data-mid]").count() == 0:
        log.info("Mở chat PCBA-CCVN (peer %s)", BOT_PEER_ID)
        page.locator(f'a.chatlist-chat[data-peer-id="{BOT_PEER_ID}"]').click()
        page.wait_for_selector(".bubble[data-mid]", timeout=20000)


def collect_history(page, scroll_rounds):
    """Cuộn lên để tải thêm lịch sử; gộp theo message id."""
    seen = {}
    for _ in range(scroll_rounds + 1):
        for m in page.evaluate(JS_BUBBLES):
            seen[m["mid"]] = m
        page.evaluate("() => { const s = document.querySelector('#column-center .bubbles .scrollable'); if (s) s.scrollTop = 0; }")
        page.wait_for_timeout(1500)
    return [seen[k] for k in sorted(seen)]


def parse_board_label(label):
    if " - " not in label:
        return None, None
    board_id, board_name = label.split(" - ", 1)
    return board_id.strip(), board_name.strip()


def build_board_rows(messages):
    board_msgs = [m for m in messages if not m["out"] and BOARD_STEP_MARKER in m["text"]]
    if not board_msgs:
        raise RuntimeError("Không thấy tin nhắn 'Bước 1: Chọn Board' trong lịch sử đã tải")
    latest = board_msgs[-1]
    variants = {json.dumps(m["kb"], ensure_ascii=False) for m in board_msgs}
    if len(variants) > 1:
        log.warning("Danh sách Board khác nhau giữa các lần (%d biến thể) - dùng bản mới nhất", len(variants))
    seen_in = [m["mid"] for m in board_msgs]
    rows = []
    for r, row in enumerate(latest["kb"]):
        for c, label in enumerate(row):
            if label in NON_BOARD_LABELS:
                continue
            board_id, board_name = parse_board_label(label)
            rows.append({
                "step_name": "Chọn Board",
                "button_label": label,
                "board_id": board_id,
                "board_name": board_name,
                "callback_data": None,
                "message_id": latest["mid"],
                "menu_page": 1,
                "keyboard_row": r,
                "keyboard_col": c,
                "source": "Telegram Web DOM (button.reply-markup-button)",
                "notes": (f"Thấy giống hệt trong {len(board_msgs)} tin nhắn: {seen_in}. "
                          "callback_data không có trong DOM Telegram Web K."),
            })
    return rows, latest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "output"))
    ap.add_argument("--scroll-rounds", type=int, default=12)
    ap.add_argument("--raw", help="Lưu toàn bộ lịch sử đọc được ra file JSON (tuỳ chọn)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.stdout.reconfigure(encoding="utf-8")

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP_URL)
        page = get_telegram_page(browser)
        ensure_bot_chat_open(page)
        messages = collect_history(page, args.scroll_rounds)

    log.info("Đã đọc %d tin nhắn (mid %d..%d)", len(messages), messages[0]["mid"], messages[-1]["mid"])
    rows, latest = build_board_rows(messages)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "boards.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out / "boards.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    if args.raw:
        Path(args.raw).write_text(json.dumps(messages, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Ghi %d board từ message %d vào %s", len(rows), latest["mid"], out)


if __name__ == "__main__":
    main()
