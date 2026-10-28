"""Find obilet hotel-search locations with the site's own search call.

Opens a results page (for the site's cookies) and sends each query to the search call the
destination box uses (POST /json/oteller), from inside the page. The location objects found
there (type / id / externalId / name / level) are what obilet.py needs in
obilet_regions.json.

    py obilet_locate.py "Konyaaltı" "Kaleiçi" "Çeşme"
    py obilet_locate.py --headed "Göreme"

In GitHub Actions every candidate is also printed as a ::notice:: line.
"""

import argparse
import json
import os
import sys
from pathlib import Path

START_URL = "https://www.obilet.com/oteller/kemer-609-60689_23501_125560-2/20261210-20261211/2ad?locationLevel=5"
OUT = Path(__file__).resolve().parent / "probe_out_obilet"
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"
SEARCH_PATH = "/json/oteller"
SEARCH_JS = """async (q) => {
  const r = await fetch("/json/oteller", {method: "POST", credentials: "include",
    headers: {"content-type": "application/json", "x-correlation-id": ""},
    body: JSON.stringify({query: q, page: 1})});
  return {status: r.status, text: await r.text()};
}"""


def walk(obj):
    """Yield every dict that looks like a location (has externalId or locationId)."""
    if isinstance(obj, dict):
        if "externalId" in obj or "locationId" in obj:
            yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def compact(d):
    keep = ("type", "id", "locationId", "externalId", "name", "text", "display", "level",
            "locationLevel", "parentName", "cityName", "isCityCenter", "hotelCount", "latitude", "longitude")
    return {k: d[k] for k in keep if k in d and not isinstance(d[k], (dict, list))}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("queries", nargs="+")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)

    from playwright.sync_api import sync_playwright

    found = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(locale="tr-TR", timezone_id="Europe/Istanbul",
                                      viewport={"width": 1366, "height": 900})
        page = context.new_page()
        page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

        for q in args.queries:
            # The search box calls POST /json/oteller {"query", "page"}; call it directly
            # from the page (same cookies) instead of typing, which can race the page load.
            page.wait_for_timeout(1500)
            res = page.evaluate(SEARCH_JS, q)
            responses = [(SEARCH_PATH, "POST", json.dumps({"query": q, "page": 1}, ensure_ascii=False),
                          res["text"])] if res["status"] == 200 else []
            if res["status"] != 200:
                print(f"   search status {res['status']}: {res['text'][:120]}")
            cands, urls = [], []
            for url, method, body, text in responses:
                try:
                    data = json.loads(text)
                except ValueError:
                    continue
                items = [compact(d) for d in walk(data)]
                if items:
                    urls.append(f"{method} {url} body={body}")
                    cands.extend(items)
            found[q] = dict(endpoints=urls, candidates=cands)
            print(f"\n== {q}: {len(cands)} candidates")
            for u in urls:
                print("   endpoint:", u[:300])
            for c in cands[:12]:
                line = json.dumps(c, ensure_ascii=False)
                print("  ", line)
            if IN_ACTIONS:  # GitHub keeps only ~10 notices per step: one compact line per query
                places = [c for c in cands if c.get("type") == 1][:6]
                print(f"::notice title=locate {q}::" + json.dumps(
                    [[c.get("id"), c.get("externalId"), c.get("text"), c.get("locationLevel"),
                      round(c.get("latitude") or 0, 4), round(c.get("longitude") or 0, 4)] for c in places],
                    ensure_ascii=False))
            if not cands:
                shot = OUT / f"locate_{len(found)}.png"
                page.screenshot(path=str(shot))
                print("   no candidates; screenshot:", shot)
        browser.close()
    (OUT / "locate.json").write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if any(v["candidates"] for v in found.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
