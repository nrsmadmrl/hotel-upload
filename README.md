# hotel-upload

Hotel price collection for the Rotelio pilot (accommodation data). The collector runs in
GitHub Actions twice a week and writes its results to the `data` branch. The Rotelio app
repository does not run collectors or store datasets; this repository does.

## How it works

- **Source:** obilet. One browser page per region is opened once for the site's cookies;
  every price query is then sent from inside that page. One query returns every hotel of
  the obilet location, so nothing is chosen or frozen in advance.
- **Regions:** the eight pilot regions of the flight data, same `region_id`s. Each has a
  centre and a radius in `hotels/obilet_regions.json`; only hotels inside it are kept.
- **Stays:** a one-night stay for every check-in date in the next 60 days (the flight
  horizon), for 2 adults (1 adult: first 30 days), plus a few real 3/7-night stays to check how well the sum
  of nights matches a multi-night price. About 95 queries per region, 10 seconds apart; if obilet refuses (HTTP 403/429) the
  region pauses once, then stops for that run.
- **Schedule:** Tuesday and Friday 03:17 Turkish time (`.github/workflows/hotel-collect.yml`),
  three regions at a time, about 25 minutes each.
- **Tiers:** `hotels/segments.py` turns a run into Economy / Standard / Premium hotels for
  given dates, using only hotels bookable on every night.

Field definitions: [hotels/DATA_CONTRACT.md](hotels/DATA_CONTRACT.md).

## Running it by hand

Actions tab → **hotel-collect** → **Run workflow**. For a short trial set `limit` to 2–3 and
`target_branch` to `data-test`, so the real `data` branch stays clean.

Locally (Windows: `py` instead of `python`), from the `hotels` folder:

```
python -m pip install -r requirements.txt
python -m playwright install chromium
python obilet.py plan
python obilet.py collect --region antalya --limit 5
python segments.py --run out/2026-10-30 --region antalya --check-in 2026-11-14 --check-out 2026-11-17
python -m unittest discover -s tests -v
```

New location ids: `python obilet_locate.py "Place name"` (or the **obilet-locate** workflow).

## Older code

`collect_hotels.py`, `trivago.py`, `selection.py`, `cohort.py` and `pilot_hotels.json` are
the earlier Trivago collector. It is kept as a fallback and is not scheduled. The
`obilet_*probe.py` scripts were the access tests that led to the current design.
