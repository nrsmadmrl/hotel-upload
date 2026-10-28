"""obilet hotel-list JSON probe v2 (writes only to probe_out_obilet/).

The site's own hotel-list call is captured while the results page loads (so we do not have to
guess its HTTP method or body). For each variant (2 adults / 1 night, 1 adult / 1 night,
2 adults / 3 nights) the results page is opened and the page's own response is read.

Questions it answers:
  1. Which HTTP method / body does the hotel-list call use?
  2. Does it return ALL hotels in a single response (no paging)?
  3. Do 1 adult and 2 adults give different prices for the same hotel ids?
  4. For a 3-night stay, is price.amount the stay total or the nightly price?
  5. Does a plain HTTP replay (no browser) work?

    py obilet_api_probe.py
    py obilet_api_probe.py --headed --days-ahead 7
"""

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

LOCATION = "60689_23501_125560"
PAGE_URL = "https://www.obilet.com/oteller/kemer-609-60689_23501_125560-2/{dr}/{ad}ad?locationLevel=5"
API_URL = "https://www.obilet.com/json/oteller/" + LOCATION + "/{dr}/{ad}ad/2"
OUT = Path(__file__).resolve().parent / "probe_out_obilet"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")


def dr(check_in, nights):
    return f"{check_in:%Y%m%d}-{check_in + timedelta(days=nights):%Y%m%d}"


def hotels_of(data):
    inner = data.get("data") if isinstance(data, dict) else None
    return (inner or {}).get("searchHotel") or []


def first_offer(h):
    offers = h.get("offers") or []
    return offers[0] if offers else {}


def price_of(h):
    return (first_offer(h).get("price") or {}).get("amount")


def board_of(h):
    return (first_offer(h).get("boardItem") or {}).get("boardCategoryName")


def summarize(label, data):
    hotels = hotels_of(data)
    prices = [p for p in (price_of(h) for h in hotels) if p]
    print(f"\n== {label}: {len(hotels)} hotels, {len(prices)} with a price")
    top = data.get("data") or {}
    print("   top-level keys:", list(data)[:12])
    print("   data keys:", list(top)[:25])
    hints = {k: v for k, v in list(data.items()) + list(top.items())
             if any(w in k.lower() for w in ("total", "count", "page", "next"))
             and not isinstance(v, (list, dict))}
    print("   count/paging hints:", hints or "none")
    if prices:
        print(f"   price min/median/max: {min(prices):.0f} / {statistics.median(prices):.0f} / {max(prices):.0f}")
    print("   boards:", dict(Counter(board_of(h) for h in hotels).most_common(8)))
    print("   suppliers:", dict(Counter((first_offer(h).get("supplier") or {}).get("name") for h in hotels).most_common(8)))
    print("   offer 'night' values:", dict(Counter(first_offer(h).get("night") for h in hotels)))
    print("   stars:", dict(sorted(Counter(h.get("stars") for h in hotels).items(), key=lambda kv: str(kv[0]))))
    refundable = sum(1 for h in hotels if first_offer(h).get("isRefundable"))
    print(f"   refundable first offers: {refundable}/{len(hotels)}")
    print("   sample:")
    for h in hotels[:5]:
        print(f"     {h.get('id')} {str(h.get('name'))[:32]:32} price={price_of(h)} board={board_of(h)} "
              f"score={h.get('averageScore')} reviews={h.get('commentCount')}")


def compare(label, a, b, a_name, b_name):
    """ratio = price in b / price in a, for hotels present in both with the same board."""
    pa = {h["id"]: h for h in hotels_of(a)}
    pb = {h["id"]: h for h in hotels_of(b)}
    common = set(pa) & set(pb)
    print(f"\n== {label}: {a_name} has {len(pa)} hotels, {b_name} has {len(pb)}, common {len(common)}, "
          f"only in {a_name}: {len(set(pa) - set(pb))}, only in {b_name}: {len(set(pb) - set(pa))}")
    ratios, diff_board, rows = [], 0, []
    for i in common:
        x, y = price_of(pa[i]), price_of(pb[i])
        if not (x and y):
            continue
        if board_of(pa[i]) != board_of(pb[i]):
            diff_board += 1
            continue
        ratios.append(y / x)
        rows.append((str(pa[i].get("name"))[:30], x, y))
    if not ratios:
        print("   no comparable hotels")
        return
    same = sum(1 for r in ratios if abs(r - 1) < 0.001)
    print(f"   comparable (same board): {len(ratios)}, board differs: {diff_board}")
    print(f"   ratio {b_name}/{a_name}: median {statistics.median(ratios):.3f}, "
          f"min {min(ratios):.3f}, max {max(ratios):.3f}, identical price: {same}/{len(ratios)}")
    for name, x, y in rows[:6]:
        print(f"     {name:30} {a_name}={x:.0f}  {b_name}={y:.0f}")


SHOW_HEADERS = ("content-type", "accept", "x-requested-with")


def capture(page, url, wait_s=40):
    """Open a results page and return the page's own hotel-list request/response."""
    got = {}

    def on_response(resp):
        if got or "/json/oteller/" + LOCATION not in resp.url:
            return
        try:
            req = resp.request
            got.update(url=resp.url, status=resp.status, method=req.method,
                       post_data=req.post_data, headers=dict(req.headers), body=resp.body())
        except Exception as error:
            got.update(url=resp.url, status=resp.status, error=f"{type(error).__name__}: {error}")

    page.on("response", on_response)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        for _ in range(wait_s):
            if got:
                break
            page.wait_for_timeout(1000)
    finally:
        page.remove_listener("response", on_response)
    return got


def describe(label, cap):
    print(f"\n[{label}] {cap.get('method')} {cap.get('url')}")
    print(f"   status {cap.get('status')}, {len(cap.get('body') or b''):,} bytes")
    shown = {k: v for k, v in (cap.get("headers") or {}).items() if k in SHOW_HEADERS or k.startswith("x-")}
    print("   headers:", shown or "(none of interest)")
    print("   header names:", sorted((cap.get("headers") or {}).keys()))
    pd = cap.get("post_data")
    print("   body:", (pd[:800] + ("..." if len(pd) > 800 else "")) if pd else "(no body)")


def plain_replay(cap):
    """Repeat the captured request WITHOUT a browser and without cookies."""
    data = cap["post_data"].encode("utf-8") if cap.get("post_data") else None
    headers = {"User-Agent": UA, "Accept": "application/json", "Referer": "https://www.obilet.com/"}
    for name in ("content-type", "x-requested-with"):
        if (cap.get("headers") or {}).get(name):
            headers[name] = cap["headers"][name]
    req = urllib.request.Request(cap["url"], data=data, headers=headers, method=cap["method"])
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()[:300]
    except Exception as error:
        return None, str(error).encode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days-ahead", type=int, default=5)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    check_in = date.today() + timedelta(days=args.days_ahead)
    OUT.mkdir(exist_ok=True)

    from playwright.sync_api import sync_playwright

    results, caps = {}, {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(locale="tr-TR", timezone_id="Europe/Istanbul",
                                      viewport={"width": 1366, "height": 900})
        page = context.new_page()
        for i, (label, nights, adults) in enumerate((("2ad_1n", 1, 2), ("1ad_1n", 1, 1), ("2ad_3n", 3, 2))):
            if i:
                time.sleep(10)
            url = PAGE_URL.format(dr=dr(check_in, nights), ad=adults)
            cap = capture(page, url)
            if not cap:
                print(f"{label}: the page made no hotel-list call within the time limit ({url})")
                continue
            caps[label] = cap
            describe(label, cap)
            if cap.get("error"):
                print("   could not read the response:", cap["error"])
                continue
            body = cap["body"]
            (OUT / f"full_{label}.json").write_bytes(body)
            (OUT / f"request_{label}.txt").write_text(
                f"{cap['method']} {cap['url']}\n{cap.get('post_data') or ''}\n", encoding="utf-8")
            if cap["status"] == 200:
                try:
                    results[label] = json.loads(body)
                except ValueError:
                    print("   response is not JSON:", body[:150])
        browser.close()

    if "2ad_1n" in caps and not caps["2ad_1n"].get("error"):
        status, body = plain_replay(caps["2ad_1n"])
        print(f"\nplain replay (no browser, no cookies): status {status}, {len(body):,} bytes")
        if status != 200:
            print("   ", body[:200])

    for label, data in results.items():
        summarize(label, data)
    if "2ad_1n" in results and "1ad_1n" in results:
        compare("1 adult vs 2 adults (1 night)", results["2ad_1n"], results["1ad_1n"], "2ad", "1ad")
    if "2ad_1n" in results and "2ad_3n" in results:
        compare("3 nights vs 1 night (2 adults)", results["2ad_1n"], results["2ad_3n"], "1n", "3n")
        print("   ratio near 3 = amount is the stay TOTAL; near 1 = amount is the NIGHTLY price")
    print("\nfull responses saved in", OUT)


if __name__ == "__main__":
    main()
