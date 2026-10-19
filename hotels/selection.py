"""Availability-aware hotel selection (no network, tested offline).

`discover` runs a few probe searches on Trivago (1/3/7 nights at an early, a middle and a
late check-in, plus an optional date range the user cares about) and hands the parsed hotel
cards of every search to build_registry():

  1. profile      per hotel: in how many probes did it show a VERIFIED price
                  (price_scope per_night / single_night), also for 1-night stays
  2. eligibility  only hotels that are really bookable are kept: rating/review floors,
                  a verified price in >= min_coverage of the probes, sold for 1 night
                  (a trip is priced as the sum of its nights) and priced in the user's
                  requested range when one was given
  3. segments     price terciles of the ELIGIBLE hotels (median nightly price), so
                  low / mid / high describe hotels you can actually book
  4. roles        per segment: `primaries_per_segment` hotels closest to the segment's
                  median price, then `backups_per_segment` similar hotels. Backups are
                  handed to primaries round-robin, so every primary gets a backup before
                  any primary gets a second one.

choose_active() is used at analysis time: when a primary is not bookable on every night of
the requested stay, the best complete backup is proposed instead.

Nothing here is specific to one region: thresholds come from pilot_hotels.json
("selection" / "discovery", optionally overridden inside a region entry).
"""

import math
import statistics

OK_SCOPES = ("per_night", "single_night")  # price scopes that are verified

DISCOVERY_DEFAULTS = dict(
    probe_nights=[1, 3, 7],
    probe_check_in_offsets=[3, 12, 21],
    min_coverage=0.75,        # share of probe searches in which the hotel needs a verified price
    require_one_night=True,   # hotel must also be priced in >= min_coverage of the 1-night probes
    max_pages=8,              # result pages read per probe search
)
SELECTION_DEFAULTS = dict(
    min_rating=7.5,
    min_reviews=100,
    min_stars=3,          # yeni
    primaries_per_segment=2,
    backups_per_segment=2,
    segments=["low", "mid", "high"],
)


def settings(cfg, region, key, defaults):
    """defaults < global cfg[key] < region[key]. Understands the old `per_segment` key."""
    raw = dict(cfg.get(key) or {})
    raw.update((region or {}).get(key) or {})
    merged = {**defaults, **raw}
    if key == "selection" and "per_segment" in raw and "primaries_per_segment" not in raw:
        merged["primaries_per_segment"] = raw["per_segment"]
    return merged


# ---------------- profiles ----------------

def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _mode(values):
    values = [v for v in values if v is not None]
    return max(set(values), key=values.count) if values else None


def build_profiles(probe_results):
    """probe_results: [{"stay": {..., "nights": n}, "required": bool, "records": [...]}].

    A probe in which a hotel does not appear counts as "not bookable" for that hotel.
    """
    n_probes = len(probe_results)
    n_one = sum(1 for p in probe_results if p["stay"]["nights"] == 1)
    n_required = sum(1 for p in probe_results if p.get("required"))
    raw = {}
    for pr in probe_results:
        nights = pr["stay"]["nights"]
        for r in pr["records"]:
            p = raw.setdefault(r["hotel_id"], dict(
                hotel_name=r["hotel_name"], stars=[], ratings=[], reviews=[], dist=[],
                listed=0, priced=0, priced_1n=0, required_priced=0, nightly=[], nights=set()))
            p["listed"] += 1
            p["stars"].append(r.get("stars"))
            p["ratings"].append(r.get("rating"))
            p["reviews"].append(r.get("review_count"))
            p["dist"].append(r.get("distance_km"))
            if r.get("price_minor") and r.get("price_scope") in OK_SCOPES:
                p["priced"] += 1
                p["nightly"].append(r["price_minor"] / 100)
                p["nights"].add(nights)
                if nights == 1:
                    p["priced_1n"] += 1
                if pr.get("required"):
                    p["required_priced"] += 1
    profiles = []
    for hotel_id, p in raw.items():
        reviews = [v for v in p["reviews"] if v is not None]
        profiles.append(dict(
            hotel_id=hotel_id, hotel_name=p["hotel_name"],
            stars=_mode(p["stars"]), rating=_median(p["ratings"]),
            review_count=max(reviews) if reviews else None, distance_km=_median(p["dist"]),
            n_probes=n_probes, listed=p["listed"], priced=p["priced"],
            coverage=p["priced"] / n_probes if n_probes else 0.0,
            coverage_1n=(p["priced_1n"] / n_one) if n_one else None,
            required_ok=(p["required_priced"] == n_required),
            nightly_median=_median(p["nightly"]),
            priced_nights=sorted(p["nights"]),
            min_nights_priced=min(p["nights"]) if p["nights"] else None))
    return profiles


def ineligible_reason(p, sel, disc):
    """None when the hotel may enter the segments, else a short reason."""
    if p["nightly_median"] is None:
        return "no verified price in any probe"
    if p["rating"] is None or p["rating"] < sel["min_rating"]:
        return "rating below minimum"
    if sel.get("min_stars") and (p["stars"] is None or p["stars"] < sel["min_stars"]):
        return "no star class or below minimum"
    if p["review_count"] is not None and p["review_count"] < sel["min_reviews"]:
        return "too few reviews"
    if not p["required_ok"]:
        return "not bookable in the requested range"
    if p["coverage"] < disc["min_coverage"]:
        return f"bookable in only {p['priced']}/{p['n_probes']} probe searches"
    if (disc.get("require_one_night", True) and p["coverage_1n"] is not None
            and p["coverage_1n"] < disc["min_coverage"]):
        return "not sold for 1 night"
    return None


# ---------------- segments, primaries, backups ----------------

def similarity_distance(a, b):
    """Smaller = more alike. Price dominates, then stars, guest rating, distance to centre."""
    d = 0.0
    if a["nightly_median"] and b["nightly_median"]:
        d += abs(math.log(a["nightly_median"] / b["nightly_median"]))
    if a.get("stars") and b.get("stars"):
        d += 0.15 * abs(a["stars"] - b["stars"])
    if a.get("rating") and b.get("rating"):
        d += 0.10 * abs(a["rating"] - b["rating"])
    if a.get("distance_km") is not None and b.get("distance_km") is not None:
        d += 0.02 * abs(a["distance_km"] - b["distance_km"])
    return d


def pick_backups(primaries, rest, count):
    """[(backup, primary_it_replaces)], round-robin over primaries, most similar first."""
    chosen, remaining = [], list(rest)
    load = [0] * len(primaries)
    for _ in range(count):
        if not remaining or not primaries:
            break
        i = min(range(len(primaries)), key=lambda k: (load[k], k))
        target = primaries[i]
        best = min(remaining, key=lambda c: (similarity_distance(c, target), -c["coverage"]))
        chosen.append((best, target))
        remaining.remove(best)
        load[i] += 1
    return chosen


def _entry(p, segment, role, rank, backup_for):
    return dict(
        hotel_id=p["hotel_id"], hotel_name=p["hotel_name"], segment=segment,
        stars=p["stars"], rating=p["rating"], review_count=p["review_count"],
        airport_distance_km=None,
        role=role, rank=rank, backup_for=backup_for,
        nightly_median=round(p["nightly_median"], 2), coverage=round(p["coverage"], 2),
        min_nights_priced=p["min_nights_priced"])


def segment_and_pick(pool, sel):
    segments = sel["segments"]
    k = len(segments)
    n_primary = sel["primaries_per_segment"]
    n_backup = sel["backups_per_segment"]
    pool = sorted(pool, key=lambda p: (p["nightly_median"], p["hotel_id"]))
    n = len(pool)
    if n < k:
        return []
    out = []
    for i, label in enumerate(segments):
        band = pool[i * n // k:(i + 1) * n // k]
        if not band:
            continue
        median = statistics.median(p["nightly_median"] for p in band)
        by_median = sorted(band, key=lambda p: (abs(p["nightly_median"] - median),
                                                -p["coverage"], p["hotel_id"]))
        primaries = by_median[:n_primary]
        rest = [p for p in band if p not in primaries]
        for rank, p in enumerate(primaries, 1):
            out.append(_entry(p, label, "primary", rank, None))
        for j, (b, target) in enumerate(pick_backups(primaries, rest, n_backup)):
            out.append(_entry(b, label, "backup", len(primaries) + j + 1, target["hotel_id"]))
    return out


def build_registry(probe_results, sel, disc):
    """-> (registry hotels, all profiles, {hotel_id: reason it was left out})."""
    profiles = build_profiles(probe_results)
    pool, excluded = [], {}
    for p in profiles:
        reason = ineligible_reason(p, sel, disc)
        if reason:
            excluded[p["hotel_id"]] = reason
        else:
            pool.append(p)
    return segment_and_pick(pool, sel), profiles, excluded


# ---------------- analysis time: propose a backup ----------------

def choose_active(hotels, complete):
    """hotels: registry entries of ONE segment. complete: {hotel_id: True if every night of
    the requested stay has a verified price}.

    Returns one decision per primary:
      {"hotel_id", "replaces": primary id or None, "status": "primary" | "backup" | "unavailable"}
    A primary that is not complete is replaced by the best complete, still unused backup
    (the one built for it first, then by rank). "unavailable" keeps the primary's id.
    """
    primaries = sorted((h for h in hotels if h.get("role", "primary") == "primary"),
                       key=lambda h: h.get("rank", 0))
    backups = sorted((h for h in hotels if h.get("role") == "backup"),
                     key=lambda h: h.get("rank", 0))
    used, out = set(), []
    for p in primaries:
        if complete.get(p["hotel_id"]):
            out.append(dict(hotel_id=p["hotel_id"], replaces=None, status="primary"))
            continue
        options = [b for b in backups if b["hotel_id"] not in used and complete.get(b["hotel_id"])]
        options.sort(key=lambda b: (b.get("backup_for") != p["hotel_id"], b.get("rank", 0)))
        if options:
            used.add(options[0]["hotel_id"])
            out.append(dict(hotel_id=options[0]["hotel_id"], replaces=p["hotel_id"], status="backup"))
        else:
            out.append(dict(hotel_id=p["hotel_id"], replaces=None, status="unavailable"))
    return out