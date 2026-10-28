"""obilet hotel feasibility probe (writes nothing except probe_out_obilet/).

Questions it answers:
  1. does the results page open (or is it blocked) from this machine / from GitHub Actions?
  2. are hotel cards and prices visible in the page text?
  3. which background JSON calls carry the hotel data? (if one does, no browser parsing is needed)

    py obilet_probe.py                 # default URL, check-in 5 days from now, 2 adults
    py obilet_probe.py --headed
    py obilet_probe.py --adults 1
    py obilet_probe.py --url "https://www.obilet.com/oteller/..."
"""

import argparse
import json
import re
from datetime import date, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

DEFAULT_URL = ("https://www.obilet.com/oteller/kemer-609-60689_23501_125560-2/"
               "20261021-20261022/2ad?locationLevel=5")
BLOCK_RE = re.compile(r"access denied|verify you are human|captcha|unusual traffic|"
                      r"erişiminiz engellendi|too many requests", re.I)
DATE_PART = re.compile(r"/\d{8}-\d{8}/")
GUESTS_PART = re.compile(r"/\d+ad(?=[/?]|$)")
PRICE_RE = re.compile(r"\d{1,3}(?:\.\d{3})*\s*TL")
HOTELISH = re.compile(r"price|fiyat|hotel|otel|TL", re.I)
OUT = Path(__file__).resolve().parent / "probe_out_obilet"


def build_url(url, check_in, nights, adults):
    out = check_in + timedelta(days=nights)
    url = DATE_PART.sub(f"/{check_in:%Y%m%d}-{out:%Y%m%d}/", url)
    return GUESTS_PART.sub(f"/{adults}ad", url)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--days-ahead", type=int, default=5)
    ap.add_argument("--nights", type=int, default=1)
    ap.add_argument("--adults", type=int, default=2)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    check_in = date.today() + timedelta(days=args.days_ahead)
    url = build_url(args.url, check_in, args.nights, args.adults)
    OUT.mkdir(exist_ok=True)
    print("URL:", url)

    calls, saved = [], []

    def on_response(resp):
        try:
            rtype = resp.request.resource_type
            ctype = resp.headers.get("content-type", "")
            if rtype not in ("xhr", "fetch") or "json" not in ctype:
                return
            body = resp.text()
            calls.append(dict(url=resp.url, status=resp.status, chars=len(body)))
            if HOTELISH.search(body) and len(saved) < 8:
                name = OUT / f"json_{len(saved) + 1}.json"
                name.write_text(json.dumps(dict(url=resp.url, status=resp.status,
                                                body=body[:300000]), ensure_ascii=False, indent=1),
                                encoding="utf-8")
                saved.append(name.name)
        except Exception:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(locale="tr-TR", timezone_id="Europe/Istanbul",
                                      viewport={"width": 1366, "height": 900})
        page = context.new_page()
        page.on("response", on_response)
        resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        status = resp.status if resp else None
        page.wait_for_timeout(8000)
        for _ in range(6):
            page.mouse.wheel(0, 1400)
            page.wait_for_timeout(1200)
        text = page.inner_text("body")
        (OUT / "page.txt").write_text(text, encoding="utf-8")
        (OUT / "page.html").write_text(page.content(), encoding="utf-8")
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(OUT / "page.png"))
        final_url = page.url
        browser.close()

    summary = dict(
        requested_url=url, final_url=final_url, http_status=status,
        blocked_text=bool(BLOCK_RE.search(text)),
        oda_sec_buttons=len(re.findall(r"Oda Seç", text)),
        price_matches=len(PRICE_RE.findall(text)),
        json_calls=len(calls), saved_json=saved)
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("\nJSON calls seen (url, size):")
    for c in calls[:25]:
        print(f"  {c['status']} {c['chars']:>8}  {c['url'][:140]}")
    print("\nFiles in", OUT)


if __name__ == "__main__":
    main()