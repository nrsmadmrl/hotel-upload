"""Economy / Standard / Premium hotels for a requested stay (no network, tested offline).

Input is one collection run of obilet.py (<run>/<region>/offers.csv.gz + hotels.csv).
For the requested check-in / check-out and adult count:

  1. stay price  per hotel: the site's own multi-night price when that exact stay was
                 collected (kind=validation), otherwise the sum of its one-night prices.
                 A hotel that is not bookable on every night is left out (no guessing).
  2. quality     guest score / review count / star floors (defaults below, overridable)
  3. tiers       price terciles of the remaining hotels' average nightly price:
                 economy < standard < premium (the app's stay tiers). Tiers describe
                 hotels that are bookable for these dates, so they move with the season.

    py segments.py --run out/2026-10-30 --region antalya --check-in 2026-11-14 --check-out 2026-11-17
    py segments.py ... --adults 1 --meal breakfast --json

Money stays integer kurus. Mixed meal plans across nights are reported, not hidden.
"""

import argparse
import csv
import gzip
import json
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

TIERS = ("economy", "standard", "premium")
DEFAULTS = dict(min_score=7.0, min_comments=10, min_stars=0)
# obilet board categories -> the app's meal request (room only / breakfast / all inclusive)
MEAL_GROUPS = {
    "room_only": {"Sadece Oda"},
    "breakfast": {"Kahvaltı Dahil", "Oda Kahvaltı Plus", "Yarım Pansiyon", "Tam Pansiyon",
                  "Her Şey Dahil", "Ultra Her Şey Dahil"},
    "all_inclusive": {"Her Şey Dahil", "Ultra Her Şey Dahil"},
}


def read_csv(path):
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def load_run(run_dir, region_id):
    base = Path(run_dir) / region_id
    return read_csv(base / "offers.csv.gz"), {h["hotel_id"]: h for h in read_csv(base / "hotels.csv")}


def _num(v, cast=float):
    try:
        return cast(v) if v not in (None, "") else None
    except ValueError:
        return None


def nights_of(check_in, check_out):
    a, b = date.fromisoformat(check_in), date.fromisoformat(check_out)
    if b <= a:
        raise ValueError("check-out must be after check-in")
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days)]


def stay_prices(offers, check_in, check_out, adults):
    """{hotel_id: {"total_minor", "method", "boards", "refundable_all"}} for hotels bookable
    on every night of the stay. Exact multi-night quotes win over the sum of nights."""
    wanted = nights_of(check_in, check_out)
    n = len(wanted)
    exact, per_night = {}, {}
    for r in offers:
        if r.get("analysis_eligible") != "true" or _num(r.get("search_adults"), int) != adults:
            continue
        total = _num(r.get("price_total_minor"), int)
        if not total:
            continue
        hid = r["hotel_id"]
        if r["check_in_date"] == check_in and r["check_out_date"] == check_out and n > 1:
            exact[hid] = dict(total_minor=total, method="exact_stay", boards=[r.get("board_category_name")],
                              refundable_all=r.get("is_refundable") == "true")
        elif _num(r.get("nights"), int) == 1 and r["check_in_date"] in wanted:
            per_night.setdefault(hid, {})[r["check_in_date"]] = r
    out = dict(exact)
    for hid, by_day in per_night.items():
        if hid in out or len(by_day) != n:
            continue
        rows = [by_day[d] for d in wanted]
        out[hid] = dict(total_minor=sum(int(r["price_total_minor"]) for r in rows), method="sum_of_nights",
                        boards=[r.get("board_category_name") for r in rows],
                        refundable_all=all(r.get("is_refundable") == "true" for r in rows))
    return out


def meal_ok(boards, meal):
    if not meal:
        return True
    allowed = MEAL_GROUPS[meal]
    return all(b in allowed for b in boards)


def quality_reason(h, rules):
    score, comments, stars = _num(h.get("average_score")), _num(h.get("comment_count"), int), _num(h.get("stars"))
    if score is None or score < rules["min_score"]:
        return "guest score below minimum"
    if comments is None or comments < rules["min_comments"]:
        return "too few reviews"
    if rules["min_stars"] and (stars is None or stars < rules["min_stars"]):
        return "star class below minimum"
    return None


def tiers_for_stay(offers, hotels, check_in, check_out, adults=2, meal=None, **rules):
    """-> {"tiers": {tier: [hotel dicts sorted by price]}, "excluded": {hotel_id: reason}, ...}"""
    rules = {**DEFAULTS, **{k: v for k, v in rules.items() if v is not None}}
    n = len(nights_of(check_in, check_out))
    prices = stay_prices(offers, check_in, check_out, adults)
    seen = {r["hotel_id"] for r in offers}
    excluded, pool = {}, []
    for hid in sorted(seen):
        h = hotels.get(hid, {})
        p = prices.get(hid)
        if p is None:
            excluded[hid] = "not bookable on every night"
            continue
        reason = quality_reason(h, rules)
        if reason is None and not meal_ok(p["boards"], meal):
            reason = f"meal plan is not {meal} on every night"
        if reason:
            excluded[hid] = reason
            continue
        boards = sorted({b for b in p["boards"] if b})
        pool.append(dict(hotel_id=hid, hotel_name=h.get("hotel_name"), stars=_num(h.get("stars")),
                         average_score=_num(h.get("average_score")), comment_count=_num(h.get("comment_count"), int),
                         distance_to_region_center_km=_num(h.get("distance_to_region_center_km")),
                         latitude=_num(h.get("latitude")), longitude=_num(h.get("longitude")),
                         photo_url=h.get("photo_url") or None,
                         stay_total_minor=p["total_minor"], average_nightly_minor=round(p["total_minor"] / n),
                         price_method=p["method"], meal_plans=boards, mixed_meal_plans=len(boards) > 1,
                         refundable_every_night=p["refundable_all"]))
    pool.sort(key=lambda x: (x["average_nightly_minor"], x["hotel_id"]))
    tiers = {t: [] for t in TIERS}
    k = len(pool)
    for i, h in enumerate(pool):
        h["tier"] = TIERS[min(len(TIERS) - 1, i * len(TIERS) // k)]
        tiers[h["tier"]].append(h)
    bands = {t: dict(count=len(v),
                     min_nightly_minor=v[0]["average_nightly_minor"] if v else None,
                     median_nightly_minor=round(statistics.median(x["average_nightly_minor"] for x in v)) if v else None,
                     max_nightly_minor=v[-1]["average_nightly_minor"] if v else None)
             for t, v in tiers.items()}
    return dict(check_in_date=check_in, check_out_date=check_out, nights=n, search_adults=adults,
                search_rooms=1, meal=meal, rules=rules, bookable=len(pool), bands=bands,
                tiers=tiers, excluded=excluded)


def validation_report(offers, adults=2):
    """How far is 'sum of nights' from the site's own multi-night price? ratio = sum / exact."""
    ratios = []
    stays = {(r["check_in_date"], r["check_out_date"]) for r in offers if r.get("stay_kind") == "validation"}
    for ci, co in sorted(stays):
        exact = {h: p for h, p in stay_prices(offers, ci, co, adults).items() if p["method"] == "exact_stay"}
        only_nights = [r for r in offers if r.get("stay_kind") == "night"]
        summed = stay_prices(only_nights, ci, co, adults)
        for hid, p in exact.items():
            if hid in summed:
                ratios.append(summed[hid]["total_minor"] / p["total_minor"])
    if not ratios:
        return dict(pairs=0)
    return dict(pairs=len(ratios), median_ratio=round(statistics.median(ratios), 3),
                p10=round(sorted(ratios)[len(ratios) // 10], 3), p90=round(sorted(ratios)[len(ratios) * 9 // 10], 3))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--run", required=True, help="collection folder, e.g. out/2026-10-30")
    ap.add_argument("--region", required=True)
    ap.add_argument("--check-in", required=True)
    ap.add_argument("--check-out", required=True)
    ap.add_argument("--adults", type=int, default=2)
    ap.add_argument("--meal", choices=sorted(MEAL_GROUPS), default=None)
    ap.add_argument("--min-score", type=float, default=None)
    ap.add_argument("--min-comments", type=int, default=None)
    ap.add_argument("--min-stars", type=float, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    offers, hotels = load_run(args.run, args.region)
    res = tiers_for_stay(offers, hotels, args.check_in, args.check_out, args.adults, args.meal,
                         min_score=args.min_score, min_comments=args.min_comments, min_stars=args.min_stars)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0
    print(f"{args.region} {args.check_in} -> {args.check_out} ({res['nights']} nights, {args.adults} adults): "
          f"{res['bookable']} bookable hotels")
    for t in TIERS:
        b = res["bands"][t]
        print(f"\n{t.upper()}  {b['count']} hotels" + (
            f", nightly {b['min_nightly_minor'] / 100:.0f}-{b['max_nightly_minor'] / 100:.0f} TRY" if b["count"] else ""))
        for h in res["tiers"][t][:8]:
            print(f"  {h['hotel_name'][:34]:34} {h['stay_total_minor'] / 100:>9.0f} TRY  score {h['average_score']} "
                  f"({h['comment_count']})  {','.join(h['meal_plans'])}  {h['price_method']}")
    reasons = {}
    for r in res["excluded"].values():
        reasons[r] = reasons.get(r, 0) + 1
    print("\nleft out:", reasons)
    print("sum-of-nights check:", validation_report(offers, args.adults))
    return 0


if __name__ == "__main__":
    sys.exit(main())
