"""Twice-weekly obilet hotel prices for the Rotelio pilot regions.

One browser page per region is opened once (the site needs its own cookies); every price
query is then sent with fetch() from inside that page, without navigating. A single query
returns every hotel of the obilet location, so nothing is frozen in advance: each run
records every hotel inside the region radius, open or closed, and segments are computed
later for the dates a user asks about (segments.py).

Stays (all inside the flight horizon, default 60 days, see obilet_regions.json):
  kind="night"       one-night stay for every check-in date, for every adult count
  kind="validation"  a few real multi-night stays (2 adults) used to check how far the
                     sum of nightly prices is from the site's own multi-night price

Commands
    py obilet.py plan                         # show the queries, no network
    py obilet.py collect --region antalya --limit 5 --headed
    py obilet.py collect --region all
    py obilet.py collect --region antalya --out out/

Output (one folder per collection day and region):
    <out>/<YYYY-MM-DD>/<region_id>/offers.csv.gz   one row per hotel per query
    <out>/<YYYY-MM-DD>/<region_id>/hotels.csv      hotel facts seen in this run
    <out>/<YYYY-MM-DD>/<region_id>/queries.csv     one row per query (audit)
    <out>/<YYYY-MM-DD>/<region_id>/manifest.json   counts and settings

Safety rules: money is integer kurus; a missing price stays blank (never 0); a query is
kept only when the response echoes the requested dates; three failed queries in a row
stop the region (no retry loops against an access block).
"""

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "obilet_regions.json"
TZ = ZoneInfo("Europe/Istanbul")
SCHEMA_VERSION = "1.0"
SITE = "https://www.obilet.com"

# observation_id and hotel_name are computed for each row but not written: they are the
# least compressible columns (hotel names live in hotels.csv; query_id + hotel_id is unique).
OFFER_FIELDS = """schema_version query_id collection_date observed_at region_id
hotel_id stay_kind check_in_date check_out_date nights lead_days search_adults
search_children search_rooms availability_status currency price_total_minor price_per_night_minor
price_scope board_category_id board_category_name board_name is_refundable supplier
accommodation_tax_included response_cached response_expires_at analysis_eligible source""".split()

HOTEL_FIELDS = """region_id hotel_id hotel_name stars average_score comment_count hotel_type_id
latitude longitude distance_to_region_center_km obilet_center_name obilet_center_distance_km
photo_url seen_available seen_unavailable source""".split()

QUERY_FIELDS = """collection_date query_id region_id stay_kind check_in_date check_out_date nights
search_adults query_state http_status seconds hotels_total hotels_in_radius available_in_radius
unavailable_in_radius response_cached observed_at error_message""".split()

FETCH_JS = """async ([path, body]) => {
  const t0 = performance.now();
  try {
    const r = await fetch(path, {method: "POST", credentials: "include",
      headers: {"content-type": "application/json", "x-correlation-id": ""},
      body: JSON.stringify(body)});
    return {status: r.status, ms: performance.now() - t0, text: await r.text()};
  } catch (e) {
    return {status: null, ms: performance.now() - t0, text: String(e)};
  }
}"""


# ---------------- config and plan (no network) ----------------

def load_config(path=CONFIG):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def today():
    return datetime.now(TZ).date()


def _stay(check_in, nights, kind, adults):
    return dict(check_in_date=check_in.isoformat(),
                check_out_date=(check_in + timedelta(days=nights)).isoformat(),
                nights=nights, kind=kind, adults=adults)


def plan_stays(day, cfg):
    """Validation stays first (fewest, most valuable), then every one-night stay."""
    start = day + timedelta(days=cfg.get("start_offset_days", 1))
    last_checkout = day + timedelta(days=cfg["horizon_days"])
    stays = []
    val = cfg.get("validation") or {}
    for off in val.get("check_in_offsets", []):
        a = day + timedelta(days=off)
        for n in val.get("nights", []):
            if a >= start and a + timedelta(days=n) <= last_checkout:
                for ad in val.get("adults", [2]):
                    stays.append(_stay(a, n, "validation", ad))
    # adults_horizon_days: {"1": 30} = 1-adult nights only for check-ins in the first 30 days
    limits = {int(k): v for k, v in (cfg.get("adults_horizon_days") or {}).items()}
    d = start
    while d + timedelta(days=1) <= last_checkout:
        for ad in cfg.get("adults", [2]):
            if ad in limits and (d - day).days > limits[ad]:
                continue
            stays.append(_stay(d, 1, "night", ad))
        d += timedelta(days=1)
    return stays


def identifier(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:20]


def query_id(region_id, stay, rooms):
    return identifier("|".join(["obilet-hotel", SCHEMA_VERSION, region_id, stay["check_in_date"],
                                stay["check_out_date"], str(stay["adults"]), str(rooms), "TRY"]))


def date_range(stay):
    a = stay["check_in_date"].replace("-", "")
    b = stay["check_out_date"].replace("-", "")
    return f"{a}-{b}"


def api_path(region, stay):
    loc = region["location"]
    return f"/json/oteller/{loc['externalId']}/{date_range(stay)}/{stay['adults']}ad/{loc['type']}"


COOKIE_SLUG = "kemer-609-60689_23501_125560-2"  # any results page gives the site's cookies


def page_url(region, stay):
    """Results page opened once per region, only to get the site's cookies. The price
    queries name their own location in the request body, so a region without a known
    page slug falls back to a page that is known to work."""
    loc = region["location"]
    slug = region.get("slug") or COOKIE_SLUG
    level = loc.get("level", 5) if region.get("slug") else 5
    return f"{SITE}/oteller/{slug}/{date_range(stay)}/{stay['adults']}ad?locationLevel={level}"


def request_body(region):
    loc = region["location"]
    return {"id": loc["id"], "externalId": loc["externalId"], "locationId": loc.get("locationId", loc["id"]),
            "name": loc["name"], "type": str(loc["type"]), "isCityCenter": bool(loc.get("isCityCenter", False)),
            "level": loc.get("level", 5)}


# ---------------- parsing (no network, tested offline) ----------------

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def to_minor(amount):
    """TRY amount -> integer kurus; None/0/negative -> None (unknown, never free)."""
    if amount is None:
        return None
    try:
        value = round(float(amount) * 100)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def hotel_facts(h, region):
    geo = h.get("geoLocation") or {}
    lat, lon = geo.get("latitude"), geo.get("longitude")
    c = region["center"]
    dist = (round(haversine_km(c["lat"], c["lon"], lat, lon), 2)
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon) else None)
    near = h.get("nearestCityCenter") or {}
    return dict(region_id=region["region_id"], hotel_id=str(h.get("id")), hotel_name=h.get("name"),
                stars=h.get("stars"), average_score=h.get("averageScore"), comment_count=h.get("commentCount"),
                hotel_type_id=h.get("hotelTypeId"), latitude=lat, longitude=lon,
                distance_to_region_center_km=dist, obilet_center_name=near.get("locationName"),
                obilet_center_distance_km=near.get("distanceKm"), photo_url=h.get("photoUrl"), source="obilet")


def in_radius(facts, region):
    d = facts["distance_to_region_center_km"]
    return d is not None and d <= region["radius_km"]


def parse_response(data, region, stay, rooms, day, observed_at):
    """-> (offer rows, hotel facts by id, stats). Only hotels inside the radius are kept."""
    d = (data or {}).get("data") or {}
    qid = query_id(region["region_id"], stay, rooms)
    lead = (date.fromisoformat(stay["check_in_date"]) - day).days
    cached = d.get("isCachedResponse")
    expires = d.get("searchExpireOn")
    rows, facts_by_id = [], {}
    stats = dict(hotels_total=0, hotels_in_radius=0, available_in_radius=0, unavailable_in_radius=0)

    def base(f, status):
        return dict(schema_version=SCHEMA_VERSION,
                    observation_id=identifier("|".join([qid, f["hotel_id"], observed_at])),
                    query_id=qid, collection_date=day.isoformat(), observed_at=observed_at,
                    region_id=region["region_id"], hotel_id=f["hotel_id"], hotel_name=f["hotel_name"],
                    stay_kind=stay["kind"], check_in_date=stay["check_in_date"],
                    check_out_date=stay["check_out_date"], nights=stay["nights"], lead_days=lead,
                    search_adults=stay["adults"], search_children=0, search_rooms=rooms,
                    availability_status=status, currency="TRY", response_cached=cached,
                    response_expires_at=expires, source="obilet")

    for h in d.get("searchHotel") or []:
        stats["hotels_total"] += 1
        f = hotel_facts(h, region)
        if not in_radius(f, region):
            continue
        stats["hotels_in_radius"] += 1
        offers = h.get("offers") or []
        o = offers[0] if offers else {}
        price = o.get("price") or {}
        total = to_minor(price.get("amount")) if (price.get("currency") or "TRY") == "TRY" else None
        offer_nights = o.get("night")
        board = o.get("boardItem") or {}
        tax = ((o.get("taxInformation") or {}).get("accommodationTaxInfo") or {})
        available = bool(total) and o.get("isAvailable", True) is not False
        nights_ok = offer_nights in (None, stay["nights"])
        row = base(f, "available" if available else "listed_no_price")
        row.update(
            price_total_minor=total,
            price_per_night_minor=round(total / stay["nights"]) if total else None,
            price_scope="stay_total_one_room_all_guests",
            board_category_id=board.get("boardCategoryId"), board_category_name=board.get("boardCategoryName"),
            board_name=board.get("name"), is_refundable=o.get("isRefundable"),
            supplier=(o.get("supplier") or {}).get("name"),
            accommodation_tax_included=tax.get("included"),
            analysis_eligible=bool(available and nights_ok))
        rows.append(row)
        f["seen_available"] = 1 if available else 0
        f["seen_unavailable"] = 0
        facts_by_id[f["hotel_id"]] = f
        if available:
            stats["available_in_radius"] += 1

    for h in d.get("unavailableSearchHotel") or []:
        f = hotel_facts(h, region)
        if not in_radius(f, region):
            continue
        stats["unavailable_in_radius"] += 1
        row = base(f, "unavailable")
        row.update(analysis_eligible=False)
        rows.append(row)
        f["seen_available"] = 0
        f["seen_unavailable"] = 1
        if f["hotel_id"] not in facts_by_id:
            facts_by_id[f["hotel_id"]] = f
    return rows, facts_by_id, stats, cached


def response_matches(data, stay):
    """The site echoes the searched dates; a mismatch means the price is for other dates."""
    d = (data or {}).get("data") or {}
    ci, co = str(d.get("checkInDate") or "")[:10], str(d.get("checkOutDate") or "")[:10]
    if not ci or not co:
        return True  # field absent: cannot check, rely on the request path
    return ci == stay["check_in_date"] and co == stay["check_out_date"]


def merge_facts(acc, new):
    for hid, f in new.items():
        old = acc.get(hid)
        if old is None:
            acc[hid] = dict(f)
            continue
        old["seen_available"] += f["seen_available"]
        old["seen_unavailable"] += f["seen_unavailable"]
        for k, v in f.items():  # unavailable records carry fewer fields
            if old.get(k) in (None, "") and v not in (None, ""):
                old[k] = v


# ---------------- writing ----------------

def csv_value(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


def write_csv(path, fields, rows, compress=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    opener = (lambda: gzip.open(path, "wt", encoding="utf-8", newline="")) if compress else \
        (lambda: path.open("w", encoding="utf-8", newline=""))
    with opener() as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: csv_value(r.get(k)) for k in fields})


# ---------------- collection (network) ----------------

BLOCK_STATUS = (401, 403, 429)


def collect_region(p, cfg, region, day, args):
    rooms = cfg.get("rooms", 1)
    stays = plan_stays(day, cfg)
    if args.limit:
        stays = stays[: args.limit]
    gap = cfg.get("gap_seconds", 4)
    max_fail = cfg.get("max_consecutive_failures", 3)
    out_dir = Path(args.out) / day.isoformat() / region["region_id"]
    rid = region["region_id"]
    print(f"{rid}: {len(stays)} queries, radius {region['radius_km']} km -> {out_dir}")

    browser = p.chromium.launch(headless=not args.headed)
    context = browser.new_context(locale="tr-TR", timezone_id="Europe/Istanbul",
                                  viewport={"width": 1366, "height": 900})
    page = context.new_page()

    def open_page():
        page.goto(page_url(region, stays[0]), wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)

    def ask(stay):
        try:
            return page.evaluate(FETCH_JS, [api_path(region, stay), body])
        except Exception as error:  # page crashed or navigated away
            return dict(status=None, ms=0, text=f"{type(error).__name__}: {error}")

    pause = cfg.get("block_pause_seconds", 120)
    body = request_body(region)
    open_page()
    offers, facts, queries = [], {}, []
    fails, reopened, result = 0, False, 0
    for i, stay in enumerate(stays, 1):
        if i > 1:
            time.sleep(gap)
        observed_at = datetime.now(TZ).isoformat(timespec="seconds")
        q = dict(collection_date=day.isoformat(), query_id=query_id(rid, stay, rooms), region_id=rid,
                 stay_kind=stay["kind"], check_in_date=stay["check_in_date"],
                 check_out_date=stay["check_out_date"], nights=stay["nights"],
                 search_adults=stay["adults"], observed_at=observed_at)
        res = ask(stay)
        if res["status"] in BLOCK_STATUS and not reopened:
            # The site refused (rate limit or expired session). Respect it: pause, open the
            # results page once more and repeat this one query. A second refusal stops the
            # region below; no identity changes, no retry loop.
            reopened = True
            print(f"  HTTP {res['status']}: pausing {pause}s, then reopening the results page once")
            time.sleep(pause)
            try:
                open_page()
                res = ask(stay)
            except Exception as error:
                print("  reopen failed:", error)
        q.update(http_status=res["status"], seconds=round(res["ms"] / 1000, 1))
        state, data = None, None
        if res["status"] == 200:
            try:
                data = json.loads(res["text"])
            except ValueError:
                state = "not_json"
        else:
            state = "blocked" if res["status"] in BLOCK_STATUS else "http_error"
        if data is not None and not response_matches(data, stay):
            state, data = "dates_mismatch", None
        if data is not None:
            rows, fb, stats, cached = parse_response(data, region, stay, rooms, day, observed_at)
            offers.extend(rows)
            merge_facts(facts, fb)
            state = "ok" if stats["available_in_radius"] else "empty"
            q.update(stats, response_cached=cached)
            fails = 0
        else:
            q["error_message"] = (res["text"] or "")[:300]
            fails += 1
        q["query_state"] = state
        queries.append(q)
        print(f"  [{i}/{len(stays)}] {stay['kind']:10} {stay['check_in_date']} {stay['nights']}n "
              f"{stay['adults']}ad {state:14} {q.get('available_in_radius', '-')}/"
              f"{q.get('hotels_in_radius', '-')} in radius  {q['seconds']}s")
        if state == "blocked" or fails >= max_fail:
            why = f"HTTP {res['status']} again after the pause" if state == "blocked" \
                else f"{max_fail} failed queries in a row ({state})"
            print(f"  STOP {rid}: {why}")
            result = 3
            break
    browser.close()

    write_csv(out_dir / "offers.csv.gz", OFFER_FIELDS, offers, compress=True)
    write_csv(out_dir / "hotels.csv", HOTEL_FIELDS, sorted(facts.values(), key=lambda f: f["hotel_id"]))
    write_csv(out_dir / "queries.csv", QUERY_FIELDS, queries)
    states = {}
    for q in queries:
        states[q["query_state"]] = states.get(q["query_state"], 0) + 1
    manifest = dict(schema_version=SCHEMA_VERSION, source="obilet", region_id=rid,
                    collection_date=day.isoformat(), finished_at=datetime.now(TZ).isoformat(timespec="seconds"),
                    planned_queries=len(stays), run_queries=len(queries), query_states=states,
                    offer_rows=len(offers), hotels_seen=len(facts),
                    hotels_ever_available=sum(1 for f in facts.values() if f["seen_available"]),
                    stopped_early=result != 0,
                    settings=dict(radius_km=region["radius_km"], center=region["center"],
                                  horizon_days=cfg["horizon_days"], adults=cfg.get("adults"),
                                  rooms=rooms, validation=cfg.get("validation"),
                                  location=region["location"]))
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{rid}: {states} | offer rows {len(offers)} | hotels {len(facts)}")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        level = "warning" if result else "notice"
        print(f"::{level} title=obilet {rid}::{json.dumps(dict(states=states, offers=len(offers), hotels=len(facts)))}")
    return result


def cmd_collect(args):
    cfg = load_config(args.config)
    day = date.fromisoformat(args.date) if args.date else today()
    regions = [r for r in cfg["regions"] if args.region in ("all", r["region_id"])]
    missing = [r["region_id"] for r in regions if not (r.get("location") or {}).get("externalId")]
    if missing:
        print("no obilet location configured for:", ", ".join(missing), "(skipped)")
    regions = [r for r in regions if r["region_id"] not in missing]
    if not regions:
        print("nothing to collect")
        return 2
    from playwright.sync_api import sync_playwright
    result = 0
    with sync_playwright() as p:
        for region in regions:
            try:
                result = max(result, collect_region(p, cfg, region, day, args))
            except Exception as error:
                print(region["region_id"], "ERROR", type(error).__name__, str(error)[:300])
                result = max(result, 2)
    return result


def cmd_plan(args):
    cfg = load_config(args.config)
    day = date.fromisoformat(args.date) if args.date else today()
    stays = plan_stays(day, cfg)
    kinds = {}
    for s in stays:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    print(f"{day}: {len(stays)} queries per region {kinds}")
    for s in stays[:8]:
        print("  ", s)
    for r in cfg["regions"]:
        ok = "ok" if (r.get("location") or {}).get("externalId") else "NO LOCATION"
        print(f"  {r['region_id']:14} radius {r['radius_km']:>4} km  {ok}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--config", default=str(CONFIG))
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--region", default="all")
    c.add_argument("--limit", type=int, default=0, help="only the first N queries per region (testing)")
    c.add_argument("--out", default=str(ROOT / "out"))
    c.add_argument("--date", default=None, help="collection day override (testing only)")
    c.add_argument("--headed", action="store_true")
    pl = sub.add_parser("plan")
    pl.add_argument("--date", default=None)
    args = ap.parse_args(argv)
    return {"collect": cmd_collect, "plan": cmd_plan}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
