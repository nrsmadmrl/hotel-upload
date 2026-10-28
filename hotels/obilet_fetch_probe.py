"""obilet probe v3: one browser page, many dates via in-page fetch (writes only to probe_out_obilet/).

v2 showed: the hotel-list JSON works from the browser but a plain HTTP replay gets 403.
This probe opens ONE results page (so the browser has the site's cookies), then asks for
other dates/guests with fetch() from inside that page, without navigating.

Questions it answers:
  1. Does in-page fetch work for dates other than the page's own? (status + hotel count)
  2. How long does one query take? (decides how fast a full collect run can be)
  3. Does it keep working for ~10 queries in a row, or does it get blocked?
  4. How many hotels are really near the searched area? (distance to the area centre)

    py obilet_fetch_probe.py
    py obilet_fetch_probe.py --headed --queries 6 --gap 5
"""

import argparse
import json
import statistics
import time
from datetime import date, timedelta
from pathlib import Path

LOCATION = "60689_23501_125560"
BODY = {"id": 609, "externalId": LOCATION, "locationId": 609, "name": "Kemer",
        "type": "2", "isCityCenter": False, "level": 5}
PAGE_URL = "https://www.obilet.com/oteller/kemer-609-60689_23501_125560-2/{dr}/2ad?locationLevel=5"
API_PATH = "/json/oteller/" + LOCATION + "/{dr}/{ad}ad/2"
OUT = Path(__file__).resolve().parent / "probe_out_obilet"

FETCH_JS = """async ([path, body]) => {
  const t0 = performance.now();
  try {
    const r = await fetch(path, {method: "POST", credentials: "include",
      headers: {"content-type": "application/json", "x-correlation-id": ""},
      body: JSON.stringify(body)});
    const text = await r.text();
    return {status: r.status, ms: performance.now() - t0, text: text};
  } catch (e) {
    return {status: null, ms: performance.now() - t0, text: String(e)};
  }
}"""


def dr(check_in, nights):
    return f"{check_in:%Y%m%d}-{check_in + timedelta(days=nights):%Y%m%d}"


def summarize(text):
    try:
        data = (json.loads(text).get("data") or {})
    except ValueError:
        return dict(hotels=None, note="not JSON: " + text[:80])
    hotels = data.get("searchHotel") or []
    priced = [h for h in hotels if (h.get("offers") or [{}])[0].get("price")]
    dist = [(h.get("nearestCityCenter") or {}).get("distanceKm") for h in hotels]
    dist = [d for d in dist if d is not None]
    return dict(hotels=len(hotels), priced=len(priced),
                unavailable=len(data.get("unavailableSearchHotel") or []),
                cached=data.get("isCachedResponse"), night=data.get("night"),
                within_10km=sum(1 for d in dist if d <= 10),
                within_15km=sum(1 for d in dist if d <= 15),
                median_km=round(statistics.median(dist), 1) if dist else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--queries", type=int, default=10, help="in-page queries after the first page")
    ap.add_argument("--gap", type=float, default=8, help="seconds between queries")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    start = date.today() + timedelta(days=3)

    # check-in offsets / nights / adults: a mix of what collect would ask for
    plan = [(i * 3, 1, 2 if i % 2 == 0 else 1) for i in range(args.queries)]
    plan[-1] = (7, 7, 2)  # one long stay too

    from playwright.sync_api import sync_playwright

    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(locale="tr-TR", timezone_id="Europe/Istanbul",
                                      viewport={"width": 1366, "height": 900})
        page = context.new_page()
        t0 = time.monotonic()
        page.goto(PAGE_URL.format(dr=dr(start, 1)), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(8000)
        print(f"results page opened in {time.monotonic() - t0:.1f}s, cookies: {len(context.cookies())}")

        for i, (offset, nights, adults) in enumerate(plan, 1):
            if i > 1:
                time.sleep(args.gap)
            check_in = start + timedelta(days=offset)
            path = API_PATH.format(dr=dr(check_in, nights), ad=adults)
            res = page.evaluate(FETCH_JS, [path, BODY])
            info = summarize(res["text"]) if res["status"] == 200 else dict(note=res["text"][:120])
            row = dict(n=i, check_in=check_in.isoformat(), nights=nights, adults=adults,
                       status=res["status"], seconds=round(res["ms"] / 1000, 1),
                       bytes=len(res["text"]), **info)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False))
            if res["status"] == 200 and i == 1:
                (OUT / "fetch_first.json").write_text(res["text"], encoding="utf-8")
        browser.close()

    ok = [r for r in rows if r["status"] == 200 and r.get("hotels")]
    print(f"\n{len(ok)}/{len(rows)} in-page queries returned hotels")
    if ok:
        print(f"seconds per query: median {statistics.median(r['seconds'] for r in ok):.1f}, "
              f"max {max(r['seconds'] for r in ok):.1f}")
    bad = [r for r in rows if r not in ok]
    if bad:
        print("failed:", [(r["n"], r["status"], r.get("note")) for r in bad])
    (OUT / "fetch_summary.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
