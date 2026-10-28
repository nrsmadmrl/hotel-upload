"""Find obilet hotel-search locations by typing into the site's own search box.

Opens a results page, types each query into the destination box (#origin-input) and
prints every JSON response the page receives while typing. The location objects found
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
        responses = []

        def on_response(resp):
            try:
                if "obilet.com" in resp.url and "json" in (resp.headers.get("content-type") or ""):
                    responses.append((resp.url, resp.request.method, resp.request.post_data, resp.text()))
            except Exception:
                pass

        page.on("response", on_response)
        page.goto(START_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

        for q in args.queries:
            responses.clear()
            box = page.locator("#origin-input")
            box.click()
            page.wait_for_timeout(800)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.type(q, delay=120)
            page.wait_for_timeout(4000)
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
            if IN_ACTIONS:
                print(f"::notice title=locate {q}::" + json.dumps(cands[:10], ensure_ascii=False))
                if urls:
                    print(f"::notice title=endpoint {q}::" + urls[0][:900])
            if not cands:
                shot = OUT / f"locate_{len(found)}.png"
                page.screenshot(path=str(shot))
                print("   no candidates; screenshot:", shot)
        browser.close()
    (OUT / "locate.json").write_text(json.dumps(found, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if any(v["candidates"] for v in found.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
