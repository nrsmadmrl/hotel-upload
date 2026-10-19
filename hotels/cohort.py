"""Hotel stay dates for the "user picks any check-in / check-out" design.

Why not one query per (check_in, check_out)?  A 30-day window has hundreds of
possible pairs.  Instead we collect:

  kind="night"       one-night stay for EVERY date in the flight window.
                     A user-chosen range is priced by summing its nights
                     (see trip_cost.py).  ~29 queries per region per day.
  kind="validation"  a few real multi-night searches (3/7/14 nights) on fixed
                     check-in dates.  They measure how far "sum of nights" is
                     from Trivago's real multi-night price (trip_cost.py validate).

The flight window is the one in the flights file: collected on day D, flights
cover D+1 ... D+30.  It is configured in pilot_hotels.json:

    "window_start_offset": 1,
    "window_days": 30,
    "validation_nights": [3, 7, 14],
    "validation_anchor": "2026-10-22",
    "validation_every_days": 7,
    "validation_count": 3

Validation check-in dates are generated automatically (see validation_check_ins).

Drop-in replacement for the old cohort.py (same names: TZ, today, stay_pairs).
It no longer needs transportation/pilot_config.json.
"""

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Istanbul")
ROOT = Path(__file__).resolve().parent


def today():
    return datetime.now(TZ).date()


def load_cfg():
    return json.loads((ROOT / "pilot_hotels.json").read_text(encoding="utf-8"))


def flight_window(day, cfg=None):
    """(first, last) departure date covered by the flight data collected on `day`."""
    cfg = cfg or load_cfg()
    start = day + timedelta(days=cfg.get("window_start_offset", 1))
    end = start + timedelta(days=cfg.get("window_days", 30) - 1)
    return start, end


def validation_check_ins(day, cfg=None):
    """Automatic, stable validation check-in dates.

    Dates sit on a fixed calendar grid (anchor + k * every_days), so the same
    dates are re-priced every day until they pass; the next grid date then
    enters by itself. The first `count` grid dates that still leave room for the
    longest validation stay inside the flight window are used.
    """
    cfg = cfg or load_cfg()
    start, end = flight_window(day, cfg)
    anchor = date.fromisoformat(cfg.get("validation_anchor", "2026-10-22"))
    step = cfg.get("validation_every_days", 7)
    count = cfg.get("validation_count", 3)
    shortest = min(cfg.get("validation_nights", [3, 7, 14]))
    skip = max(0, -((start - anchor).days // -step))  # ceil: first grid index >= start
    out, k = [], skip
    while len(out) < count:
        a = anchor + timedelta(days=k * step)
        # keep a date while at least the shortest stay fits; longer stays that
        # do not fit are dropped one by one in stay_pairs
        if a + timedelta(days=shortest) > end:
            break
        out.append(a)
        k += 1
    return out


def _pair(check_in, nights, kind):
    return dict(
        check_in_date=check_in.isoformat(),
        check_out_date=(check_in + timedelta(days=nights)).isoformat(),
        nights=nights,
        kind=kind,
    )


def stay_pairs(day, nights=None, flight_cfg=None, cfg=None):
    """Validation stays first (most valuable), then every one-night stay.

    `nights` and `flight_cfg` are accepted only so old call sites keep working.
    """
    cfg = cfg or load_cfg()
    start, end = flight_window(day, cfg)
    pairs = []
    for a in validation_check_ins(day, cfg):
        for n in cfg.get("validation_nights", [3, 7, 14]):
            if a + timedelta(days=n) <= end:
                pairs.append(_pair(a, n, "validation"))
    d = start
    while d + timedelta(days=1) <= end:
        pairs.append(_pair(d, 1, "night"))
        d += timedelta(days=1)
    return pairs


def reference_stay(day, cfg=None):
    """Stay used by `discover` to pick low/mid/high hotels: prefer a 7-night validation stay."""
    pairs = stay_pairs(day, cfg=cfg)
    for p in pairs:
        if p["kind"] == "validation" and p["nights"] == 7:
            return p
    return pairs[0] if pairs else None


def probe_stays(day, disc=None, cfg=None):
    """Searches `discover` runs to learn which hotels are really bookable.

    Early / middle / late check-in dates of the flight window (offsets from its first
    day) x 1/3/7 nights, all configurable under "discovery" in pilot_hotels.json
    (or per region). Stays that do not fit inside the window are skipped.
    """
    cfg = cfg or load_cfg()
    disc = disc or cfg.get("discovery") or {}
    start, end = flight_window(day, cfg)
    out = []
    for offset in disc.get("probe_check_in_offsets", [3, 12, 21]):
        a = start + timedelta(days=offset)
        for n in disc.get("probe_nights", [1, 3, 7]):
            if a + timedelta(days=n) <= end:
                out.append(_pair(a, n, "probe"))
    return out


def requested_stay(check_in, check_out):
    """A date range the user cares about; a hotel must be bookable in it to be selected."""
    a, b = date.fromisoformat(check_in), date.fromisoformat(check_out)
    if b <= a:
        raise ValueError("check-out must be after check-in")
    return dict(check_in_date=a.isoformat(), check_out_date=b.isoformat(),
                nights=(b - a).days, kind="probe_requested")


if __name__ == "__main__":
    d = today()
    print("window:", *flight_window(d))
    ps = stay_pairs(d)
    print(len(ps), "stays,", sum(p["kind"] == "validation" for p in ps), "validation")
    for p in ps[:12]:
        print(p)