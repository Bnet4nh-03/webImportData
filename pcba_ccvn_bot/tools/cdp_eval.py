"""Chạy 1 đoạn JS trong tab Telegram Web qua CDP (Edge --remote-debugging-port=9222) và in JSON kết quả."""
import json, sys
from playwright.sync_api import sync_playwright

CDP = "http://127.0.0.1:9222"

def get_tg_page(browser):
    for ctx in browser.contexts:
        for p in ctx.pages:
            if "web.telegram.org" in p.url:
                return p
    raise SystemExit("Không tìm thấy tab web.telegram.org")

if __name__ == "__main__":
    js = open(sys.argv[1], encoding="utf-8").read()
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(CDP)
        page = get_tg_page(b)
        res = page.evaluate(js)
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps(res, ensure_ascii=False, indent=1))
